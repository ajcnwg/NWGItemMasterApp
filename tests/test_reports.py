import os
"""New-item reports (draft preview + after push) and the staged re-check, on live data, then restored."""
from testbase import BASE, IMPORT_SNAP, KEEP
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uiharness import *  # noqa
import pandas as pd
import streamlit as st_mod
from sqlalchemy import text, bindparam
from itemmaster import dept_mapping as dm
from itemmaster import monthly_refresh
from itemmaster.db import get_engine

E = get_engine()
F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


with E.connect() as c:
    dec = c.execute(text("SELECT TOP 1 combo_id, raw_department, raw_category, raw_subcategory, decided_department FROM dbo.dept_mapping_combos "
                         "WHERE source_key='kehe' AND tier='auto' AND decision_state='not_reviewed' AND decided_department IS NOT NULL "
                         "AND n_upcs_total BETWEEN 20 AND 200 ORDER BY combo_id")).mappings().one()
    cw = c.execute(text("SELECT TOP 1 combo_id, raw_department, raw_category, raw_subcategory FROM dbo.dept_mapping_combos "
                        "WHERE source_key='kehe' AND tier='review' AND decision_state='not_reviewed' AND decided_department IS NULL ORDER BY combo_id")).mappings().one()
new_dec = [f"8888200{i:05d}" for i in range(40)]
new_cw = [f"8888300{i:05d}" for i in range(5)]
new_cat = [f"8888400{i:05d}" for i in range(10)]
ALL = new_dec + new_cw + new_cat
rows = ([{"u": u, "d": dec["raw_department"], "c": dec["raw_category"], "s": dec["raw_subcategory"]} for u in new_dec]
        + [{"u": u, "d": cw["raw_department"], "c": cw["raw_category"], "s": cw["raw_subcategory"]} for u in new_cw]
        + [{"u": u, "d": "ZZ TEST DEPT", "c": "ZZ TEST CATEGORY", "s": "ZZ TEST SUB"} for u in new_cat])
with E.begin() as c:
    c.execute(text("INSERT INTO dbo.raw_items (upc, source_key, department, category, subcategory, brand, description) "
                   "VALUES (:u, 'kehe', :d, :c, :s, 'TEST BRAND', 'TEST NEW ITEM')"), rows)
