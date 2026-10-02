"""Part 3: stage decisions (whole group + Broken Out items), two editors
approve, one pushes -> Item Master Department updates immediately (no
Merge). Then an admin Settings change re-runs the engine, and reverting it
puts everything back."""
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


def show(at, search):
    at.session_state["dept_shared_filter"] = {"search": search, "sort_label": None, "sort_desc": True, "page_size": 25}


def wb_ver(at, key):
    return at.session_state[f"wb_{key}"]["ver"] if f"wb_{key}" in at.session_state else 0


def item_depts(upcs):
    with E.connect() as c:
        return dict(c.execute(text("SELECT upc, department FROM dbo.items WHERE upc IN :u").bindparams(
            __import__("sqlalchemy").bindparam("u", expanding=True)), {"u": list(upcs)}).all())


aj = session("aj"); run(aj, "load")
jason = session("jason"); run(jason, "load")
# --- Settings: strict department re-runs the engine immediately; revert restores ---
before = dm.combo_decision_map(E)
target_raw = "FROZEN"  # a raw department text on KEHE
aff = [cid for cid, (tier, stt, dd) in before.items() if tier == "auto" and stt == "not_reviewed"]
with E.connect() as c:
    kehe_frozen_auto = c.execute(text(
        "SELECT COUNT(*) FROM dbo.dept_mapping_combos WHERE source_key='kehe' AND UPPER(raw_department)='FROZEN' "
        "AND tier='auto' AND decision_state='not_reviewed' AND manual_department IS NULL")).scalar()
print("  kehe FROZEN auto groups:", kehe_frozen_auto)
import streamlit as st_mod
import time as _t


def save_strict_via_ui(rows_sql, label):
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.dept_mapping_strict_departments"))
        for r in rows_sql:
            c.execute(text("INSERT INTO dbo.dept_mapping_strict_departments (source_key, old_department, trust_direct_evidence, updated_by) "
                           "VALUES (:s, :o, :t, 'AJ')"), r)
    st_mod.cache_data.clear()
    goto(aj, "Department Review", "Settings")
    # (Save is enabled only after an edit: add a blank row — the save drops blank rows,
    # so it saves exactly what's in the table now and re-runs the engine)
    edit_grid(aj, "strict_departments_editor", {})
    el = grid(aj, "strict_departments_editor")
    GRID_EDITS[el.proto.id]["added_rows"] = [{"source_key": None, "old_department": None}]
    run(aj, "edit the grid")
    t = _t.time()
    next(b for b in aj.button if b.label == "Save Strict Departments").click(); run(aj, label)
    GRID_EDITS.pop(el.proto.id, None)
    print(f"  engine re-run took {_t.time() - t:.0f}s; toast:", [x.value for x in aj.toast])


strict0 = [dict(s=r[0], o=r[1], t=r[2]) for r in E.connect().execute(text(
    "SELECT source_key, old_department, trust_direct_evidence FROM dbo.dept_mapping_strict_departments")).all()]
print("  strict now:", strict0)
with E.connect() as c:
    kf = [r[0] for r in c.execute(text(
        "SELECT combo_id FROM dbo.dept_mapping_combos WHERE source_key='kehe' AND UPPER(raw_department)='FROZEN' "
        "AND tier='auto' AND decision_state='not_reviewed' AND manual_department IS NULL"))]
    kf_upcs = [r[0] for r in c.execute(text(
        "SELECT TOP 50 cu.upc FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.dept_mapping_combos c ON c.combo_id=cu.combo_id "
        "WHERE c.source_key='kehe' AND UPPER(c.raw_department)='FROZEN' AND c.tier='auto' AND c.decision_state='not_reviewed'"))]
sample_before = set(item_depts(kf_upcs).values())
print(f"  kehe FROZEN auto groups: {len(kf)}; sample items dept:", sample_before)
save_strict_via_ui(strict0 + [dict(s="kehe", o="FROZEN", t=False)], "Save Strict Departments (+kehe FROZEN)")
after = dm.combo_decision_map(E)
moved = [cid for cid in kf if after[cid][0] != "auto"]
check(len(moved) >= len(kf) * 0.8, f"most kehe FROZEN auto groups left auto ({len(moved)}/{len(kf)})")
cw = set(dm.get_review_queue(E, "review")["combo_id"]) | set(dm.get_review_queue(E, "unmatched")["combo_id"])
check(all(c in cw for c in moved), "they now wait in Crosswalk/Unmatched")
check(not any(item_depts(kf_upcs).values()), "their items' Department cleared in Item Master (no decision any more)")
save_strict_via_ui(strict0, "Save Strict Departments (reverted)")
final = dm.combo_decision_map(E)
check(all(final[c] == before[c] for c in kf), "reverting puts every one of those groups back exactly")
check(set(item_depts(kf_upcs).values()) == sample_before, f"their Department is back in Item Master ({set(item_depts(kf_upcs).values())})")
diffs = [c for c in before if final.get(c) != before[c]]
print("  groups different from before the Settings round trip:", len(diffs), diffs[:10])


print("FAILURES:", len(FAIL))
for f in FAIL: print(" -", f)
