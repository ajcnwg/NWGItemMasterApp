"""Part 4: spreadsheet Add / Delete / UPC Overrides, pushed from Pending
Changes; overrides beat Department Review; a Merge draft computed BEFORE
those pushes still keeps them; Merge push end to end."""
import io
import time
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster import item_bulk
import openpyxl
import pandas as pd
from itemmaster.db import get_engine
from sqlalchemy import text, bindparam

E = get_engine()
FAIL = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def items(upcs):
    with E.connect() as c:
        return {r["upc"]: dict(r) for r in c.execute(text(
            "SELECT upc, description, department, brand, source_key FROM dbo.items WHERE upc IN :u").bindparams(
            bindparam("u", expanding=True)), {"u": list(upcs)}).mappings()}


def overrides(upcs):
    with E.connect() as c:
        return {r["upc"]: dict(r) for r in c.execute(text(
            "SELECT upc, description, department, brand, category FROM dbo.manual_overrides WHERE upc IN :u").bindparams(
            bindparam("u", expanding=True)), {"u": list(upcs)}).mappings()}


class F(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


def live_dict(upcs):
    with E.connect() as c:
        rows = c.execute(text(
            "SELECT upc, description, department, category, subcategory, brand, pack, size, uom, source_key FROM dbo.items "
            "WHERE upc IN :u").bindparams(bindparam("u", expanding=True)), {"u": list(upcs) or ["x"]}).mappings().all()
    return {r["upc"]: dict(r) for r in rows}


depts = dm.get_departments(E)["department"].tolist()
aj, jason = session("aj"), session("jason")
run(aj, "load aj"); run(jason, "load jason")

# 0. template files open and look right
for kind in ("add", "delete", "edit"):
    wb = openpyxl.load_workbook(io.BytesIO(item_bulk.template_bytes(kind, depts)))
    hdr = [c.value for c in wb["Items"][1]]
    print(f"  template {kind}: sheets={wb.sheetnames} headers={hdr}")
check(True, "templates build and open in Excel format")
goto(aj, "Add Item")
check(any(b.label.strip() == "Download the blank template" for b in aj.get("download_button")), "Add Item shows the template download")

# 1. compute a Merge draft FIRST (so we can prove later pushes survive an older draft)
goto(aj, "Merge")
t = time.time()
next(b for b in aj.button if b.label == "Compute Merge").click(); run(aj, "Compute Merge")
print(f"  compute took {time.time() - t:.0f}s")
meta = dm.get_merge_compute_meta(E)
check(meta is not None, f"draft computed ({meta and meta['item_count']} items)")

# 2. Add via spreadsheet
with E.connect() as c:
    existing = c.execute(text("SELECT TOP 1 upc FROM dbo.items WHERE source_key='kehe'")).scalar()
add_csv = (
    "UPC,Description,Department,Brand\n"
    "999000000011,TEST ADD ONE,grocery,TB\n"
    "abc,BAD UPC,,\n"
    f"{existing},ALREADY THERE,,\n"
    "999000000028,,FROZEN,\n"
    "999000000035,TEST ADD BAD DEPT,FOODZ,\n"
    "999000000011,TEST ADD DUP,,\n"
    "999000000042,TEST ADD TWO,,\n"
)
df = item_bulk.read_upload(F(add_csv.encode(), "adds.csv"))
live = live_dict([existing])
prev, ch, _decisions = item_bulk.check_upload("add", df, live, depts, dm.get_item_master_pending(E), "Jason")
print(prev.to_string(index=False))
exp = ["Ready", "Not a valid UPC", "Already in the item master", "Description is required", "Unknown Department", "Duplicate", "Ready"]
check(all(s.startswith(e) for s, e in zip(prev["Status"], exp)), "each add row gets the right status")
check(ch["999000000011"]["department"] == "GROCERY", "lower-case 'grocery' matched to GROCERY")
dm.save_item_master_pending_bulk(E, ch, "Jason")

# 3. Overrides via spreadsheet (blank = keep)
with E.connect() as c:
    g = c.execute(text("SELECT TOP 2 cu.upc FROM dbo.dept_mapping_combo_upcs cu JOIN dbo.items i ON i.upc=cu.upc "
                       "WHERE cu.combo_id=2976")).scalars().all()
    k2 = c.execute(text("SELECT TOP 1 i.upc FROM dbo.items i JOIN dbo.dept_mapping_combo_upcs cu ON cu.upc=i.upc "
                        "JOIN dbo.dept_mapping_combos c ON c.combo_id=cu.combo_id WHERE c.decision_state='not_reviewed' "
                        "AND c.tier='auto' AND i.source_key='kehe' AND i.department IS NOT NULL")).scalar()
X1, X3 = g[0], g[1]
X2 = k2
live = live_dict([X1, X2, X3])
print("  before:", {u: (live[u]["department"], live[u]["brand"]) for u in live})
ed_csv = (
    "UPC,Description,Department,Category,Subcategory,Brand,Pack,Size,UOM\n"
    f"{X1},,FROZEN,,,,,,\n"
    f"{X2},,,,,TESTBRAND,,,\n"
    f"{X3},,{live[X3]['department']},,,,,,\n"
    "999999999999,,DAIRY,,,,,,\n"
)
df = item_bulk.read_upload(F(ed_csv.encode(), "edits.csv"))
prev, ch, _decisions = item_bulk.check_upload("edit", df, live, depts, dm.get_item_master_pending(E), "Jason")
print(prev.to_string(index=False))
st_ = prev["Status"].tolist()
check(st_[0].startswith("Ready") and "Department" in st_[0] and st_[1].startswith("Ready") and "Brand" in st_[1]
      and prev["Status"].tolist()[2] == "No change" and prev["Status"].tolist()[3] == "Not in the item master",
      "override rows: only filled columns change; unchanged / unknown rows flagged")
check(ch[X2]["department"] == live[X2]["department"] and ch[X2]["brand"] == "TESTBRAND", "blank cells kept the current values")
dm.save_item_master_pending_bulk(E, ch, "Jason")

# 4. Delete via spreadsheet
with E.connect() as c:
    D1 = c.execute(text("SELECT TOP 1 upc FROM dbo.items WHERE source_key='spins' AND upc NOT IN (SELECT upc FROM dbo.item_master_pending_changes)")).scalar()
df = item_bulk.read_upload(F(f"UPC\n{D1}\n000000000\n".encode(), "del.csv"))
prev, ch, _decisions = item_bulk.check_upload("delete", df, live_dict([D1]), depts, dm.get_item_master_pending(E), "Jason")
print(prev.to_string(index=False))
check(prev["Status"].tolist()[0] == "Ready" and prev["Status"].tolist()[1].startswith("Not a valid UPC: “000000000”"),
      "delete rows checked")
dm.save_item_master_pending_bulk(E, ch, "Jason")

# 5. Jason pushes them from Pending Changes
import streamlit as _st
_st.cache_data.clear()  # the test staged directly; the UI clears this itself
jason = session("jason"); run(jason, "reload jason")
goto(jason, "Pending Changes")
jason.checkbox(key="confirm_push_item_master").check(); run(jason, "tick")
pb = jason.button(key="push_item_master_pending"); print("  ", pb.label)
pb.click(); run(jason, "push item master changes")
it = items(["999000000011", "999000000042", X1, X2, X3, D1])
check("999000000011" in it and it["999000000011"]["department"] == "GROCERY", "added item live, GROCERY")
check("999000000042" in it, "second added item live")
check(D1 not in it, "deleted item gone")
check(it[X1]["department"] == "FROZEN", "override Department live")
check(it[X2]["brand"] == "TESTBRAND" and it[X2]["department"] == live[X2]["department"], "Brand-only override: Department untouched")
ov = overrides([X1, X2])
check(ov[X1]["department"] == "FROZEN" and ov[X1]["brand"] is None, "X1 pins only Department")
check(ov[X2]["brand"] == "TESTBRAND" and ov[X2]["department"] is None, "X2 pins only Brand (Department still follows Department Review)")

# 6. Department sync (what a Department Review push runs) keeps the override on top
dm.sync_item_departments(E)
it = items([X1, X2])
check(it[X1]["department"] == "FROZEN", "sync: override still beats the group decision (GROCERY)")

# 7. push the OLDER draft from the Merge tab
aj = session("aj"); run(aj, "reload aj")
goto(aj, "Merge")
with E.connect() as c:
    n_before = c.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()
x1_group = dm.groups_for_upcs(E, [X1]).get(X1)
G_BEFORE = items(dm.get_combo_member_items(E, x1_group)["upc"].tolist()) if x1_group else {}
aj.checkbox(key="confirm_push_merge").check(); run(aj, "tick push")
if [c for c in aj.checkbox if c.key == "confirm_override_pending_work"]:
    aj.checkbox(key="confirm_override_pending_work").check(); run(aj, "tick override ack")
t = time.time()
push_btn = next(b for b in aj.button if b.label == "Push Items to Database")
if push_btn.disabled:
    # a draft with nothing new can't be pushed from the page; push it the way the button would
    check(any("Nothing new to add" in x.value for x in aj.caption), "empty draft: Push disabled with 'Nothing new to add'")
    dm.push_merge_compute(E, "AJ", is_admin=True)
else:
    push_btn.click(); run(aj, "Push Items to Database")
print(f"  merge push took {time.time() - t:.0f}s")
print("  after push:", [x.value[:160] for x in list(aj.success) + list(aj.error) + list(aj.warning)][:6])
it = items(["999000000011", "999000000042", X1, X2, D1])
with E.connect() as c:
    n_after = c.execute(text("SELECT COUNT(*) FROM dbo.items")).scalar()
check("999000000011" in it and "999000000042" in it, "added items survived pushing a draft computed before they existed")
check(D1 not in it, "deleted item stayed deleted")
check(it[X1]["department"] == "FROZEN", "override Department survived the Merge")
check(it[X2]["brand"] == "TESTBRAND", "override Brand survived the Merge")
check(n_after == n_before, f"item count consistent ({n_before} -> {n_after})")
g_after = items(list(G_BEFORE))
check(all(g_after[u]["department"] == (("FROZEN" if u == X1 else G_BEFORE[u]["department"])) for u in G_BEFORE),
      "the rest of X1's group kept its Department through the Merge")
bo = [u for u in dm.get_combo_upc_decisions(E, 318)["upc"]]
check(set(v["department"] for v in items(bo).values()) >= {"FROZEN"}, "Broken Out decisions still applied after Merge")
diff = dm._department_targets(E, "items")
norm = lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) or v == "" else v
bad = diff[[norm(a) != norm(b) for a, b in zip(diff["department"], diff["target"])]]
check(len(bad) == 0, f"after Merge every item's Department equals what the rules say ({len(bad)} off)")

print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
