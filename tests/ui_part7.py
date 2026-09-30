"""Part 7: the Undo… popup with several points (stay / back past a Break
Out), and the admin popup for a Broken Out group (whole-group override,
unlock), each followed by top-bar undo/redo — all exact."""
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine()
FAIL = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def keyed(at, prefix):
    return [b for b in at.button if (b.key or "").startswith(prefix)]


def wb_ver(at, key):
    return at.session_state[f"wb_{key}"]["ver"] if f"wb_{key}" in at.session_state else 0


def state(cid):
    with E.connect() as c:
        return c.execute(text("SELECT decision_state FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": cid}).scalar()


def staged(cid):
    return {u: c["department"] for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == cid}


q = dm.get_review_queue(E, "review")
row = q[q["n_upcs_total"].between(4, 20)].sort_values("n_upcs_total").iloc[1]
cid = int(row.combo_id); lab = max([row.raw_category or "", row.raw_subcategory or ""], key=len)
orig = dm.get_combo_snapshot(E, cid)
print(f"  group {cid} {lab} ({int(row.n_upcs_total)} items)")

jason = session("jason"); run(jason, "load jason")
jason.session_state["dept_shared_filter"] = {"search": lab, "sort_label": None, "sort_desc": True, "page_size": 25}
goto(jason, "Department Review", "Crosswalk")
jason.button(key=f"breakout_review_{cid}").click(); run(jason, "Break Out")
next(b for b in jason.button if b.label in ("Start blank", "Break it out")).click(); run(jason, "confirm (blank)")
goto(jason, "Department Review", "Broken Out")
jason.button(key=f"claim_broken_out_{cid}").click(); run(jason, "claim")
k = f"bo_{cid}"; v = wb_ver(jason, k)
jason.selectbox(key=f"wb_pick_{k}_{v}").select("GROCERY")
jason.button(key=f"wb_setall_{k}_{v}").click(); run(jason, "set all GROCERY")
v = wb_ver(jason, k)
jason.button(key=f"bo_stage_all_{cid}").click(); run(jason, "Stage all in this group")
check(len(staged(cid)) == int(row.n_upcs_total), f"all {int(row.n_upcs_total)} items staged")

goto(jason, "Department Review", "Pending Changes")
jason.button(key=f"undo_pending_upc_combo_{cid}").click(); run(jason, "open Undo…")
pts = keyed(jason, f"undo_to_{cid}_")
names = [m.value for m in jason.markdown if m.value.startswith("**") and m.value.endswith("**")]
print("  points:", [b.key for b in pts], names[-3:])
check(len(pts) == 2, "two points: stay in Broken Out, or back to Crosswalk")
next(b for b in pts if b.key.endswith("_1")).click(); run(jason, "Confirm undo -> back to Crosswalk")
check(state(cid) == "not_reviewed" and not staged(cid), "back in Crosswalk, nothing staged")
check(dm._redo_state_key(dm.get_combo_snapshot(E, cid)) == dm._redo_state_key(orig), "exactly as it started")
check(dm.peek_undo_redo(E, "Jason")["redo"] is None and dm.peek_undo_redo(E, "Jason")["undo"]["description"].startswith("Undid"), "the card Undo… is itself a step on the top-bar Undo (nothing to redo)")
jason.button(key="topbar_undo").click(); run(jason, "top-bar undo of the popup undo")
next(b for b in jason.button if b.label == "Confirm undo").click(); run(jason, "confirm")
check(state(cid) == "broken_out" and len(staged(cid)) == int(row.n_upcs_total), "Broken Out with every staged item back")

# admin popup on the Broken Out group: whole-group override, then unlock
aj = session("aj"); run(aj, "load aj")
aj.session_state["dept_shared_filter"] = {"search": lab, "sort_label": None, "sort_desc": True, "page_size": 25}
goto(aj, "Department Review", "Pending Changes")
b = keyed(aj, f"admin_override_btn_group_ready_{cid}")
check(len(b) == 1, "admin sees the override button on the group")
b[0].click(); run(aj, "open admin popup")
sel = [s for s in aj.selectbox if s.key == f"admin_override_dialog_group_dept_{cid}"]
check(len(sel) == 1, "popup offers a whole-group override")
sel[0].select("FROZEN"); run(aj, "pick FROZEN")
btn = [x for x in aj.button if "whole group" in (x.label or "").lower() or (x.label or "").startswith("Override all")]
print("  popup buttons:", [x.label for x in aj.button if x.label and ("verride" in x.label or "nlock" in x.label or x.label == "Cancel")])
btn[0].click(); run(aj, f"click {btn[0].label}")
st_ = staged(cid)
check(set(st_.values()) == {"FROZEN"}, f"every item now FROZEN by admin override ({set(st_.values())})")
with E.connect() as c:
    locked = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id=:c AND overridden_by='AJ'"), {"c": cid}).scalar()
check(locked == int(row.n_upcs_total), "all locked by AJ")
# Jason can't change a locked item: his grid change is refused
jason = session("jason"); run(jason, "reload jason")
jason.session_state["dept_shared_filter"] = {"search": lab, "sort_label": None, "sort_desc": True, "page_size": 25}
goto(jason, "Department Review", "Pending Changes")
k = f"pc_ready_{cid}"; v = wb_ver(jason, k)
jason.selectbox(key=f"wb_pick_{k}_{v}").select("DAIRY")
jason.button(key=f"wb_setall_{k}_{v}").click(); run(jason, "jason sets DAIRY")
v = wb_ver(jason, k)
jason.button(key=f"wb_stage_{k}_{v}").click(); run(jason, "jason Apply changes")
print("  toast:", [t.value for t in jason.toast])
check(set(staged(cid).values()) == {"FROZEN"}, "locked items unchanged by Jason")
# aj undoes the override from the top bar, then redoes it
aj.button(key="topbar_undo").click(); run(aj, "aj undo override")
next(x for x in aj.button if x.label == "Confirm undo").click(); run(aj, "confirm")
check(set(staged(cid).values()) == {"GROCERY"}, "override undone: Jason's GROCERY back")
aj.button(key="topbar_redo").click(); run(aj, "aj redo override")
next(x for x in aj.button if x.label == "Confirm redo").click(); run(aj, "confirm")
check(set(staged(cid).values()) == {"FROZEN"}, "override redone")
# unlock the whole group from the popup
goto(aj, "Department Review", "Pending Changes")
keyed(aj, f"admin_override_btn_group_ready_{cid}")[0].click(); run(aj, "open admin popup")
un = [x for x in aj.button if "nlock" in (x.label or "")]
print("  unlock buttons:", [x.label for x in un])
if un:
    un[-1].click(); run(aj, f"click {un[-1].label}")
with E.connect() as c:
    locked = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id=:c AND overridden_by IS NOT NULL"), {"c": cid}).scalar()
check(locked == 0, "whole group unlocked")

# clean up: back to exactly where it started
dm.release_broken_out_claim(E, cid, "AJ", is_admin=True)
with E.begin() as c:
    for t in ("dept_mapping_recent_moves",) + dm.STAGED_TABLES:
        c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": cid})
    dm._restore_combo_snapshot(c, cid, orig, "test")
check(dm._redo_state_key(dm.get_combo_snapshot(E, cid)) == dm._redo_state_key(orig), "restored")
print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
