"""Full review of the old-workbook import section: every card has the same
features as regular pending changes, and every Undo path works."""
from testbase import BASE, IMPORT_SNAP, KEEP
import io
from uiharness import *
import streamlit as st_mod
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
E = get_engine(); F = []
WB = __import__('testbase').V2
DATA = open(WB, "rb").read()



def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)


def keys(at):
    return {b.key for b in at.button if b.key} | {s.key for s in at.selectbox if s.key} | {c.key for c in at.checkbox if c.key}


def fresh():
    st_mod.cache_data.clear()
    a = session("aj"); run(a, "load"); goto(a, "Department Review", "Pending Changes"); return a


def fresh_import() -> int:
    """Baseline, then the import through the Settings upload, like an admin would.
    Returns the import's own "Before importing" snapshot."""
    dm.restore_snapshot(E, BASE, "AJ")
    if "FOOD SERVICE" not in dm.get_departments(E).iloc[:, 0].tolist():
        dm.add_department(E, "FOOD SERVICE")
    real = st_mod.file_uploader
    st_mod.file_uploader = lambda label, *a, key=None, **k: (type("U", (io.BytesIO,), {"name": "department_workbook V2.xlsx"})(DATA)
                                                             if key and key.startswith("owi_file_") else real(label, *a, key=key, **k))
    try:
        st_mod.cache_data.clear()
        a = session("aj"); run(a, "load"); goto(a, "Department Review", "Settings")
        a.button(key="owi_apply").click(); run(a, "import via Settings")
    finally:
        st_mod.file_uploader = real
    return int(dm.list_snapshots(E).iloc[0]["snapshot_id"])


def pend_counts():
    with E.connect() as c:
        return tuple(c.execute(text("SELECT (SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes), "
                                    "(SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes), "
                                    "(SELECT COUNT(*) FROM dbo.item_master_pending_changes), "
                                    "(SELECT COUNT(*) FROM dbo.dept_mapping_recent_moves)")).one())


def state(cid):
    with E.connect() as c:
        return c.execute(text("SELECT decision_state FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": cid}).scalar()


