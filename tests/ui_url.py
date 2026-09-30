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
qp = dict(j.query_params)
print("  address:", qp)
check(qp.get("group") == [str(CID)] or qp.get("group") == str(CID), "the claimed group goes in the address")
# a reload with only tab/sub/group in the address
r = session("jason")
r.query_params["tab"] = "Department Review"; r.query_params["sub"] = "Broken Out"; r.query_params["group"] = str(CID)
run(r, "reload from the address")
check(any(b.key == f"bo_stage_all_{CID}" for b in r.button), "reload opens straight to that group, grids and all")
# Item Master filters
im = session("jason"); run(im, "load"); goto(im, "Item Master")
im.selectbox(key="im_dept").select("FROZEN"); run(im, "Department = FROZEN")
im.text_input(key="im_search").input("pizza"); run(im, "search pizza")
qp = dict(im.query_params); print("  address:", qp)
check("im_dept" in qp and "im_q" in qp, "Item Master filters go in the address")
r2 = session("jason")
for k, v in qp.items():
    r2.query_params[k] = v[0] if isinstance(v, list) else v
run(r2, "reload Item Master from the address")
check(r2.selectbox(key="im_dept").value == "FROZEN" and r2.text_input(key="im_search").value == "pizza", "reload restores the Item Master filters")
dm.release_broken_out_claim(E, CID, "Jason", is_admin=True)
print("FAILURES:", len(F))
