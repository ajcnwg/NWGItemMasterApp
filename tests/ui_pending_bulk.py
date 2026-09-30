from testbase import BASE, IMPORT_SNAP, KEEP
import time
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text, bindparam
E = get_engine(); F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
dm.restore_snapshot(E, BASE, "AJ")
with E.connect() as c:  # 120 staged UPC overrides to work with
    rows = c.execute(text("SELECT TOP 120 upc, description, category, subcategory, brand, pack, size, uom, source_key FROM dbo.items "
                          "WHERE department = 'GROCERY' AND upc NOT IN (SELECT upc FROM dbo.manual_overrides) ORDER BY upc")).mappings().all()
dm.save_item_master_pending_bulk(E, {r["upc"]: {**dict(r), "change_type": "edit",
                                                "department": "BAKERY" if i < 60 else "GENERAL MERCHANDISE"}
                                     for i, r in enumerate(rows)}, "Jason")
pend = dm.get_item_master_pending(E)
pick = sorted(u for u, c in pend.items() if c["department"] == "BAKERY")[:3] + sorted(u for u, c in pend.items() if c["department"] == "GENERAL MERCHANDISE")[:2]
try:
    a = session("aj"); run(a, "load")
    t = time.time(); goto(a, "Pending Changes"); took = time.time() - t
    check(took < 20, f"Pending Changes opens quickly with {len(pend):,} staged ({took:.1f}s)")
    check(sum(1 for c in a.checkbox if c.key and c.key.startswith("im_pending_include_")) == 50, "50 cards per page")
    a.button(key="im_pending_select_none").click(); run(a, "leave out all")
    for u in pick:
        a.session_state[f"im_pending_include_{u}"] = True
    run(a, "include 5")
    w = [x.value for x in a.warning if "included change(s) live" in x.value]
    check(w and "**5** included" in w[0], f"5 included ({w[0][:80] if w else ''})")
    a.checkbox(key="confirm_push_item_master").check(); run(a, "tick")
    t = time.time(); a.button(key="push_item_master_pending").click(); run(a, "push 5"); print(f"   push {time.time()-t:.1f}s")
    with E.connect() as c:
        live = dict(c.execute(text("SELECT upc, department FROM dbo.items WHERE upc IN :u").bindparams(bindparam("u", expanding=True)), {"u": pick}).all())
        pins = {r[0]: r[1:] for r in c.execute(text("SELECT upc, department, brand, description, category FROM dbo.manual_overrides WHERE upc IN :u").bindparams(bindparam("u", expanding=True)), {"u": pick}).all()}
    check(live == {u: pend[u]["department"] for u in pick}, "live item master has the new Departments")
    check(set(pins) == set(pick) and all(v[0] == pend[u]["department"] and v[1] is None and v[2] is None and v[3] is None for u, v in pins.items()),
          "overrides pin only Department")
    left = dm.get_item_master_pending(E)
    check(len(left) == len(pend) - 5 and not set(pick) & set(left), "only the 5 left Pending; the rest still staged")
finally:
    print("  restoring #0…"); dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.user_workspace WHERE username='AJ'"))
    print("  live vs #0:", dm.compare_snapshot_to_live(E, BASE), len(dm.get_item_master_pending(E)))
print("FAILURES:", len(F))
