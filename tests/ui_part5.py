import os
"""Part 5: a new source (UNFI Natural, with Pack/Size/UOM) and Pack/Size/UOM
mapped onto an existing source (cs_ca), both staged and pushed from Pending
Changes; Upload & Ingest; Merge; then Department Review works on the new
source's groups (approve, undo, redo, break out)."""
import io
import time
import random
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster import ingest
import pandas as pd
import streamlit as _st
from itemmaster.db import get_engine
from sqlalchemy import text, bindparam

E = get_engine()
FAIL = []
SCR = os.path.dirname(os.path.abspath(__file__))


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def q(sql, **p):
    with E.connect() as c:
        return c.execute(text(sql), p).all()


# ---- build a UNFI Natural file: 150 items NWG also carries (evidence) + 250 only UNFI has ----
random.seed(7)
nwg = pd.read_sql(text("SELECT TOP 150 upc, department FROM dbo.raw_items WHERE source_key='nwg' AND department IN ('GROCERY','FROZEN') "
                       "ORDER BY upc"), E.connect())
cats = {"GROCERY": ("SNACKS", "CHIPS"), "FROZEN": ("FROZEN ENTREES", "BOWLS")}
rows = []
for u, d in zip(nwg["upc"], nwg["department"]):
    c, s = cats[d]
    rows.append({"UPC": u, "Item Description": f"UNFI OVERLAP {u}", "Brand": "UNFI BRAND", "Department": f"NAT {d}",
                 "Category": c, "Sub Category": s, "Pack": "12", "Size": "16", "UOM": "OZ"})
for i in range(250):
    d = "GROCERY" if i % 2 else "FROZEN"
    c, s = cats[d]
    if i % 25 == 0:
        c, s = "SPECIALTY", "MIXED BAG"
    rows.append({"UPC": f"7777{i:08d}", "Item Description": f"UNFI ONLY ITEM {i}", "Brand": f"BRAND {i % 9}",
                 "Department": f"NAT {d}", "Category": c, "Sub Category": s, "Pack": str(6 + i % 3), "Size": "12", "UOM": "OZ"})
unfi = pd.DataFrame(rows)
path = SCR + r"\UNFI Natural Test 09.24.26.xlsx"
unfi.to_excel(path, index=False, sheet_name="Items")
print(f"  UNFI test file: {len(unfi)} rows ({len(nwg)} overlap NWG)")

aj = session("aj"); run(aj, "load aj")

# ---- stage: add UNFI Natural; add Pack/Size/UOM columns to cs_ca (Apply Now) ----
cfg_unfi = {c: None for c in dm.SOURCE_CONFIG_COLUMNS}
cfg_unfi.update({
    "source_label": "UNFI Natural", "enabled": True, "priority_rank": 7, "file_keyword": "UNFI", "sheet_name": "Items",
    "header_row": 1, "upc_column": "UPC", "strip_trailing_digits": 0, "department_column": "Department",
    "category_column": "Category", "subcategory_column": "Sub Category", "brand_column": "Brand",
    "description_column": "Item Description", "pack_column": "Pack", "size_column": "Size", "uom_column": "UOM",
    "size_format": "plain", "notes": "Test source added by the full test pass",
})
dm.save_source_pending_change(E, "unfi_natural", "add", cfg_unfi, False, "AJ")
cs = dict(q("SELECT * FROM dbo.sources WHERE source_key='cs_ca'")[0]._mapping)
cfg_cs = {c: cs[c] for c in dm.SOURCE_CONFIG_COLUMNS}
cfg_cs.update({"pack_column": "Case", "size_column": "Size", "uom_column": "Unit of Measure"})
dm.save_source_pending_change(E, "cs_ca", "edit", cfg_cs, True, "AJ")
_st.cache_data.clear()
goto(aj, "Pending Changes")
body = texts(aj)
check("unfi_natural" in body.lower() or "UNFI Natural" in body, "Pending Changes lists the new source")
aj.checkbox(key="confirm_push_source_changes").check(); run(aj, "tick")
t = time.time()
aj.button(key="push_source_pending").click(); run(aj, "push source changes (cs_ca re-ingests from its stored file)")
print(f"  source push took {time.time() - t:.0f}s; msgs:", [x.value[:140] for x in list(aj.success) + list(aj.warning) + list(aj.info)][:5])
check(q("SELECT COUNT(*) FROM dbo.sources WHERE source_key='unfi_natural'")[0][0] == 1, "UNFI Natural source exists")
n_cs_size = q("SELECT COUNT(*) FROM dbo.raw_items WHERE source_key='cs_ca' AND ISNULL(size,'')<>''")[0][0]
check(n_cs_size > 1000, f"cs_ca raw rows now carry Size ({n_cs_size:,})")

# ---- Upload & Ingest for UNFI (what the tab's Save button runs) ----
goto(aj, "Upload & Ingest")
aj.selectbox(key=[s.key for s in aj.selectbox][0]).select("unfi_natural") if False else None
src = {k: v for k, v in dict(q("SELECT * FROM dbo.sources WHERE source_key='unfi_natural'")[0]._mapping).items()}


