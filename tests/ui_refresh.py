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
AU = dm.get_auto_decided_upc_overrides(E, CID)["upc"].tolist()
def reset():
    with E.begin() as c:
        for t in ("dept_mapping_recent_moves",) + dm.STAGED_TABLES:
            c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": CID})
        dm._restore_combo_snapshot(c, CID, orig, "test")
        c.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
        c.execute(text("DELETE FROM dbo.user_workspace WHERE username = 'Jason'"))
    dm.release_broken_out_claim(E, CID, "Jason", is_admin=True); st_mod.cache_data.clear()
buf = io.BytesIO(); pd.DataFrame([{"UPC": u, "Department": "USE AUTO/SUGGESTED"} for u in AU[:3]]).to_excel(buf, index=False)
real = st_mod.file_uploader
UP = [False]
st_mod.file_uploader = lambda label, *a, key=None, **k: type("U", (io.BytesIO,), {"name": "my group.xlsx"})(buf.getvalue()) if key and key.startswith("grp_up_") and UP[0] else real(label, *a, key=key, **k)
S = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
def open_group(at):
    at.session_state["dept_shared_filter"] = dict(S); goto(at, "Department Review", "Broken Out")
try:
    reset()
    j = session("jason"); run(j, "load"); open_group(j)
    j.button(key=f"claim_broken_out_{CID}").click(); run(j, "claim")
    v = j.session_state[f"wb_bo_{CID}"]["ver"]
    j.selectbox(key=f"wb_pick_bo_{CID}_{v}").select("FROZEN"); j.button(key=f"wb_setall_bo_{CID}_{v}").click(); run(j, "review: set all")
    v = j.session_state[f"wb_ba_{CID}"]["ver"]
    j.button(key=f"wb_fill_ba_{CID}_{v}").click(); run(j, "auto: fill")
    UP[0] = True; run(j, "upload a file"); UP[0] = False
    before = {k: dict(j.session_state[f"wb_{k}_{CID}"]["draft"]) for k in ("bo", "ba")}
    n_steps = len(j.session_state["_draft_undo"])
    with E.connect() as c:
        keys = c.execute(text("SELECT item_key FROM dbo.user_workspace WHERE username='Jason'")).scalars().all()
    print("  saved for Jason:", sorted(keys))
    # --- "refresh": a brand-new browser session for the same person
    r = session("jason"); run(r, "refresh (new session)"); open_group(r)
    after = {k: {u: d for u, d in r.session_state[f"wb_{k}_{CID}"]["draft"].items()} for k in ("bo", "ba")}
    check(after == before and sum(1 for d in after["bo"].values() if d) == 6 and sum(1 for d in after["ba"].values() if d) == 8,
          "grid values survive the refresh (6 review + 8 auto)")
    check(r.button(key=f"bo_stage_all_{CID}").label == "Stage all 14 decision(s) in this group", "Stage count is right after refresh")
    check(len(r.session_state["_draft_undo"]) == n_steps, f"{n_steps} Undo steps survive the refresh")
    check(any("my group.xlsx" in m.value and "not used yet" in m.value for m in r.markdown), "the waiting Excel file survives the refresh")
    r.button(key="topbar_undo").click(); run(r, "top-bar Undo after refresh")
    print("  undo dialog:", [m.value for m in r.markdown if m.value.startswith("**Fill") or "Filled" in m.value][:1])
    next(b for b in r.button if b.label == "Confirm undo").click(); run(r, "confirm")
    check(not any(r.session_state[f"wb_ba_{CID}"]["draft"].get(u) for u in AU), "Undo after refresh takes back the auto fill")
    r2 = session("jason"); run(r2, "refresh again")
    check(not any(r2.session_state[f"wb_ba_{CID}"]["draft"].get(u) for u in AU) and len(r2.session_state["_draft_redo"]) == 1,
          "that undo (and its Redo) survive another refresh too")
    k = session("kristi"); run(k, "Kristi")
    check(not any(x.startswith("wb_bo_197") for x in k.session_state._state._new_session_state) if hasattr(k.session_state, "_state") else True,
          "Kristi doesn't get Jason's work")
finally:
    st_mod.file_uploader = real
    reset(); print("  restored exactly:", dm._redo_state_key(dm.get_combo_snapshot(E, CID)) == dm._redo_state_key(orig))
print("FAILURES:", len(F))
