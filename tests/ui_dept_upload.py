from testbase import BASE, IMPORT_SNAP, KEEP
import io
import pandas as pd
import streamlit as st_mod
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text, bindparam
E = get_engine(); F = []; BO = 197
SNAP = BASE
print(dm.get_combo_snapshot(E, BO).get('decision_state') if False else '')
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
rev = dm.get_pending_upc_overrides(E, BO)["upc"].tolist()
aut = dm.get_auto_decided_upc_overrides(E, BO)["upc"].tolist()
q = dm.get_review_queue(E, "review"); cw = q[q["n_upcs_total"].between(4, 30)].sort_values("combo_id").iloc[0]
cw_upcs = dm.get_combo_member_items(E, int(cw.combo_id))["upc"].tolist()[:2]
with E.connect() as c:
    nwg = c.execute(text("SELECT TOP 1 upc, department FROM dbo.items WHERE source_key='nwg' AND department='GROCERY'")).one()
    same = c.execute(text("SELECT TOP 1 upc, department FROM dbo.items WHERE source_key='nwg' AND department='FROZEN'")).one()
rows = [(rev[0], "DELI", ""), (rev[1], "dairy", ""), (aut[0], "MEAT", "TEST BRAND"), (cw_upcs[0], "BAKERY", ""), (cw_upcs[1], "BAKERY", ""),
        (nwg[0], "FROZEN", ""), (same[0], "FROZEN", ""), ("abc", "DELI", ""), (rev[2], "NOT A DEPT", "")]
buf = io.BytesIO(); pd.DataFrame(rows, columns=["UPC", "Department", "Brand"]).to_excel(buf, index=False)
real = st_mod.file_uploader
st_mod.file_uploader = lambda label, *a, key=None, **k: type("U", (io.BytesIO,), {"name": "depts.xlsx"})(buf.getvalue()) if key and key.startswith("bulk_edit_file_") else real(label, *a, key=key, **k)
try:
    j = session("jason"); run(j, "load"); goto(j, "UPC Overrides")
    check(not any(e.label.startswith("🏷️ Setting Departments") for e in j.expander), "separate Department upload is gone")
    check(any(e.label.startswith("📄 Changing many items (Departments or any other fields)") for e in j.expander), "one combined upload")
    summ = [m.value for m in j.markdown if "Broken Out item decision(s)" in m.value]
    print("  ", summ)
    check(summ and summ[0].startswith("**6 ready** (4 UPC override(s), 3 Broken Out item decision(s)) · 1 no change")
          and "2 need fixing" in summ[0], "routing: 3 Broken Out decisions, 4 overrides (one is the Brand on a Broken Out item), 1 no change, 2 bad")
    df = j.dataframe[-1].value if j.dataframe else None
    print(df[["UPC", "Status", "Goes to"]].to_string() if df is not None else "no df")
    j.button(key="bulk_edit_stage").click(); run(j, "Stage")
    st_mod.file_uploader = real
    print("  ", [t.value for t in j.toast])
    pend = {u: c["department"] for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == BO}
    check(pend == {rev[0]: "DELI", rev[1]: "DAIRY", aut[0]: "MEAT"}, f"Broken Out items staged as item decisions ({pend})")
    im = dm.get_item_master_pending(E)
    got = {u: (im[u]["department"], im[u].get("brand")) for u in (cw_upcs[0], cw_upcs[1], nwg[0], aut[0]) if u in im}
    print("  ", got)
    check({u: v[0] for u, v in got.items() if u != aut[0]} == {cw_upcs[0]: "BAKERY", cw_upcs[1]: "BAKERY", nwg[0]: "FROZEN"},
          "the other 3 staged as UPC overrides")
    check(aut[0] in got and got[aut[0]][1] == "TEST BRAND" and got[aut[0]][0] != "MEAT",
          "Broken Out item's Brand went to a UPC override, its Department did not")
    j.button(key="topbar_undo").click(); run(j, "top-bar Undo")
    next(b for b in j.button if b.label == "Confirm undo").click(); run(j, "confirm")
    check(not any(c["combo_id"] == BO for c in dm.get_pending_upc_changes(E).values()), "top-bar Undo takes back the Broken Out part of the upload")
finally:
    st_mod.file_uploader = real
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
        c.execute(text("DELETE FROM dbo.user_workspace WHERE username='Jason'"))
    print("  restoring the imported state…"); dm.restore_snapshot(E, SNAP, "AJ")
    print("  live vs #51:", dm.compare_snapshot_to_live(E, SNAP))
print("FAILURES:", len(F))
