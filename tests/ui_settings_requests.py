"""Editors ask for Settings changes; admins approve/deny; undo and redo of each."""
from testbase import BASE, IMPORT_SNAP, KEEP
from datetime import datetime, timedelta
from uiharness import *
import streamlit as st_mod
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
E = get_engine(); F = []
BLANK = BASE


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)


def settings(who, since=None):
    st_mod.cache_data.clear()
    at = session(who)
    if since:
        at.session_state["_notif_since"] = {at.session_state["name"]: since}
    run(at, f"{who} load"); goto(at, "Department Review", "Settings")
    return at


def fill_and_send(j, kind, **f):
    ver = j.session_state["_req_ver"] if "_req_ver" in j.session_state else 0
    j.radio(key=f"req_kind_{ver}").set_value(kind); run(j, f"pick {kind}")
    for k, v in f.items():
        w = f"req_{k}_{ver}"
        if k in ("src", "new", "old"):
            j.selectbox(key=w).select(v)
        elif k == "trust":
            j.checkbox(key=w).check()
        elif k == "reason":
            j.text_area(key=w).input(v)
        else:
            j.text_input(key=w).input(v)
    run(j, "fill")
    j.button(key=f"req_send_{ver}").click(); run(j, "Send request")
    return j


depts = lambda: dm.get_departments(E).iloc[:, 0].tolist()
strict = lambda: [tuple(r) for r in E.connect().execute(text("SELECT source_key, old_department FROM dbo.dept_mapping_strict_departments")).all()]
default = lambda s, o: E.connect().execute(text("SELECT new_department FROM dbo.dept_mapping_unmatched_defaults WHERE source_key=:s AND old_department=:o"),
                                           {"s": s, "o": o}).scalar()
