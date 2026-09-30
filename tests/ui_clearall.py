from uiharness import *
from itemmaster import dept_mapping as dm
import streamlit as st_mod
from itemmaster.db import get_engine
E = get_engine(); CID = 197; F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
dm.release_broken_out_claim(E, CID, "Jason", is_admin=True); st_mod.cache_data.clear()
j = session("jason"); run(j, "load")
j.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
goto(j, "Department Review", "Broken Out")
j.button(key=f"claim_broken_out_{CID}").click(); run(j, "claim")
ver = lambda k: j.session_state[f"wb_{k}_{CID}"]["ver"]
filled = lambda k: sum(1 for v in j.session_state[f"wb_{k}_{CID}"]["draft"].values() if v)
check(j.button(key=f"wb_clearall_bo_{CID}_{ver('bo')}").disabled, "Clear all greyed out when nothing is filled")
j.selectbox(key=f"wb_pick_bo_{CID}_{ver('bo')}").select("FROZEN"); j.button(key=f"wb_setall_bo_{CID}_{ver('bo')}").click(); run(j, "review: set all 6")
j.button(key=f"wb_fill_ba_{CID}_{ver('ba')}").click(); run(j, "auto: fill 8")
one = next(iter(j.session_state[f"wb_bo_{CID}"]["draft"]))
j.text_input(key=f"wb_filter_bo_{CID}_{ver('bo')}").input(one); run(j, "filter review grid to 1 row")
j.button(key=f"wb_clearall_bo_{CID}_{ver('bo')}").click(); run(j, "Clear all (review)")
check(filled("bo") == 0 and filled("ba") == 8, f"all 6 review rows cleared, auto grid untouched ({filled('bo')}, {filled('ba')})")
check(j.button(key=f"bo_stage_all_{CID}").label == "Stage all 8 decision(s) in this group", "group count updated to 8")
j.button(key="topbar_undo").click(); run(j, "top-bar Undo")
print("  undo dialog:", [m.value for m in j.markdown if "Cleared all" in m.value])
next(b for b in j.button if b.label == "Confirm undo").click(); run(j, "confirm")
check(filled("bo") == 6, "Undo brought the 6 back")
j.button(key=f"wb_clearall_ba_{CID}_{ver('ba')}").click(); run(j, "Clear all (auto)")
check(filled("ba") == 0 and filled("bo") == 6, "Clear all on the auto grid clears only that grid")
dm.release_broken_out_claim(E, CID, "Jason", is_admin=True)
print("FAILURES:", len(F))