MOVED = {2928: "broken_out", 2592: "broken_out", 2404: "not_reviewed"}  # before the import
BO = 3109  # SS COOKIES (SUPPLIES waits on Needs your choice: nothing staged)
try:
    print("  fresh import..."); BEFORE = fresh_import()
    pend = dm.get_pending_changes(E)
    GROUPS = list(pend)
    BULK_NUTS = next(cid for cid, c in pend.items() if c["label"].endswith("BULK NUTS BEANS & GRAINS / NUTS"))
    print("  start:", pend_counts())

    # ---------- 1. same features as regular pending changes ----------
    print("\n== 1. Every import card has the regular features")
    a = fresh(); k = keys(a)
    exps = {e.key for e in a.expander if e.key}
    miss = {cid: [n for n, key in (("Include", f"dept_pending_include_combo_{cid}"), ("Undo", f"undo_pending_{cid}"),
                                   ("Admin override", f"admin_override_btn_ready_{cid}"),
                                   ("Suggest a different Department", f"dept_pending_suggest_{cid}"))
                   if key not in k] + ([] if f"pending_items_{cid}" in exps else ["Show affected items"]) for cid in GROUPS}
    miss = {c: m for c, m in miss.items() if m}
    check(not miss, f"all {len(GROUPS)} import group cards: Include, Undo, Admin override, Suggest, Show affected items {miss or ''}")
    wb_lines = [c.value for c in a.caption if "Staged by " in c.value and "Old workbook (" in c.value]
    check(len(wb_lines) >= len(GROUPS),
          f"...and each card's one detail line says who staged it and what the old workbook said ({len(wb_lines)})")
    bo_keys = [x for x in k if str(BO) in x]
    check(any(x.startswith("dept_pending_include_upc_group_") for x in bo_keys) and any(x.startswith("undo_pending_upc_combo_") for x in bo_keys)
          and any("admin" in x for x in bo_keys), "Broken Out item card: Include, Undo, Admin override, item grid")
    check(pend_counts()[2] == 0 and not any(x.startswith("undo_overrides_imp_") for x in k), "no UPC overrides at all")
    for cid in (3109,):
        ck = [x for x in k if str(cid) in x]
        check(any(x.startswith("dept_pending_include_upc_group_") for x in ck) and any(x.startswith("undo_pending_upc_combo_") for x in ck)
              and any("admin" in x for x in ck), f"Broken Out group {cid}: Include, Undo, Admin override, item grid")
    check(sum(1 for x in k if x.startswith("undo_recent_")) == 3, "3 move cards with Undo")
    heads = [x.value for x in a.main.markdown if x.value.startswith("#####")]
    print("   ", heads)
    check(any(h.startswith("##### Broken Out items (1 group(s))") for h in heads), "Broken Out items: SS COOKIES (SUPPLIES waits on its question)")
    check("undo_whole_import" in k, "admin: Undo the whole import")
    j = session("jason"); run(j, "editor load"); goto(j, "Department Review", "Pending Changes")
    check("undo_whole_import" not in keys(j), "editors don't get Undo the whole import")

    # ---------- 2. top-bar Undo / Redo on an import step ----------
    print("\n== 2. Top-bar Undo / Redo")
    a = fresh()
    a.button(key="topbar_undo").click(); run(a, "top-bar Undo")
    next(b for b in a.button if b.label == "Confirm undo").click(); run(a, "confirm")
    check(pend_counts()[1] == 0, f"top-bar Undo took back the import's last step (SS COOKIES' 59 items) {pend_counts()}")
    a = fresh()
    a.button(key="topbar_redo").click(); run(a, "top-bar Redo")
    btn = [b for b in a.button if (b.label or "").startswith("Confirm")]
    if btn:
        btn[0].click(); run(a, "confirm redo")
    with E.connect() as c:
        notes = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE origin_note LIKE '%old-workbook%'")).scalar()
    check(pend_counts()[1] == 59 and notes == 59, f"Redo put them back, import note intact ({pend_counts()}, notes {notes})")

    # ---------- 3. card Undo on an import group, then top-bar Undo of that ----------
    print("\n== 3. Undo on an import group card")
    a = fresh()
    a.button(key=f"undo_pending_{BULK_NUTS}").click(); run(a, "Undo on KEHE BULK NUTS")
    a.button(key=f"undo_to_{BULK_NUTS}_0").click(); run(a, "Confirm undo")
    check(BULK_NUTS not in dm.get_pending_changes(E), "the group's staged decision is gone")
    a = fresh()
    a.button(key="topbar_undo").click(); run(a, "top-bar Undo (of that undo)")
    next(b for b in a.button if b.label == "Confirm undo").click(); run(a, "confirm")
    p2 = dm.get_pending_changes(E).get(BULK_NUTS)
    check(p2 is not None and "old-workbook" in (p2.get("origin_note") or ""), "top-bar Undo brings it back, note and all")

    # ---------- 4. card Undo on an import move ----------
    print("\n== 4. Undo on an import move")
    a = fresh()
    a.button(key="undo_recent_2928").click(); run(a, "Undo on KEHE FROZEN BKRY BULK move")
    print("   options:", [b.key for b in a.button if (b.key or "").startswith("undo_to_2928_")])
    a.button(key="undo_to_2928_1").click(); run(a, "Confirm undo back to Broken Out")
    check(state(2928) == "broken_out", f"KEHE BKRY BULK is back in Broken Out ({state(2928)})")

    # ---------- 5. Undo the whole import ----------
    print("\n== 5. Undo the whole import (on a fresh import)")
    BEFORE = fresh_import()
    a = fresh()
    a.button(key="undo_whole_import").click(); run(a, "Undo the whole import")
    print("  ", [t.value[:230] for t in a.toast])
    check(pend_counts() == (0, 0, 0, 0), f"nothing from the import is left staged or in Recent moves {pend_counts()}")
    bad = {cid: state(cid) for cid, st_ in MOVED.items() if state(cid) != st_}
    check(not bad, f"its 4 moves are undone - groups back where they were {bad or ''}")
    cmp_ = dm.compare_snapshot_to_live(E, BEFORE)
    check(cmp_["groups_different"] == 0 and cmp_["changed"] == 0, f"everything matches the snapshot from just before the import {cmp_}")
    with E.connect() as c:
        steps = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_action_log WHERE click_id LIKE 'oldwb-%'")).scalar()
    check(steps == 0, "its top-bar Undo steps are cleared")
    a = fresh()
    check(not any(x.value.startswith("#### Old-workbook import") for x in a.main.markdown), "the import section is gone")
    check(len(a.exception) == 0 and not any("Something went wrong" in x.value for x in a.markdown), "no errors")
finally:
    print("\n  leaving a fresh import (with its top-bar Undo steps)...")
    BEFORE = fresh_import()
    SAFE = dm.take_snapshot(E, "AJ", label="Old-workbook import (no UPC overrides; items staged in their Broken Out groups) - revert point", kind="manual")
    with E.connect() as c:
        steps = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_action_log WHERE click_id LIKE 'oldwb-%'")).scalar()
    print(f"  now: {pend_counts()}, top-bar Undo steps {steps}, before-import snapshot #{BEFORE}, revert point #{SAFE}")
print("FAILURES:", len(F))