try:
    start = datetime.utcnow() - timedelta(minutes=1)
    print("== 1. Jason (editor) asks for changes")
    j = settings("jason")
    heads = [m.value for m in j.main.markdown if m.value.startswith("####")]
    print("   editor Settings:", heads)
    check(heads[0] == "#### Request a change" and "#### Strict Departments" not in heads, "editor Settings: the request form, not the admin settings")
    check("#### Current settings (read-only)" in heads, "…plus the current settings, read-only")
    check(not any(b.key in ("add_department_btn", "remove_department_btn", "owi_apply") for b in j.button) and "owx_download" not in
          [d.proto.id for d in j.get("download_button")], "no admin controls for editors")
    j = fill_and_send(j, "add_department", dept="GROCERY")
    check(any("already a Department" in e.value for e in j.error), "asking for a Department that exists is refused with a reason")
    j = fill_and_send(j, "add_department", dept="ZZ TEST DEPT", reason="new catch-all for testing")
    j = fill_and_send(j, "add_strict", src="kehe", old="FROZEN", trust=True, reason="too broad")
    j = fill_and_send(j, "unmatched_default", src="kehe", old="BULK", new="GROCERY", reason="always grocery")
    j = fill_and_send(j, "add_department", dept="ZZ WITHDRAW ME")
    mine = dm.list_settings_requests(E, requested_by="Jason")
    check(len(mine) == 4 and all(r["status"] == "pending" for r in mine), "4 requests waiting")
    wid = next(r["request_id"] for r in mine if r["payload"].get("department") == "ZZ WITHDRAW ME")
    j = settings("jason")
    j.button(key=f"req_withdraw_{wid}").click(); run(j, "Withdraw")
    check(next(r for r in dm.list_settings_requests(E) if r["request_id"] == wid)["status"] == "withdrawn", "Jason can withdraw a waiting request")
    ids = {r["payload"].get("department") or r["payload"].get("old_department"): r["request_id"] for r in dm.list_settings_requests(E, status="pending")}

    print("\n== 2. AJ (admin) is told, and decides")
    a = settings("aj")
    check(a.button(key="topbar_bell").label.startswith("🔔 ") and any("Settings request" in x.value for x in a.sidebar.markdown),
          f"AJ's bell / notifications show the requests ({a.button(key='topbar_bell').label})")
    check(any(m.value == "#### Requests from editors (3 waiting)" for m in a.main.markdown), "Settings: Requests from editors (3 waiting), at the top")
    a.button(key=f"req_approve_{ids['ZZ TEST DEPT']}").click(); run(a, "Approve the Department")
    check("ZZ TEST DEPT" in depts(), "approved: ZZ TEST DEPT is a Department")
    a = settings("aj")
    a.button(key=f"req_approve_{ids['FROZEN']}").click(); run(a, "Approve the Strict Department (re-runs the engine)")
    check(("kehe", "FROZEN") in strict(), "approved: the Strict Department is in place")
    a = settings("aj")
    a.text_input(key=f"req_note_{ids['BULK']}").input("we'll use Unmatched review instead"); run(a, "note")
    a.button(key=f"req_deny_{ids['BULK']}").click(); run(a, "Deny the default")
    check(default("kehe", "BULK") is None, "denied: no default was set")

    print("\n== 3. Jason hears back")
    j = settings("jason", since=start)
    side = " ".join(x.value for x in j.sidebar.markdown) + " ".join(x.value for x in j.sidebar.caption)
    check("approved your request" in side and "denied your request" in side and "Unmatched Review instead".lower() in side.lower(),
          "Jason's notifications: approved ×2, denied (with AJ's note)")
    badges = " ".join(m.value for m in j.main.markdown)
    check("Approved" in badges and "Denied" in badges and "Withdrawn" in badges, "Your requests shows each outcome")

    print("\n== 4. Undo and redo")
    a = settings("aj")
    a.button(key=f"req_undo_{ids['ZZ TEST DEPT']}").click(); run(a, "Undo the Department approval")
    check("ZZ TEST DEPT" not in depts() and next(r for r in dm.list_settings_requests(E) if r["request_id"] == ids["ZZ TEST DEPT"])["status"] == "pending",
          "Undo: the Department is taken back out, and the request is waiting again")
    a = settings("aj")
    a.button(key=f"req_approve_{ids['ZZ TEST DEPT']}").click(); run(a, "Redo: approve again")
    check("ZZ TEST DEPT" in depts(), "Redo (approve again): it's back")
    a = settings("aj")
    a.button(key=f"req_undo_{ids['FROZEN']}").click(); run(a, "Undo the Strict approval (re-runs the engine)")
    check(("kehe", "FROZEN") not in strict(), "Undo: the Strict Department is removed")
    a = settings("aj")
    a.button(key=f"req_undo_{ids['BULK']}").click(); run(a, "Undo the denial")
    check(next(r for r in dm.list_settings_requests(E) if r["request_id"] == ids["BULK"])["status"] == "pending",
          "Undo a denial: waiting again")
    a = settings("aj")
    a.button(key=f"req_approve_{ids['BULK']}").click(); run(a, "…and approve it after all")
    check(default("kehe", "BULK") == "GROCERY", "approved: the Unmatched Default is set")
    a = settings("aj")
    a.button(key=f"req_undo_{ids['BULK']}").click(); run(a, "Undo that approval")
    check(default("kehe", "BULK") is None, "Undo: the default is put back as it was (none)")
    check(len(a.exception) == 0 and not any("Something went wrong" in x.value for x in a.markdown), "no errors")
finally:
    print("\n  back to the blank baseline…"); dm.restore_snapshot(E, BLANK, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.dept_settings_requests"))
        c.execute(text("DELETE FROM dbo.user_workspace")); c.execute(text("DELETE FROM dbo.change_discard_notices"))
    for s_ in dm.list_snapshots(E).to_dict("records"):
        if s_["snapshot_id"] not in KEEP:
            dm.delete_snapshot(E, s_["snapshot_id"])
    print("  vs #%d:" % BLANK, dm.compare_snapshot_to_live(E, BLANK), "ZZ left:", [d for d in depts() if d.startswith("ZZ")], strict())
print("FAILURES:", len(F))
