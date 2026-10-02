"""Round 2 — a group's whole journey, by several people:
Crosswalk ⇄ Broken Out ⇄ Decided with each card's Undo…, votes from four
accounts, editor push approvals vs admin push, pushes of new / changed /
brought-back decisions, how the top-bar Undo / Redo behave around a push
(they never reach behind one), filters, notifications, and the Activity
report — from the Item Master Baseline (#0)."""
from uiharness import *
from testbase import BASE
import streamlit as st_mod
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine(); F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def clean(at, where):
    bad = [x.value for x in at.markdown if "Something went wrong" in x.value]
    check(len(at.exception) == 0 and not bad, f"{where}: no errors")


def tab(who, sub, search=""):
    st_mod.cache_data.clear()
    who.session_state["dept_shared_filter"] = {"search": search, "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(who, "Department Review", sub)
    return who


def state(cid):
    with E.connect() as c:
        return c.execute(text("SELECT decision_state, decided_department FROM dbo.dept_mapping_combos WHERE combo_id=:c"),
                         {"c": cid}).one()


def label(r):
    bits = [b for b in (r["raw_department"], r["raw_category"], r["raw_subcategory"]) if b]
    return " / ".join(bits)


def free(tier, lo=2, hi=40, n=4):
    q = dm.get_review_queue(E, tier)
    busy = set(dm.get_pending_changes(E)) | {m["combo_id"] for m in dm.get_recent_moves(E)}
    q = q[q["suggested_department"].notna() & q["n_upcs_total"].between(lo, hi) & ~q["combo_id"].isin(busy)]
    # a search that finds exactly this group
    rows = [r for r in q.sort_values("combo_id").to_dict("records")]
    return rows[:n]


def topbar(at, which, confirm=True):
    at.button(key=f"topbar_{which}").click(); run(at, f"top-bar {which}")
    b = [x for x in at.button if (x.label or "").startswith("Confirm")]
    info = [x.value for x in at.info]
    if b and confirm:
        b[0].click(); run(at, "confirm")
    return bool(b), info


def push_only(a, include_ids):
    for cid in dm.get_pending_changes(E):
        a.session_state[f"dept_pending_include_combo_{cid}"] = cid in include_ids
    for cid in {c["combo_id"] for c in dm.get_pending_upc_changes(E).values()}:
        a.session_state[f"dept_pending_include_upc_group_{cid}"] = cid in include_ids
    tab(a, "Pending Changes")
    a.checkbox(key="confirm_push_dept_changes").check(); run(a, "tick")
    a.button(key="push_pending_changes").click(); run(a, "Push")


try:
    dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.activity_log"))
    G = free("review")
    A, B, C, D = G[0], G[1], G[2], G[3]
    print("  groups:", [(g["combo_id"], label(g)) for g in G])
    j, k, e, a = session("jason"), session("kristi"), session("eric"), session("aj")
    for s_, n in ((j, "Jason"), (k, "Kristi"), (e, "Eric"), (a, "AJ")):
        run(s_, f"{n} signs in")

    print("\n== 1. Permissions")
    tabs_of = lambda at: at.radio(key="active_tab").options
    check("Activity" in tabs_of(a) and "Sources" in tabs_of(a), f"admin tabs: {tabs_of(a)}")
    check(tabs_of(j) == ["Item Master", "Department Review", "Add Item", "Delete Item", "Upload Reports", "UPC Overrides", "Pending Changes", "Activity"],
          f"editor tabs: {tabs_of(j)}")
    v = session("viewer"); run(v, "viewer")
    check(tabs_of(v) == ["Item Master"], f"viewer tabs: {tabs_of(v)}")
    tab(j, "Settings")
    heads = [m.value for m in j.main.markdown if m.value.startswith("####")]
    check(heads[0] == "#### Request a change" and "add_department_btn" not in {b.key for b in j.button},
          "editor Settings: request form, no admin buttons")

    print("\n== 2. Crosswalk filters")
    tab(j, "Crosswalk")
    src = A["source_key"]
    j.multiselect(key="dept_review_review_facet_source_key").select(src); run(j, f"Source = {src}")
    cards = [b.key for b in j.button if (b.key or "").startswith("approve_review_")]
    check(cards and all(dm.get_review_queue(E, "review").set_index("combo_id").loc[int(k_.rsplit("_", 1)[1]), "source_key"] == src
                        for k_ in cards), f"Source filter: only {src.upper()} groups ({len(cards)})")
    j.button(key="dept_review_review_clear_filters").click(); run(j, "Clear filters")
    check(j.multiselect(key="dept_review_review_facet_source_key").value == [], "Clear filters clears the Source pick too")
    clean(j, "Crosswalk")

    print("\n== 3. Votes: Jason approves, Kristi disagrees, Eric agrees, Kristi comes round")
    ca = A["combo_id"]; dept_a = A["suggested_department"]
    other = next(d for d in dm.get_departments(E).iloc[:, 0] if d != dept_a)
    tab(j, "Crosswalk", label(A))
    j.selectbox(key=f"dept_choice_review_{ca}").select(dept_a); run(j, "pick")
    j.button(key=f"approve_review_{ca}").click(); run(j, "Jason approves")
    check(dm.get_pending_changes(E).get(ca, {}).get("department") == dept_a, f"staged as {dept_a}")
    tab(k, "Pending Changes")
    k.selectbox(key=f"dept_pending_suggest_{ca}").select(other); run(k, "pick")
    k.button(key=f"dept_pending_update_{ca}").click(); run(k, f"Kristi suggests {other}")
    check(ca in dm.get_combo_suggestions(E), "now disputed → Needs agreement")
    tab(e, "Pending Changes")
    check(f"agree_combo_{ca}_{dept_a}" in {b.key for b in e.button}, "Eric sees both options to agree with")
    e.button(key=f"agree_combo_{ca}_{dept_a}").click(); run(e, f"Eric agrees with {dept_a}")
    check(ca in dm.get_combo_suggestions(E), "still disputed — agreeing doesn't force it")
    tab(k, "Pending Changes")
    k.button(key=f"agree_combo_{ca}_{dept_a}").click(); run(k, f"Kristi switches to {dept_a}")
    check(ca not in dm.get_combo_suggestions(E) and dm.get_pending_changes(E).get(ca, {}).get("department") == dept_a,
          "resolved — back in Ready to push")
    clean(k, "Pending Changes")

    print("\n== 4. Editor push needs 2 approvals; admin pushes alone")
    tab(j, "Pending Changes")
    check(j.button(key="push_pending_changes").disabled, "Jason (editor) can't push yet")
    j.button(key="approve_dept_push").click(); run(j, "Jason approves the batch")
    tab(k, "Pending Changes"); k.button(key="approve_dept_push").click(); run(k, "Kristi approves the batch")
    tab(j, "Pending Changes"); j.checkbox(key="confirm_push_dept_changes").check(); run(j, "tick")
    check(not j.button(key="push_pending_changes").disabled, "2 approvals: Jason can push now")
    j.button(key="push_pending_changes").click(); run(j, "Jason pushes")
    check(state(ca) == ("decided", dept_a), f"pushed: decided as {dept_a} {state(ca)}")

    print("\n== 5. Top-bar Undo / Redo never reach behind a push")
    peek = dm.peek_undo_redo(E, "Jason")
    check(peek["undo"] is None or peek["undo"]["combo_id"] != ca, f"Jason's Undo no longer offers the pushed approval ({peek['undo']})")
    peek = dm.peek_undo_redo(E, "Kristi")
    check(peek["undo"] is None or peek["undo"]["combo_id"] != ca, "…nor Kristi's vote on it")
    tab(j, "Decided", label(A))
    check(f"undo_card_dec_{ca}" not in {x.key for x in j.button}, "Decided card: no Undo… (nothing unpushed to take back)")

    print("\n== 6. Decided → change Department → push; then Send Back → Undo… → push again")
    tab(a, "Decided", label(A))
    a.selectbox(key=f"decided_change_dept_{ca}").select(other); run(a, "pick new Department")
    a.button(key=f"decided_change_dept_btn_{ca}").click(); run(a, "Stage change")
    check(dm.get_pending_changes(E).get(ca, {}).get("department") == other and state(ca) == ("decided", dept_a),
          "staged; the live decision stays until pushed")
    ok, _ = topbar(a, "undo")
    check(ca not in dm.get_pending_changes(E), "AJ's top-bar Undo takes the staged change back")
    ok, _ = topbar(a, "redo")
    check(dm.get_pending_changes(E).get(ca, {}).get("department") == other, "…and Redo puts it back")
    push_only(a, {ca})
    check(state(ca) == ("decided", other), f"changed decision pushed {state(ca)}")
    ok, info = topbar(a, "undo", confirm=False)
    check(not ok or dm.peek_undo_redo(E, "AJ")["undo"] is None or dm.peek_undo_redo(E, "AJ")["undo"]["combo_id"] != ca,
          "after the push, top-bar Undo doesn't offer it")
    cl = [b for b in a.button if b.label == "Close"]
    if cl:
        cl[0].click(); run(a, "close the Undo popup")
    tab(a, "Decided", label(A))
    a.button(key=f"revert_decided_{ca}").click(); run(a, "Send Back")
    btn = [x for x in a.button if x.label == "Send it back"]
    if btn:
        btn[0].click(); run(a, "Send it back")
    check(state(ca)[0] == "not_reviewed", "sent back to Crosswalk")
    tab(a, "Crosswalk", label(A))
    check(not a.button(key=f"undo_card_review_{ca}").disabled, "its Crosswalk card now has Undo… (the move is unpushed)")
    a.button(key=f"undo_card_review_{ca}").click(); run(a, "Undo…")
    a.button(key=f"undo_to_{ca}_1").click(); run(a, "back to before the Send Back")
    check(state(ca) == ("decided", other), f"Undo… brings it back to Decided as {other}")
    tab(a, "Decided", label(A))
    a.button(key=f"revert_decided_{ca}").click(); run(a, "Send Back again")
    btn = [x for x in a.button if x.label == "Send it back"]
    if btn:
        btn[0].click(); run(a, "Send it back")
    tab(j, "Crosswalk", label(A))
    j.selectbox(key=f"dept_choice_review_{ca}").select(dept_a); run(j, "pick")
    j.button(key=f"approve_review_{ca}").click(); run(j, "Jason re-approves the brought-back group")
    push_only(a, {ca})
    check(state(ca) == ("decided", dept_a), f"brought-back group re-decided and pushed {state(ca)}")

    print("\n== 7. Crosswalk → Break Out → Broken Out card Undo… → Crosswalk")
    cb = B["combo_id"]
    tab(j, "Crosswalk", label(B))
    j.button(key=f"breakout_review_{cb}").click(); run(j, "Break Out…")
    nxt = [x for x in j.button if x.label == "Break it out"]
    if nxt:
        nxt[0].click(); run(j, "Break it out")
    check(state(cb)[0] == "broken_out", "in Broken Out")
    tab(j, "Broken Out", label(B))
    check(not j.button(key=f"undo_card_bo_{cb}").disabled, "Broken Out card: Undo… available")
    j.multiselect(key="broken_out_facet_worked_by").select("Nobody"); run(j, "filter: not being worked on")
    check(f"claim_broken_out_{cb}" in {b.key for b in j.button}, "'Being worked on by: Nobody' shows it")
    j.button(key=f"claim_broken_out_{cb}").click(); run(j, "Work on this group")
    j.multiselect(key="broken_out_facet_worked_by").set_value(["Mine"]); run(j, "filter: mine")
    check(f"release_claim_{cb}" in {b.key for b in j.button}, "'Mine' shows the group Jason is working on")
    j.button(key=f"undo_card_bo_{cb}").click(); run(j, "Undo…")
    j.button(key=f"undo_to_{cb}_1").click(); run(j, "undo the Break Out")
    check(state(cb)[0] == "not_reviewed", "back in Crosswalk")
    clean(j, "Broken Out")

    print("\n== 8. Push everything left from several people, then Undo/Redo")
    cc = C["combo_id"]
    tab(k, "Crosswalk", label(C))
    k.selectbox(key=f"dept_choice_review_{cc}").select(C["suggested_department"]); run(k, "pick")
    k.button(key=f"approve_review_{cc}").click(); run(k, "Kristi approves C")
    cd = D["combo_id"]
    tab(e, "Crosswalk", label(D))
    e.selectbox(key=f"dept_choice_review_{cd}").select(D["suggested_department"]); run(e, "pick")
    e.button(key=f"approve_review_{cd}").click(); run(e, "Eric approves D")
    ok, _ = topbar(e, "undo")
    check(cd not in dm.get_pending_changes(E), "Eric undoes D before the push")
    ok, _ = topbar(e, "redo")
    check(cd in dm.get_pending_changes(E), "…and redoes it")
    push_only(a, {cc, cd})
    check(state(cc)[0] == "decided" and state(cd)[0] == "decided", "C and D pushed")
    for who in ("Kristi", "Eric"):
        pk = dm.peek_undo_redo(E, who)
        check(pk["undo"] is None or pk["undo"]["combo_id"] not in (cc, cd), f"{who}'s Undo doesn't reach the pushed group")
        check(pk["redo"] is None or pk["redo"]["combo_id"] not in (cc, cd), f"{who}'s Redo neither")

    print("\n== 9. Notifications: clear, rolled up")
    j2 = session("jason"); j2.session_state["_notif_since"] = {"Jason": dm.get_last_seen(E, "Jason") - __import__("datetime").timedelta(hours=1)}
    run(j2, "Jason back")
    side = " ".join(x.value for x in j2.sidebar.markdown) + " " + " ".join(x.value for x in j2.sidebar.caption)
    check("pushed your decision" in side, "Jason hears his decision was pushed")
    notes = dm.get_notifications(E, "Jason", dm.get_last_seen(E, "Jason") - __import__("datetime").timedelta(hours=1))
    print("   Jason's notifications:", [(n["kind"], n["detail"][:60]) for n in notes["action"] + notes["updates"]])
    clean(j2, "sidebar")

    print("\n== 10. Activity report")
    tab(a, "Settings")
    goto(a, "Activity")
    clean(a, "Activity")
    log = dm.list_activity(E)
    people = set(log["actor"])
    check({"Jason", "Kristi", "Eric", "AJ"} <= people, f"everyone's work is in the log ({sorted(people)})")
    areas = set(log["area"])
    check({"Department Review", "Pushed live", "Undo / Redo"} <= areas, f"areas recorded: {sorted(areas)}")
    j_log = log[log["actor"] == "Jason"]
    check(any("Approved as" in x for x in j_log["action"]) and any(label(A) in (t or "") for t in j_log["target"]),
          "Jason's approvals are listed with the group")
    check(any(log[(log["actor"] == "AJ") & (log["area"] == "Pushed live")]["n_items"].fillna(0) > 0), "AJ's pushes list items pushed")
    a.multiselect(key="act_people").select("Jason"); run(a, "only Jason")
    tables = [d.value for d in a.dataframe]
    check(any("Jason" in t.index for t in tables if hasattr(t, "index")), "summary shows Jason")
    clean(a, "Activity (filtered)")
finally:
    print("\n  restoring #0…"); dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.user_workspace"))
    print("  vs #0:", dm.compare_snapshot_to_live(E, BASE))
print("FAILURES:", len(F))
