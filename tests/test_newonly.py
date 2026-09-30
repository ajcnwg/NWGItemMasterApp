import os
"""New-items-only Merge, end to end on the live data, then fully restored."""
from testbase import BASE, IMPORT_SNAP, KEEP
import json
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from sqlalchemy import text, bindparam
from itemmaster import dept_mapping as dm
from itemmaster import monthly_refresh
from itemmaster.db import get_engine

E = get_engine()
F = []
SCR = os.path.dirname(os.path.abspath(__file__))


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def items(upcs):
    with E.connect() as c:
        return {r["upc"]: dict(r) for r in c.execute(text(
            "SELECT upc, description, brand, department, category, source_key FROM dbo.items WHERE upc IN :u").bindparams(
            bindparam("u", expanding=True)), {"u": list(upcs)}).mappings()}


with E.connect() as c:
    combo = c.execute(text(
        "SELECT TOP 1 combo_id, raw_department, raw_category, raw_subcategory, decided_department FROM dbo.dept_mapping_combos "
        "WHERE source_key='kehe' AND tier='auto' AND decision_state='not_reviewed' AND decided_department IS NOT NULL "
        "AND n_upcs_total BETWEEN 20 AND 200 ORDER BY combo_id")).mappings().one()
    bad = c.execute(text("SELECT TOP 50 i.upc FROM dbo.items i JOIN dbo.raw_items r ON r.upc=i.upc AND r.source_key='kehe' "
                         "WHERE i.source_key='kehe' ORDER BY i.upc")).scalars().all()
    gone = c.execute(text("SELECT TOP 30 i.upc FROM dbo.items i JOIN dbo.raw_items r ON r.upc=i.upc AND r.source_key='spins' "
                          "WHERE i.source_key='spins' AND NOT EXISTS (SELECT 1 FROM dbo.raw_items x WHERE x.upc=i.upc AND x.source_key<>'spins') "
                          "ORDER BY i.upc DESC")).scalars().all()
    saved = pd.read_sql(text("SELECT * FROM dbo.raw_items WHERE (source_key='kehe' AND upc IN :b) OR (source_key='spins' AND upc IN :g)").bindparams(
        bindparam("b", expanding=True), bindparam("g", expanding=True)), c, params={"b": bad, "g": gone})
    n_items = c.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()
saved.to_pickle(SCR + r"\raw_saved.pkl")
before_items = items(bad + gone)
before_dec = dm.combo_decision_map(E)
print(f"  target group #{combo['combo_id']} {combo['raw_category']} / {combo['raw_subcategory']} -> {combo['decided_department']}")

new_in = [f"8888000{i:05d}" for i in range(40)]
new_cat = [f"8888100{i:05d}" for i in range(10)]
with E.begin() as c:
    c.execute(text("UPDATE dbo.raw_items SET description='BAD DESCRIPTION', brand='BAD BRAND', category='BAD CAT' "
                   "WHERE source_key='kehe' AND upc IN :b").bindparams(bindparam("b", expanding=True)), {"b": bad})
    c.execute(text("DELETE FROM dbo.raw_items WHERE source_key='spins' AND upc IN :g").bindparams(bindparam("g", expanding=True)), {"g": gone})
    c.execute(text("INSERT INTO dbo.raw_items (upc, source_key, department, category, subcategory, brand, description, pack, size, uom) "
                   "VALUES (:u, 'kehe', :d, :c, :s, 'NEW BRAND', :desc, '6', '12', 'OZ')"),
              [{"u": u, "d": combo["raw_department"], "c": combo["raw_category"], "s": combo["raw_subcategory"], "desc": f"NEW ITEM {u}"} for u in new_in]
              + [{"u": u, "d": "NATURAL GROCERY", "c": "TEST NEW CATEGORY", "s": "TEST NEW SUB", "desc": f"NEW CAT ITEM {u}"} for u in new_cat])