corrupt = []
try:
    meta = monthly_refresh.compute_draft(E, "Report test")
    check(meta["added_count"] == 55, f"draft adds 55 ({meta['added_count']})")
    pv = dm.draft_new_items(E).set_index("UPC")
    check(set(pv.loc[new_dec, "Decision"]) == {"Auto-decided"} and pv.loc[new_dec[0], "How"].endswith("Department filled in on push"),
          f"preview: 40 go into a decided group ({pv.loc[new_dec[0], 'How'][:80]}…)")
    check(set(pv.loc[new_cw, "Decision"]) == {"Waiting in Crosswalk"}, f"preview: 5 go into a Crosswalk group ({set(pv.loc[new_cw, 'Decision'])})")
    check(set(pv.loc[new_cat, "Decision"]) == {"New group"}, f"preview: 10 form a new group ({set(pv.loc[new_cat, 'Decision'])})")
    st_mod.cache_data.clear()
    aj = session("aj"); run(aj, "load aj"); goto(aj, "Merge")
    check(any("The 55 new item(s)" in e.label for e in aj.expander), "Merge tab shows the draft's 55 new items")
    check(any("55 item(s)" in m.value for m in aj.markdown), "…with the counts summary")

    res = dm.push_merge_compute(E, "Report test", is_admin=True)
    print(f"  pushed; engine error {res.get('engine_error')}")
    mid = int(dm.list_merges_with_additions(E).iloc[0]["id"])
    ad = dm.merge_added_items(E, mid).set_index("UPC")
    check(len(ad) == 55, f"report lists the 55 added items ({len(ad)})")
    check(set(ad.loc[new_dec, "Department"]) == {dec["decided_department"]} and set(ad.loc[new_dec, "Decision"]) == {"Auto-decided"},
          f"40 got {dec['decided_department']} from their group")
    check(set(ad.loc[new_cw, "Decision"]) == {"Waiting in Crosswalk"} and ad.loc[new_cw, "Department"].isna().all(),
          "5 have no Department, waiting in Crosswalk")
    print("  new-category items now:", set(ad.loc[new_cat, "Decision"]), set(ad.loc[new_cat, "Department"]))
    check(ad.loc[new_dec[0], "Brand"] == "TEST BRAND" and ad.loc[new_dec[0], "Description"] == "TEST NEW ITEM", "report carries the row data")

    st_mod.cache_data.clear()
    aj = session("aj"); run(aj, "reload"); goto(aj, "Merge")
    check(any((x.key or "") == "added_merge_pick" for x in aj.selectbox), "past-Merges report renders")
    grp = [x for x in aj.selectbox if (x.key or "").endswith("_grp")]
    if grp:
        want = "KEHE — " + " / ".join(x for x in (cw["raw_department"], cw["raw_category"], cw["raw_subcategory"]) if x)
        grp[0].set_value(want); run(aj, "pick waiting group")
        next(b for b in aj.button if (b.key or "").endswith("_open")).click(); run(aj, "Open in Crosswalk")
        check(aj.session_state["active_tab"] == "Department Review" and aj.session_state["dept_review_subtab"] == "Crosswalk",
              "jump opened Department Review / Crosswalk")
        check(any(b.key == f"approve_review_{cw['combo_id']}" for b in aj.button), "…with that group on screen")

    # staged re-check: break 5 departments, check, apply, undo, apply again
    with E.connect() as c:
        corrupt = c.execute(text("SELECT TOP 5 upc FROM dbo.items WHERE upc IN :u").bindparams(bindparam("u", expanding=True)),
                            {"u": new_dec}).scalars().all()
    with E.begin() as c:
        c.execute(text("UPDATE dbo.items SET department='WRONG' WHERE upc IN :u").bindparams(bindparam("u", expanding=True)), {"u": corrupt})
    st_mod.cache_data.clear()
    aj = session("aj"); run(aj, "reload"); goto(aj, "Merge")
    aj.button(key="rules_check_btn").click(); run(aj, "Check now")
    plan = aj.session_state["_rules_plan"]
    print(plan[["UPC", "Field", "Now", "Will be", "Why"]].head(2).to_string())
    check(len(plan) == 5 and set(plan["Now"]) == {"WRONG"} and set(plan["Will be"]) == {dec["decided_department"]},
          "plan: exactly the 5 items, WRONG -> group's Department")
    check(all("Auto-decided" in w for w in plan["Why"]), "plan says which decision is behind each change")
    with E.connect() as c:
        still = c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE department='WRONG'")).scalar()
    check(still == 5, "nothing written until Apply")
    aj.button(key="rules_fix_btn").click(); run(aj, "Apply")
    with E.connect() as c:
        check(c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE department='WRONG'")).scalar() == 0, "applied")
    aj.button(key="rules_undo_btn").click(); run(aj, "Undo that fix")
    with E.connect() as c:
        check(c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE department='WRONG'")).scalar() == 5, "undo put the 5 back exactly")
    # stale plan is refused
    aj.button(key="rules_check_btn").click(); run(aj, "Check again")
    with E.begin() as c:
        c.execute(text("UPDATE dbo.items SET department='WRONG2' WHERE upc = :u"), {"u": corrupt[0]})
    aj.button(key="rules_fix_btn").click(); run(aj, "Apply a plan that's gone stale")
    with E.connect() as c:
        check(c.execute(text("SELECT COUNT(*) FROM dbo.items WHERE department LIKE 'WRONG%'")).scalar() == 5, "stale plan refused, nothing written")
finally:
    print("  restoring…")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.raw_items WHERE upc IN :u").bindparams(bindparam("u", expanding=True)), {"u": ALL})
    dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.merge_added_items WHERE merge_id NOT IN (SELECT id FROM dbo.merge_log)"))
        for sid in c.execute(text("SELECT snapshot_id FROM dbo.dept_mapping_snapshots WHERE snapshot_id > 57 AND kind IN ('safety_merge','monthly')")).scalars().all():
            pass
    print("  live vs #51:", dm.compare_snapshot_to_live(E, BASE))
print("FAILURES:", len(F))
for f in F:
    print(" -", f)