class F(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


f = F(open(path, "rb").read(), "UNFI Natural Test 09.24.26.xlsx")
raw_df = ingest.read_raw_file(f, src)
cleaned, stats, rejected = ingest.map_and_clean(raw_df, src)
print("  ingest stats:", stats)
ingest.stage_source(E, "unfi_natural", cleaned, rejected, stats, uploaded_by="AJ", original_filename=f.name)
ingest.save_raw_upload(E, "unfi_natural", raw_df, f.name, "AJ")
check(q("SELECT COUNT(*) FROM dbo.raw_items WHERE source_key='unfi_natural'")[0][0] == 400, "400 UNFI rows ingested")
check(q("SELECT COUNT(*) FROM dbo.raw_items WHERE source_key='unfi_natural' AND pack IS NOT NULL AND size IS NOT NULL AND uom IS NOT NULL")[0][0] == 400,
      "UNFI Pack/Size/UOM ingested")
_st.cache_data.clear()
goto(aj, "Merge")
check(any("unfi_natural" in w.value for w in aj.warning), "Merge tab flags UNFI's raw data as newer than the last Merge")
t = time.time()
next(b for b in aj.button if b.label == "Compute Merge").click(); run(aj, "Compute Merge")
meta = dm.get_merge_compute_meta(E)
print(f"  compute {time.time() - t:.0f}s: added={meta['added_count']} changed={meta['changed_count']} by_field={meta.get('changed_by_field')}")
check(meta["added_count"] == 250, f"draft adds the 250 UNFI-only items ({meta['added_count']})")

# something staged BEFORE the merge, to see it's handled
qq = dm.get_review_queue(E, "review")
pre = qq[qq["suggested_department"].notna()].sort_values("n_upcs_total").iloc[0]
dm.upsert_combo_suggestion(E, int(pre.combo_id), "review", pre.suggested_department, pre.source_key,
                           pre.raw_subcategory, int(pre.n_upcs_total), "Jason")

aj.checkbox(key="confirm_push_merge").check(); run(aj, "tick")
if [c for c in aj.checkbox if c.key == "confirm_override_pending_work"]:
    aj.checkbox(key="confirm_override_pending_work").check(); run(aj, "ack pending work")
CS_CA_SIZED = q("SELECT COUNT(*) FROM dbo.items WHERE source_key='cs_ca' AND size IS NOT NULL")[0][0]
t = time.time()
next(b for b in aj.button if b.label == "Push Items to Database").click(); run(aj, "Push Items to Database")
print(f"  merge push {time.time() - t:.0f}s")
check(q("SELECT COUNT(*) FROM dbo.items WHERE source_key='unfi_natural'")[0][0] == 250, "250 UNFI-only items live, source unfi_natural")
ov_win = q("SELECT COUNT(*) FROM dbo.items i JOIN dbo.raw_items r ON r.upc=i.upc AND r.source_key='unfi_natural' "
           "WHERE i.source_key='unfi_natural' AND i.upc NOT LIKE '7777%'")[0][0]
check(ov_win == 0, "overlap items keep their higher-priority source (UNFI is last)")
check(q("SELECT COUNT(*) FROM dbo.items WHERE source_key='cs_ca' AND size IS NOT NULL")[0][0] == CS_CA_SIZED,
      "existing cs_ca items keep their values — a Merge only adds items (the new Size mapping shows as 'differ in files')")
combos = q("SELECT combo_id, raw_department, raw_category, raw_subcategory, tier, decision_state, decided_department, n_upcs_total "
           "FROM dbo.dept_mapping_combos WHERE source_key='unfi_natural' ORDER BY n_upcs_total DESC")
for c in combos:
    print("   ", tuple(c))
check(len(combos) >= 3, "Department Review built groups for UNFI Natural")
auto = [c for c in combos if c.tier == "auto" and c.decided_department]
check(len(auto) >= 1, "some UNFI groups auto-decided from NWG evidence")
if auto:
    a = auto[0]
    d = q("SELECT COUNT(*) FROM dbo.items i JOIN dbo.dept_mapping_combo_upcs cu ON cu.upc=i.upc WHERE cu.combo_id=:c AND i.department=:d",
          c=a.combo_id, d=a.decided_department)[0][0]
    check(d == a.n_upcs_total, f"their items got the Department right away ({d}/{a.n_upcs_total})")
check(int(pre.combo_id) in dm.get_pending_changes(E) or any("discarded" in n["detail"] for n in dm.get_notifications(E, "Jason", pd.Timestamp("2026-01-01"))["updates"]),
      "work staged before the Merge was either kept or its owner was told it was discarded")

# ---- Department Review on the new source's groups ----
_st.cache_data.clear()
jason = session("jason"); run(jason, "load jason")
review = [c for c in combos if c.decision_state == "not_reviewed" and not c.decided_department]
if review:
    r = review[0]
    jason.session_state["dept_shared_filter"] = {"search": "unfi", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(jason, "Department Review", "Crosswalk" if r.tier == "review" else "Unmatched")
    tier = "review" if r.tier == "review" else "unmatched"
    sel = jason.selectbox(key=f"dept_choice_{tier}_{r.combo_id}")
    sel.select("GROCERY"); run(jason, "pick GROCERY")
    jason.button(key=f"approve_{tier}_{r.combo_id}").click(); run(jason, "approve a UNFI group")
    check(r.combo_id in dm.get_pending_changes(E), "UNFI group staged")
    jason.button(key="topbar_undo").click(); run(jason, "undo")
    next(b for b in jason.button if b.label == "Confirm undo").click(); run(jason, "confirm")
    check(r.combo_id not in dm.get_pending_changes(E), "undo removed it")
    jason.button(key="topbar_redo").click(); run(jason, "redo")
    next(b for b in jason.button if b.label == "Confirm redo").click(); run(jason, "confirm")
    check(r.combo_id in dm.get_pending_changes(E), "redo put it back")
else:
    print("  (no undecided UNFI group to review)")

print("FAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