try:
    t = time.time()
    meta = monthly_refresh.compute_draft(E, "New-only test")
    print(f"  compute {time.time() - t:.0f}s:", {k: meta[k] for k in ("added_count", "changed_count", "removed_count")}, meta["changed_by_field"])
    check(meta["added_count"] == 50, "draft: 50 new items")
    check(meta["changed_count"] >= 50, "draft: the 50 bad rows show as 'differ in files (ignored)'")
    check(meta["removed_count"] >= 30, "draft: the 30 dropped items show as 'no longer in any file (kept)'")
    with E.connect() as c:
        check(c.execute(text("SELECT COUNT(*) FROM dbo.items_staged")).scalar() == 50, "draft table holds only the 50 new rows")
    t = time.time()
    res = dm.push_merge_compute(E, "New-only test", is_admin=True)
    print(f"  push {time.time() - t:.0f}s; engine error: {res.get('engine_error')}; snapshots {res.get('safety_snapshot_id')}, {res.get('monthly_snapshot_id')}")
    with E.connect() as c:
        check(c.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar() == n_items + 50, "item master grew by exactly 50")
    after = items(bad + gone)
    diffs = [(u, before_items[u], after[u]) for u in bad if after[u] != before_items[u]]
    check(not diffs, f"the 50 existing items kept description/brand/category/Department (bad file data ignored) {diffs[:2]}")
    check(all(u in after and after[u] == before_items[u] for u in gone), "the 30 items gone from their file are still there, Department and all")
    got = items(new_in)
    check(len(got) == 40 and {v["department"] for v in got.values()} == {combo["decided_department"]},
          f"40 new items in the decided group got its Department ({ {v['department'] for v in got.values()} })")
    nc = items(new_cat)
    with E.connect() as c:
        newc = c.execute(text("SELECT combo_id, tier, decision_state, decided_department, n_upcs_total FROM dbo.dept_mapping_combos "
                              "WHERE source_key='kehe' AND raw_category='TEST NEW CATEGORY'")).mappings().all()
    check(len(nc) == 10 and len(newc) == 1, f"10 new-category items added in one new group ({[dict(x) for x in newc]})")
    grp_dept = newc[0]["decided_department"] if newc else None
    check({v["department"] for v in nc.values()} == {grp_dept}, f"their Department follows the new group ({grp_dept or 'blank — waiting for review'})")
    with E.connect() as c:
        moved = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.dept_mapping_combos cc ON cc.combo_id=cu.combo_id "
                               "WHERE cu.upc IN :b AND cc.raw_category='BAD CAT'").bindparams(bindparam("b", expanding=True)), {"b": bad}).scalar()
    check(moved == 0, "recategorized items stayed in their own groups (none moved to 'BAD CAT')")
    after_dec = dm.combo_decision_map(E)
    changed = [k for k in before_dec if after_dec.get(k) != before_dec[k]]
    print("  existing groups whose decision/tab changed:", len(changed), changed[:5])
    check(not [k for k in changed if before_dec[k][2] and not after_dec.get(k, (0, 0, None))[2]], "no decided group lost its decision")
finally:
    print("  restoring…")
    s = pd.read_pickle(SCR + r"\raw_saved.pkl")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.raw_items WHERE upc IN :n").bindparams(bindparam("n", expanding=True)), {"n": new_in + new_cat})
        c.execute(text("DELETE FROM dbo.raw_items WHERE (source_key='kehe' AND upc IN :b) OR (source_key='spins' AND upc IN :g)").bindparams(
            bindparam("b", expanding=True), bindparam("g", expanding=True)), {"b": bad, "g": gone})
    s.to_sql("raw_items", E, schema="dbo", if_exists="append", index=False)
    sid = dm.restore_snapshot(E, BASE, "AJ")
    print("  restored #51; safety", sid)
    print("  live vs #51:", dm.compare_snapshot_to_live(E, BASE))
print("FAILURES:", len(F))
for f in F:
    print(" -", f)
