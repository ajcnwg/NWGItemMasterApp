import io
from uiharness import *
import streamlit as st_mod
F = []
def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok: F.append(msg)
a = session("aj"); run(a, "admin load"); goto(a, "Department Review", "Settings")
heads = [m.value for m in a.main.markdown if m.value.startswith("####")]
print("  Settings sections:", heads)
check(heads and heads[-1] == "#### Excel department workbook", "one Excel workbook section, at the very bottom")
check(not any("workbook" in (e.label or "").lower() for e in a.expander), "no dropdowns for it")
dl = [d.proto.label for d in a.get("download_button")]
check("Download the app as a workbook" in dl, f"download button there ({dl})")
check(any((u.proto.label or "").startswith("Upload a department workbook") for u in a.get("file_uploader")), "upload right below it")
WB = __import__('testbase').V2
data = open(WB, "rb").read(); real = st_mod.file_uploader
st_mod.file_uploader = lambda label, *x, key=None, **k: (type("U", (io.BytesIO,), {"name": "wb.xlsx"})(data) if key and key.startswith("owi_file_") else real(label, *x, key=key, **k))
run(a, "upload (preview only)")
st_mod.file_uploader = real
m = {x.label: x.value for x in a.metric}
check(m.get("To stage") == "81" and m.get("Moves to make") == "3", f"from #0, the V2 workbook's report shows 81 to stage (22 groups + 59 items) and 3 moves ({m})")
check(len(a.exception) == 0 and not any("Something went wrong" in x.value for x in a.markdown), "no errors")
print("FAILURES:", len(F))
