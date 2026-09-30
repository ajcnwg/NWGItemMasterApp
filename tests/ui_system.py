"""End to end, on top of the old-workbook import: Crosswalk / Unmatched /
Broken Out / Pending Changes / Decided / Settings working together, with
top-bar Undo/Redo, card Undo, moves, a push, notifications — and the
import's own staged work left untouched throughout."""
from testbase import BASE, IMPORT_SNAP, KEEP
from uiharness import *
import streamlit as st_mod
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
E = get_engine(); F = []
SAFE = IMPORT_SNAP
IMPORT = {"groups": 22, "items": 59, "overrides": 0, "moves": 3}


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)


def clean(at, where):
    bad = [x.value for x in at.markdown if "Something went wrong" in x.value]
    check(len(at.exception) == 0 and not bad, f"{where}: no errors")


def open_tab(who, sub, search=None):
    st_mod.cache_data.clear()
    if search is not None:
        who.session_state["dept_shared_filter"] = {"search": search, "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(who, "Department Review", sub)
    return who


def pending(cid):
    return cid in dm.get_pending_changes(E)


def state(cid):
    with E.connect() as c:
        return c.execute(text("SELECT decision_state FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": cid}).scalar()


def confirm_topbar(at, which):
    at.button(key=f"topbar_{which}").click(); run(at, f"top-bar {which}")
    b = [x for x in at.button if (x.label or "").startswith("Confirm")]
    if b:
        b[0].click(); run(at, "confirm")


def free(tier, lo=2, hi=40):
    q = dm.get_review_queue(E, tier)
    busy = set(dm.get_pending_changes(E)) | {m["combo_id"] for m in dm.get_recent_moves(E)}
    q = q[q["suggested_department"].notna() & q["n_upcs_total"].between(lo, hi) & ~q["combo_id"].isin(busy)]
    return q.sort_values("combo_id").to_dict("records")


def label(r):
    return max([r["raw_category"] or "", r["raw_subcategory"] or ""], key=len)


try:
    dm.restore_snapshot(E, SAFE, "AJ")  # start from the old-workbook import state
    check(dm.old_workbook_import_summary(E) == IMPORT, f"start: the import is staged {dm.old_workbook_import_summary(E)}")
    cw = free("review"); um = free("unmatched", 1, 40)
    G1, G3 = cw[0], cw[1]
    G2 = um[0]
    print("  groups:", G1["combo_id"], label(G1), "|", G2["combo_id"], label(G2), "|", G3["combo_id"], label(G3))
    j = session("jason"); run(j, "Jason signs in")

    print("\n== Crosswalk → Pending Changes → top-bar Undo / Redo")
    open_tab(j, "Crosswalk", label(G1))
    c1 = G1["combo_id"]
    j.selectbox(key=f"dept_choice_review_{c1}").select(G1["suggested_department"]); run(j, "pick")
    j.button(key=f"approve_review_{c1}").click(); run(j, "Approve")
    check(pending(c1), "Jason's Crosswalk approval is in Pending Changes")
    open_tab(j, "Pending Changes", "")
    heads = [x.value for x in j.main.markdown if x.value.startswith("####")]
    caps = [x.value for x in j.caption]
    check("#### Old-workbook import" in heads and "#### Ready to push" in heads, "import section and the regular list are both there")
    check(any(cp.startswith(f"{int(G1['n_upcs_total']):,} items · Staged by Jason") and "Old workbook" not in cp for cp in caps),
          "Jason's card: items and who staged it, with no import note")
    check(f"undo_pending_{c1}" in {b.key for b in j.button}, "…and has Undo…")
    clean(j, "Pending Changes")
    confirm_topbar(j, "undo")
    check(not pending(c1), "top-bar Undo takes it back")
    confirm_topbar(j, "redo")
    check(pending(c1), "top-bar Redo puts it back")

    print("\n== Unmatched → Pending Changes → card Undo…")
    open_tab(j, "Unmatched", label(G2))
    c2 = G2["combo_id"]
    j.selectbox(key=f"dept_choice_unmatched_{c2}").select(G2["suggested_department"]); run(j, "pick")
    j.button(key=f"approve_unmatched_{c2}").click(); run(j, "Approve")
    check(pending(c2), "Jason's Unmatched approval is in Pending Changes")
    open_tab(j, "Pending Changes", "")
    check(any(" · Staged by Jason" in cp for cp in [x.value for x in j.caption]), "its card says who staged it")
    j.button(key=f"undo_pending_{c2}").click(); run(j, "Undo… on it")
    j.button(key=f"undo_to_{c2}_0").click(); run(j, "Confirm undo")
    check(not pending(c2), "card Undo… takes it back")

    print("\n== Break Out from Crosswalk → Recent moves → Undo…")
    c3 = G3["combo_id"]
    open_tab(j, "Crosswalk", label(G3))
    j.button(key=f"breakout_review_{c3}").click(); run(j, "Break Out…")
    next(b for b in j.button if b.label == "Break it out").click(); run(j, "Break it out")
    check(state(c3) == "broken_out", "it's in Broken Out")
    open_tab(j, "Broken Out", label(G3))
    check(any(label(G3) in x.value for x in j.main.markdown), "Broken Out tab lists it")
    open_tab(j, "Pending Changes", "")
    heads = [x.value for x in j.main.markdown if x.value.startswith("####")]
    check(heads[0] == "#### Recent moves" and f"undo_recent_{c3}" in {b.key for b in j.button},
          "the regular Recent moves shows it first, apart from the import's moves")
    j.button(key=f"undo_recent_{c3}").click(); run(j, "Undo… on the move")
    j.button(key=f"undo_to_{c3}_1").click(); run(j, "Confirm undo")
    check(state(c3) == "not_reviewed", "Undo… sends it back to Crosswalk")
    check(dm.old_workbook_import_summary(E) == IMPORT, "the import is untouched so far")

    print("\n== AJ pushes only Jason's group → Decided → Send Back")
    a = session("aj"); run(a, "AJ signs in")
    for cid in dm.get_pending_changes(E):
        if cid != c1:
            a.session_state[f"dept_pending_include_combo_{cid}"] = False
    for cid in {c["combo_id"] for c in dm.get_pending_upc_changes(E).values()}:
        a.session_state[f"dept_pending_include_upc_group_{cid}"] = False
    open_tab(a, "Pending Changes", "")
    check(a.button(key="push_pending_changes").disabled, "Push waits while the import's question is open")
    q = dm.list_import_choices(E)[0]
    n_opt = len(__import__("json").loads(q["options_json"]))
    a.button(key=f"choice_use_{q['choice_id']}_{n_opt - 1}").click(); run(a, "answer it: leave as the app has it")
    push = a.button(key="push_pending_changes")
    check(push.label == f"Push {int(G1['n_upcs_total']):,} Included Item(s) to the Database", f"only Jason's group is included ({push.label})")
    a.checkbox(key="confirm_push_dept_changes").check(); run(a, "tick")
    a.button(key="push_pending_changes").click(); run(a, "Push")
    check(state(c1) == "decided", "Jason's group is decided")
    check(dm.old_workbook_import_summary(E) == IMPORT, "the import's staged work is still all there")
    open_tab(a, "Decided", label(G1))
    caps = [x.value for x in a.caption]
    check(not any("Old-workbook" in cp for cp in caps) and f"revert_decided_{c1}" in {b.key for b in a.button},
          "Decided card: no import note, and Send Back (to Crosswalk) is there")
    clean(a, "Decided")
    a.button(key=f"revert_decided_{c1}").click(); run(a, "Send Back to Crosswalk")
    next(b for b in a.button if b.label == "Send it back").click(); run(a, "Send it back")
    check(state(c1) == "not_reviewed", "sent back to Crosswalk")

    print("\n== Notifications")
    j2 = session("jason"); j2.session_state["_notif_since"] = {"Jason": dm.get_last_seen(E, "Jason")}
    run(j2, "Jason back")
    side = " ".join(x.value for x in j2.sidebar.markdown) + " ".join(x.value for x in j2.sidebar.caption)
    check(label(G1).lower() in side.lower(), "Jason is told about his group being pushed / sent back")

    print("\n== Settings")
    open_tab(a, "Settings")
    clean(a, "Settings")
    check(any(m.value == "#### Excel department workbook" for m in a.main.markdown), "admin: the Excel workbook section is in Settings")
    a.text_input(key="new_department_input").input("ZZ TEST DEPT"); run(a, "type")
    a.button(key="add_department_btn").click(); run(a, "Add")
    check("ZZ TEST DEPT" in dm.get_departments(E).iloc[:, 0].tolist(), "department added")
    open_tab(a, "Crosswalk", label(G1))
    opts = a.selectbox(key=f"dept_choice_review_{c1}").options
    check("ZZ TEST DEPT" in opts, "it's offered on Crosswalk cards right away")
    open_tab(a, "Settings")
    a.selectbox(key="remove_department_select").select("ZZ TEST DEPT"); run(a, "pick")
    a.button(key="remove_department_btn").click(); run(a, "Remove")
    check("ZZ TEST DEPT" not in dm.get_departments(E).iloc[:, 0].tolist(), "department removed")

    print("\n== Every tab, for every role")
    for who in ("aj", "jason", "viewer"):
        s = session(who); run(s, f"{who} load")
        for sub in ("Crosswalk", "Unmatched", "Broken Out", "Pending Changes", "Decided", "Settings"):
            if who == "viewer":
                break
            open_tab(s, sub, "")
            clean(s, f"{who} / {sub}")
    check(dm.old_workbook_import_summary(E) == IMPORT, f"end: the import is exactly as it was {dm.old_workbook_import_summary(E)}")
finally:
    print("\n  restoring #%d…" % SAFE); dm.restore_snapshot(E, SAFE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.user_workspace WHERE username IN ('Jason', 'Kristi', 'Eric')"))
    print("  vs #0:", dm.compare_snapshot_to_live(E, BASE), dm.old_workbook_import_summary(E))
print("FAILURES:", len(F))
