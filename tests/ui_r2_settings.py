"""Round 2 — Settings: adding / removing Departments (and why a Department
in use can't be removed), Strict Departments, Unmatched Department Defaults
(one row per distributor Department the Unmatched groups use), editor
requests — each with its effect on the groups and the item master."""
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


def settings(who):
    st_mod.cache_data.clear()
    goto(who, "Department Review", "Settings")
    return who


def depts():
    return dm.get_departments(E).iloc[:, 0].tolist()


def q(sql, **p):
    with E.connect() as c:
        return c.execute(text(sql), p).all()


try:
    dm.restore_snapshot(E, BASE, "AJ")
    a = session("aj"); run(a, "AJ signs in")

    print("\n== 1. Add a Department, use it, try to remove it")
    settings(a)
    a.text_input(key="new_department_input").input("ZZ R2 DEPT"); run(a, "type")
    a.button(key="add_department_btn").click(); run(a, "Add")
    check("ZZ R2 DEPT" in depts(), "added")
    grp = dm.get_review_queue(E, "review")
    grp = grp[grp["n_upcs_total"].between(2, 30)].sort_values("combo_id").iloc[0]
    cid = int(grp["combo_id"])
    st_mod.cache_data.clear()
    a.session_state["dept_shared_filter"] = {"search": " / ".join(b for b in (grp["raw_department"], grp["raw_category"], grp["raw_subcategory"]) if b),
                                              "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(a, "Department Review", "Crosswalk")
    check("ZZ R2 DEPT" in a.selectbox(key=f"dept_choice_review_{cid}").options, "offered on Crosswalk cards straight away")
    a.selectbox(key=f"dept_choice_review_{cid}").select("ZZ R2 DEPT"); run(a, "pick")
    a.button(key=f"approve_review_{cid}").click(); run(a, "Approve as ZZ R2 DEPT")
    settings(a)
    check("GROCERY" not in a.selectbox(key="remove_department_select").options, "Scan Advantage's own Departments can't be removed")
    a.selectbox(key="remove_department_select").select("ZZ R2 DEPT"); run(a, "pick ZZ R2 DEPT to remove")
    warn = " ".join(w.value for w in a.warning)
    check(a.button(key="remove_department_btn").disabled and "staged group decisions" in warn,
          f"in use (staged) → Remove is blocked and says where ({warn[:120]})")
    row = dm.department_usage_all(E).set_index("department").loc["ZZ R2 DEPT"]
    check(int(row["Staged"]) == 1, f"the Departments table shows it staged once ({dict(row)})")
    a.button(key="topbar_undo").click(); run(a, "top-bar Undo")
    [b for b in a.button if (b.label or "").startswith("Confirm")][0].click(); run(a, "confirm")
    settings(a)
    a.selectbox(key="remove_department_select").select("ZZ R2 DEPT"); run(a, "pick again")
    check(not a.button(key="remove_department_btn").disabled, "no longer used → Remove allowed")
    a.button(key="remove_department_btn").click(); run(a, "Remove")
    check("ZZ R2 DEPT" not in depts(), "removed")
    clean(a, "Settings")

    print("\n== 2. A pushed Department can't be removed either")
    dm.add_department(E, "ZZ R2 DEPT", "AJ")
    dm.upsert_combo_suggestion(E, cid, "review", "ZZ R2 DEPT", grp["source_key"], "x", int(grp["n_upcs_total"]), "AJ")
    pc = dm.get_pending_changes(E)[cid]
    dm.approve_combo(E, cid, "ZZ R2 DEPT", "AJ", pushed_by="AJ"); dm.delete_pending_change(E, cid)
    dm.sync_item_departments(E)
    u = dm.department_usage(E, "ZZ R2 DEPT")
    check(u["groups decided as it"] == 1 and u["items in the item master"] > 0, f"usage after the push: {u}")
    settings(a)
    a.selectbox(key="remove_department_select").select("ZZ R2 DEPT"); run(a, "pick")
    check(a.button(key="remove_department_btn").disabled, "Remove blocked: a decided group and its items use it")

    print("\n== 3. Strict Department")
    auto = q("SELECT TOP 1 source_key, UPPER(raw_department), COUNT(*) FROM dbo.dept_mapping_combos WHERE tier = 'auto' "
             "AND decision_state = 'not_reviewed' AND ISNULL(raw_department,'') <> '' GROUP BY source_key, UPPER(raw_department) "
             "HAVING COUNT(*) BETWEEN 2 AND 6 ORDER BY COUNT(*)")[0]
    s_key, old, n = auto
    print(f"   {s_key.upper()} / {old}: {n} auto-decided group(s)")
    before = q("SELECT combo_id, tier FROM dbo.dept_mapping_combos WHERE source_key=:s AND UPPER(raw_department)=:o", s=s_key, o=old)
    settings(a)
    base_rows = len(q("SELECT 1 FROM dbo.dept_mapping_strict_departments"))
    edit_grid(a, "strict_departments_editor", {})
    el = grid(a, "strict_departments_editor")
    GRID_EDITS[el.proto.id]["added_rows"] = [{"source_key": s_key, "old_department": old, "trust_direct_evidence": False}]
    run(a, "add a row")
    a.button(key="save_strict_btn").click(); run(a, "Save Strict Departments (re-runs the engine)")
    GRID_EDITS.pop(el.proto.id, None)
    after = dict(q("SELECT combo_id, tier FROM dbo.dept_mapping_combos WHERE source_key=:s AND UPPER(raw_department)=:o", s=s_key, o=old))
    check(len(q("SELECT 1 FROM dbo.dept_mapping_strict_departments")) == base_rows + 1, "saved")
    check(all(t != "auto" for t in after.values()), f"its groups are no longer auto-decided — they wait for a person ({after})")
    st_mod.cache_data.clear()
    a.session_state["dept_shared_filter"] = {"search": old, "sort_label": None, "sort_desc": True, "page_size": 100}
    goto(a, "Department Review", "Crosswalk")
    shown = {int(b.key.rsplit('_', 1)[1]) for b in a.button if (b.key or "").startswith("approve_review_")}
    goto(a, "Department Review", "Unmatched")
    shown |= {int(b.key.rsplit('_', 1)[1]) for b in a.button if (b.key or "").startswith("approve_unmatched_")}
    was_auto = {cid for cid, t in before if t == "auto"}
    check(was_auto <= shown, f"the {len(was_auto)} that were auto-decided now show up in Crosswalk / Unmatched")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.dept_mapping_strict_departments WHERE source_key=:s AND old_department=:o"), {"s": s_key, "o": old})
    dm.run_engine_guarded(E, "AJ")
    back = dict(q("SELECT combo_id, tier FROM dbo.dept_mapping_combos WHERE source_key=:s AND UPPER(raw_department)=:o", s=s_key, o=old))
    check(back == dict(before), "removing it: back to auto-decided, exactly as before")

    print("\n== 4. Unmatched Department Defaults — one row per distributor Department")
    settings(a)
    olds = dm.unmatched_old_departments(E)
    want = {r[0] for r in q("SELECT DISTINCT UPPER(LTRIM(RTRIM(ISNULL(raw_department,'')))) FROM dbo.dept_mapping_combos "
                            "WHERE tier = 'unmatched' OR (tier = 'auto' AND ISNULL(n_evidence,0) = 0)")}
    saved = {r[0] for r in q("SELECT UPPER(old_department) FROM dbo.dept_mapping_unmatched_defaults")}
    check(set(olds["old_department"]) == want | saved and olds["old_department"].is_unique,
          f"{len(olds)} rows: every distributor Department the Unmatched groups use, once each")
    target = olds[olds["n_waiting"] > 0].iloc[0]
    old_t, dflt = target["old_department"], target["default"]
    new_d = "GROCERY" if dflt != "GROCERY" else "DELI"
    pos = list(olds["old_department"]).index(old_t)
    waiting = [r[0] for r in q("SELECT combo_id FROM dbo.dept_mapping_combos WHERE tier='unmatched' AND decision_state='not_reviewed' "
                               "AND UPPER(LTRIM(RTRIM(ISNULL(raw_department,''))))=:o AND ISNULL(n_evidence,0)=0 "
                               "AND resolved_via = 'Default from Key'", o=old_t)]  # a similar group's clue wins over a default
    print(f"   {old_t}: {dflt} → {new_d}  ({len(waiting)} waiting group(s))")
    edit_grid(a, "umd_editor_All_", {pos: {"Default": new_d}}); run(a, "change the default")
    check(a.button(key="save_unmatched_defaults_btn").label == "Save 1 change(s)", "Save shows 1 change")
    a.button(key="save_unmatched_defaults_btn").click(); run(a, "Save (re-runs the engine)")
    GRID_EDITS.clear()
    sugg = dict(q("SELECT combo_id, suggested_department FROM dbo.dept_mapping_combos WHERE combo_id IN (%s)" % ",".join(map(str, waiting))))
    check(waiting and all(v == new_d for v in sugg.values()), f"its waiting Unmatched groups are now suggested as {new_d} ({set(sugg.values())})")
    st_mod.cache_data.clear()
    a.session_state["dept_shared_filter"] = {"search": old_t, "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(a, "Department Review", "Unmatched")
    caps = " ".join(c.value for c in a.caption)
    check(f"saved default: **{new_d}**" in caps, "the Unmatched cards say so")
    settings(a)
    edit_grid(a, "umd_editor_All_", {pos: {"Default": None}}); run(a, "clear the default")
    a.button(key="save_unmatched_defaults_btn").click(); run(a, "Save")
    GRID_EDITS.clear()
    check(not q("SELECT 1 FROM dbo.dept_mapping_unmatched_defaults WHERE UPPER(old_department)=:o", o=old_t), "cleared: no default")
    with E.begin() as c:
        dm.set_unmatched_default(c, "any", old_t, dflt, "AJ")
    dm.run_engine_guarded(E, "AJ")
    clean(a, "Settings")

    print("\n== 5. Editor asks for a default on an Unmatched Department")
    j = session("jason"); run(j, "Jason"); settings(j)
    j.radio(key="req_kind_0").set_value("unmatched_default"); run(j, "pick kind")
    opts = j.selectbox(key="req_old_0").options
    check(set(opts) == set(olds["old_department"]) - {""}, f"the Department list comes straight from the Unmatched groups ({len(opts)})")
    req_new = "DELI" if dflt != "DELI" else "GROCERY"  # something other than the current default
    j.selectbox(key="req_old_0").select(old_t); j.selectbox(key="req_new_0").select(req_new); run(j, "fill")
    j.button(key="req_send_0").click(); run(j, "Send request")
    reqs = dm.list_settings_requests(E, status="pending")
    check(any(r["payload"].get("old_department") == old_t for r in reqs), "request waiting for an admin")
    clean(j, "request form")
    log = dm.list_activity(E)
    check(any(log["action"].str.startswith("Requested")) and any(log["action"].str.contains("Unmatched Default")),
          "Activity has the request and the saved defaults")
finally:
    print("\n  restoring #0…"); dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.dept_settings_requests")); c.execute(text("DELETE FROM dbo.user_workspace"))
    print("  vs #0:", dm.compare_snapshot_to_live(E, BASE), [d for d in depts() if d.startswith("ZZ")])
print("FAILURES:", len(F))
