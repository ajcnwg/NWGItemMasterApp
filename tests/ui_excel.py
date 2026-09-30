"""Whole-group Excel: one file for both grids, dropdown column, import is one Undo step."""
import io
import pandas as pd
import streamlit as st_mod
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
E = get_engine(); CID = 197; FAIL = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: FAIL.append(msg)
dm.release_broken_out_claim(E, CID, "Jason", is_admin=True)
review = dm.get_pending_upc_overrides(E, CID)["upc"].tolist()
auto = dm.get_auto_decided_upc_overrides(E, CID)["upc"].tolist()
# the file a user would upload: 2 review items + 3 auto items filled in, one junk UPC
rows = [{"UPC": u, "Department": "DELI"} for u in review[:2]] + [{"UPC": u, "Department": "FROZEN"} for u in auto[:3]] + [{"UPC": "999999999999", "Department": "DELI"}]
buf = io.BytesIO(); pd.DataFrame(rows).to_excel(buf, index=False); buf.name = "group.xlsx"
real = st_mod.file_uploader
def fake(label, *a, key=None, **k):
    if key and key.startswith("grp_up_") and key.endswith("_0"):
        buf.seek(0); return buf
    return real(label, *a, key=key, **k)
st_mod.file_uploader = fake
j = session("jason"); run(j, "load")
j.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
goto(j, "Department Review", "Broken Out")
j.button(key=f"claim_broken_out_{CID}").click(); run(j, "claim")
print("  excel expanders:", [e.label for e in j.expander if "Excel" in e.label])
check(len([e for e in j.expander if "Excel" in e.label]) == 1, "one Excel section for the whole group")
print("  file check:", [c.value for c in j.caption if "have a Department" in c.value])
b = [x for x in j.button if (x.key or "").startswith("grp_apply_")]
check(b and "(5 change(s))" in b[0].label, f"5 changes found ({b and b[0].label})")
b[0].click(); run(j, "put file into grids")
d = lambda k: {u: v for u, v in j.session_state[f"wb_{k}_{CID}"]["draft"].items() if v}
check(d("bo") == {u: "DELI" for u in review[:2]}, "review grid got its 2 rows")
check(d("ba") == {u: "FROZEN" for u in auto[:3]}, "auto grid got its 3 rows")
j.button(key="topbar_undo").click(); run(j, "top-bar undo")
print("  dialog:", [m.value for m in j.markdown if "from group.xlsx" in m.value])
next(x for x in j.button if x.label == "Confirm undo").click(); run(j, "confirm undo")
check(not d("bo") and not d("ba"), "one Undo takes the whole import back out of both grids")
j.button(key="topbar_redo").click(); run(j, "redo")
next(x for x in j.button if x.label == "Confirm redo").click(); run(j, "confirm redo")
check(len(d("bo")) == 2 and len(d("ba")) == 3, "Redo puts it back")
dm.release_broken_out_claim(E, CID, "Jason", is_admin=True)
# the download itself has a dropdown Department column
import ast
src = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))), "app.py"), encoding="utf-8").read()
node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_excel_with_dropdown")
ns = {"io": io, "pd": pd}; exec(compile(ast.Module([node], []), "x", "exec"), ns)
import openpyxl
xb = ns["_excel_with_dropdown"](pd.DataFrame({"UPC": ["1", "2"], "Auto Department": ["FROZEN", None], "Department": ["", ""]}), "Department", ["GROCERY", "FROZEN", "DELI"])
ws = openpyxl.load_workbook(io.BytesIO(xb))["Items"]
dv = ws.data_validations.dataValidation[0]
check(dv.formula1 == "=Departments!$A$2:$A$4" and str(dv.sqref) == "C2:C3" and dv.showErrorMessage, f"download's Department column is a dropdown ({dv.formula1} on {dv.sqref})")
print("FAILURES:", len(FAIL))
