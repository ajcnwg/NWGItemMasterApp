import io
import pandas as pd
import streamlit as st_mod
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text
E = get_engine(); CID = 197; F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
orig = dm.get_combo_snapshot(E, CID)
auto = dm.get_auto_decided_upc_overrides(E, CID); AU = auto["upc"].tolist()
def reset():
    with E.begin() as c:
        for t in ("dept_mapping_recent_moves",) + dm.STAGED_TABLES:
            c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": CID})
        dm._restore_combo_snapshot(c, CID, orig, "test")
        c.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
    dm.release_broken_out_claim(E, CID, "Jason", is_admin=True); st_mod.cache_data.clear()
staged = lambda: {u: c["department"] for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == CID}
buf = io.BytesIO(); pd.DataFrame([{"UPC": u, "Department": "USE AUTO/SUGGESTED"} for u in AU[:3]] + [{"UPC": AU[3], "Department": "DELI"}]).to_excel(buf, index=False)
real = st_mod.file_uploader
UPLOAD = [True]
st_mod.file_uploader = lambda label, *a, key=None, **k: (buf.seek(0) or type("U", (io.BytesIO,), {"name": "my group.xlsx"})(buf.getvalue())) if key and key.startswith("grp_up_") and UPLOAD[0] else real(label, *a, key=key, **k)
def file_shown(at): return any("my group.xlsx" in m.value and "not used yet" in m.value for m in at.markdown)
def filled(at): return {u: d for u, d in at.session_state[f"wb_ba_{CID}"]["draft"].items() if d}
def topbar(at, kind):
    at.button(key=f"topbar_{kind}").click(); run(at, f"top-bar {kind}")
    next(b for b in at.button if b.label == f"Confirm {kind}").click(); run(at, "confirm")
try:
    reset()
    j = session("jason"); run(j, "load")
    j.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(j, "Department Review", "Broken Out")
    j.button(key=f"claim_broken_out_{CID}").click(); run(j, "claim + upload")
    UPLOAD[0] = False   # from here on, only what the app kept
    check(file_shown(j), "uploaded file waits with its choices")
    # --- Put into the grids, undo, redo
    next(b for b in j.button if (b.key or "").startswith("grp_apply_")).click(); run(j, "Put into the grids")
    check(len(filled(j)) == 4 and not file_shown(j), "into grids: 4 rows filled, file used up")
    topbar(j, "undo")
    check(not filled(j) and file_shown(j), "undo: grids empty AND the file is back with its choices")
    check(any((b.key or "").startswith("grp_stage_") for b in j.button) and any((b.key or "").startswith("grp_apply_") for b in j.button),
          "…both buttons are there again")
    topbar(j, "redo")
    check(len(filled(j)) == 4 and not file_shown(j), "redo: grids filled, file used up again")
    topbar(j, "undo")
    # --- Stage straight from the file, undo, redo
    next(b for b in j.button if (b.key or "").startswith("grp_stage_")).click(); run(j, "Stage these")
    check(len(staged()) == 4 and not file_shown(j), "staged 4, file used up")
    topbar(j, "undo")
    check(not staged() and not filled(j) and file_shown(j), "undo the stage: nothing staged, grids untouched, the file is back with its choices")
    topbar(j, "redo")
    check(len(staged()) == 4 and not file_shown(j), "redo: staged again, file used up")
    topbar(j, "undo")
    # --- Remove file
    next(b for b in j.button if (b.key or "").startswith("grp_rm_")).click(); run(j, "Remove file")
    check(not file_shown(j) and any(e.type == "file_uploader" for e in walk(j.main)), "Remove file: back to the upload box")
finally:
    st_mod.file_uploader = real
    reset(); print("  restored exactly:", dm._redo_state_key(dm.get_combo_snapshot(E, CID)) == dm._redo_state_key(orig))
print("FAILURES:", len(F))
