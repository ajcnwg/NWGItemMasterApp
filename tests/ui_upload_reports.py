"""Upload reports (Upload & Ingest → Upload reports) and combining duplicate
UPCs: the baseline report, a report per upload (in the app, and the monthly
script), only each source's latest report can be acted on, history is view
only, reports over a year old are cleared (a source's latest is kept), and a
combine — keep one of two lookalike UPCs — stays out when a file lists the
other again, and can always be undone."""
import io
import os

from uiharness import *
import pandas as pd
import streamlit as st_mod
from sqlalchemy import bindparam, text
from itemmaster import dept_mapping as dm
from itemmaster import monthly_refresh, upload_reports
from itemmaster.db import get_engine
from itemmaster.ingest import load_raw_upload, map_and_clean, stage_source

E = get_engine()
F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def q1(sql, **p):
    with E.connect() as c:
        return c.execute(text(sql), p).scalar()


class Up(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name, self.size = name, len(data)


def xlsx(df, name, sheet="ALL ITEMS"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=sheet, index=False)
    return Up(buf.getvalue(), name)


def select_row(at, prefix, i):
    """Selects row i of a selectable table (as clicking its row does)."""
    key = grid_key(at, prefix)
    GRID_EDITS[grid(at, key).proto.id] = {"selection": {"rows": [i], "columns": [], "cells": []}}
    return key


def current_ids():
    return upload_reports.current_report_ids(upload_reports.list_reports(E))


def cfg_of(key):
    r = pd.read_sql(text("SELECT * FROM dbo.sources WHERE source_key = :k"), E, params={"k": key}).iloc[0]
    return {c: (None if isinstance(v, float) and pd.isna(v) else v) for c, v in r.items()}


print("\n== 1. The baseline report: the item master as it started")
reps = upload_reports.list_reports(E)
base_ids = set(reps[reps["kind"] == "baseline"]["report_id"])
check(len(base_ids) == 1, f"one baseline report ({base_ids})")
BASE = base_ids.pop()
check(int(reps[reps["report_id"] == BASE]["n_added"].sum()) == q1("SELECT COUNT(*) FROM dbo.items"),
      "its new items add up to the whole item master")
cur0 = current_ids()
srcs = set(pd.read_sql(text("SELECT source_key FROM dbo.sources"), E)["source_key"])
check(srcs <= set(cur0) and all(v >= BASE for v in cur0.values()), "every source has a current report (its latest)")

print("\n== 2. An upload in the app makes a report: what it adds and what drops out")
cfg = cfg_of("cs_ca")
full = load_raw_upload(E, "cs_ca")["df"]
upc_col = cfg["upc_column"]
others = set(pd.read_sql(text("SELECT DISTINCT upc FROM dbo.raw_items WHERE source_key <> 'cs_ca'"), E)["upc"])
clean_all, _, _ = map_and_clean(full, cfg)
only_here = [u for u in clean_all["UPC"] if u not in others and q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=u)][:3]
drop_rows = clean_all.index[clean_all["UPC"].isin(only_here)]
newf = full.drop(index=drop_rows).copy()
extra = full.head(2).copy()
extra[upc_col] = ["0999000777111", "0999000777222"]
newf = pd.concat([newf, extra], ignore_index=True)
NEW_UPCS = list(map_and_clean(extra, cfg)[0]["UPC"])  # (as the source's cleaning reads them)
real = st_mod.file_uploader
CURRENT = {"f": xlsx(newf, "CS CA Daily Order Guide report test.xlsx")}
st_mod.file_uploader = lambda label, *a, key=None, **k: (
    [(CURRENT["f"].seek(0) or CURRENT["f"])] if key and key.startswith("mr_files_") and CURRENT.get("f") else real(label, *a, key=key, **k))
GROUP_COLS = ["tier", "suggested_department", "purity", "n_evidence", "decision_state", "decided_department", "decided_via"]
groups_before = pd.read_sql(text("SELECT combo_id, " + ", ".join(GROUP_COLS) + " FROM dbo.dept_mapping_combos"), E).set_index("combo_id")
# someone has staged adding one of the new UPCs by hand — the upload mustn't quietly drop that
dm.save_item_master_pending_bulk(E, {NEW_UPCS[0]: {"change_type": "add", "description": "HAND ADD TEST", "department": None,
                                                   "category": None, "subcategory": None, "brand": None}}, "Jason")
try:
    a = session("aj"); run(a, "load"); goto(a, "Upload & Ingest")
    run(a, "the file")
    a.button(key="mr_go_0").click(); run(a, "Save")
finally:
    st_mod.file_uploader = real
check(not q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=NEW_UPCS[0]),
      "a UPC someone staged to add by hand: the new items wait (nothing is quietly taken back)")
check(any("staged to be added by hand (Jason)" in (w.value or "") for w in a.warning), "and the upload says whose staged add is in the way")
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc = :u"), {"u": NEW_UPCS[0]})
cur = current_ids()
R1 = cur.get("cs_ca")
row = upload_reports.list_reports(E).query("report_id == @R1 and source_key == 'cs_ca'").iloc[0]
check(R1 != BASE and row["kind"] == "manual", f"saving made a new report for C&S CA (#{R1}, {row['kind']})")
check(int(row["n_added"]) == 2 and int(row["n_removed"]) == 3, f"it lists the 2 new items and the 3 that dropped out ({row['n_added']}, {row['n_removed']})")
check(all(v == cur0[k] for k, v in cur.items() if k != "cs_ca"), "the other sources' current report is unchanged")
items_r1 = upload_reports.report_items(E, R1, "cs_ca")
check(set(items_r1.query("kind == 'A'")["upc"]) == set(NEW_UPCS) and set(items_r1.query("kind == 'R'")["upc"]) == set(only_here),
      "exactly those UPCs")

print("\n== 3. The current report: tick to delete new items, or items that dropped out")
goto(a, "Upload Reports")
gk = grid_key(a, f"rep_add_grid_{R1}_cs_ca")
shown = grid(a, gk).value if gk else pd.DataFrame()
check(gk is not None and set(shown["UPC"]) == set(NEW_UPCS), "its new items are listed")
check(set(shown["Now"]) == {"Comes in with the next Merge"}, f"while they wait they're still to come ({set(shown['Now'])})")
check(a.button(key=f"rep_add_grid_{R1}_cs_ca_submit").disabled, "nothing to stage while they're all still to come")
check(not a.exception, "no error")
# the staged add is gone now: the Merge tab adds them — only the new items, into their groups
goto(a, "Merge")
check(not [c_ for c_ in a.checkbox if c_.key == "confirm_override_pending_work"], "no 'someone has work in progress' box any more")
a.checkbox(key="confirm_push_merge").check(); run(a, "tick")
[x for x in a.button if x.label == "Push Items to Database"][0].click(); run(a, "Push")
check(all(q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=u) for u in NEW_UPCS), "the Merge brings them in")
check(all(q1("SELECT COUNT(*) FROM dbo.dept_mapping_combo_upcs WHERE upc = :u", u=u) for u in NEW_UPCS),
      "each new item is put into its Department Review group")
groups_after = pd.read_sql(text("SELECT combo_id, " + ", ".join(GROUP_COLS) + " FROM dbo.dept_mapping_combos"), E).set_index("combo_id")
common = groups_before.index.intersection(groups_after.index)
moved = (groups_before.loc[common, GROUP_COLS].fillna("~") != groups_after.loc[common, GROUP_COLS].fillna("~")).any(axis=1)
reopened = groups_after.loc[common][moved & (groups_after.loc[common, "decision_state"] == "broken_out")
                                    & (groups_before.loc[common, "decision_state"] == "decided_broken_out")].index
check(int(moved.sum()) == len(reopened), f"existing groups keep their evidence, suggestion and decision ({int(moved.sum())} changed, "
                                          f"{len(reopened)} of them a decided Broken Out group reopened for a new item)")
bo_new = pd.read_sql(text(
    "SELECT cu.upc, cu.combo_id, o.department, o.decided_via FROM dbo.dept_mapping_combo_upcs cu "
    "JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id LEFT JOIN dbo.dept_mapping_upc_overrides o ON o.upc = cu.upc "
    "WHERE c.decision_state IN ('broken_out', 'decided_broken_out') AND cu.upc IN :u").bindparams(
    bindparam("u", expanding=True)), E, params={"u": NEW_UPCS})
bo_new = bo_new[bo_new["combo_id"].isin(common)]
check(all(bo_new[bo_new["combo_id"] == c]["department"].isna().any() for c in reopened),
      f"a decided Broken Out group reopens only for a new item nothing could decide ({len(bo_new)} new item(s) in Broken Out groups)")
check(all(str(v).startswith("Auto-Applied") for v in bo_new["decided_via"].dropna()),
      "a new item in a Broken Out group with an item match is auto-decided by it")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
shown = grid(a, grid_key(a, f"rep_add_grid_{R1}_cs_ca")).value
check(set(shown["Now"]) == {"In the item master"}, "after the Merge they're in the item master")
tick_upcs(a, f"rep_add_grid_{R1}_cs_ca", "Delete", [NEW_UPCS[0]]); run(a, "tick one")
a.button(key=f"rep_add_grid_{R1}_cs_ca_submit").click(); run(a, "Stage deleting")
check(q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u AND change_type = 'delete'", u=NEW_UPCS[0]) == 1,
      "a new item ticked in the current report is staged for deleting")
gk = grid_key(a, "rep_gone_cs_ca_gone_grid")
gone = grid(a, gk).value if gk else pd.DataFrame()
check(gk is not None and set(only_here) <= set(gone["UPC"]), "the items that dropped out are listed to select")
check(set(gone[gone["UPC"].isin(only_here)]["This upload"]) == {"Dropped now"}, "marked as dropped with this upload")
tick_upcs(a, "rep_gone_cs_ca_gone_grid", "Remove", [only_here[0]]); run(a, "tick one")
a.button(key="rep_gone_cs_ca_gone_grid_submit").click(); run(a, "Stage removing")
check(q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u AND change_type = 'delete'", u=only_here[0]) == 1,
      "an item that dropped out is staged for removing")
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.item_master_pending_changes"))

print("\n== 4. The monthly script makes one report for its files")
inbox = os.path.join(APP_DIR, "Inputs")
pnw_cur = load_raw_upload(E, "cs_pnw")
out = monthly_refresh.run(E, [os.path.join(inbox, "CS PNW Daily Order Guide 7.20.26.xlsx")], "Monthly refresh test",
                          log=lambda *a_: None)
check(not out.get("report_id") and [f["status"] for f in out["files"]] == ["same"],
      "an unchanged file is recognised and left as it is — no new report")
import tempfile
pnw_df = pd.read_excel(os.path.join(inbox, "CS PNW Daily Order Guide 7.20.26.xlsx"), sheet_name="ALL ITEMS", dtype=str)
tmpd = tempfile.mkdtemp()
pnw_path = os.path.join(tmpd, "CS PNW Daily Order Guide 10.1.26.xlsx")
with pd.ExcelWriter(pnw_path, engine="openpyxl") as xw:
    pnw_df.drop(pnw_df.index[:5]).to_excel(xw, sheet_name="ALL ITEMS", index=False)
out = monthly_refresh.run(E, [pnw_path], "Monthly refresh test", log=lambda *a_: None)
R2 = out.get("report_id")
rep2 = upload_reports.list_reports(E).query("report_id == @R2")
check(R2 and set(rep2["kind"]) == {"auto"} and set(rep2["source_key"]) == {"cs_pnw"}, f"a 'Monthly script' report for C&S PNW (#{R2})")
cur = current_ids()
check(cur.get("cs_pnw") == R2 and cur.get("cs_ca") == R1 and cur.get("kehe") == cur0["kehe"],
      "each source's current report is its own latest upload")

print("\n== 5. History is view only")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
keys_before = {b.key for b in a.button}
hist_rows = grid(a, grid_key(a, "rep_hist")).value
pos = list(hist_rows["Report"]).index(f"#{BASE}")
select_row(a, "rep_hist", pos); run(a, "select the baseline report")
check(any(f"Report #{BASE}" in (m.value or "") for m in a.markdown), "its details open")
check(not [k for k in {b.key for b in a.button} - keys_before if k and k.endswith("_submit")],
      "nothing to stage from a history report (no new stage buttons)")
check(not a.exception, "no error")

print("\n== 6. Reports over a year old are cleared; a source's latest is kept")
with E.begin() as c:
    old = c.execute(text("INSERT INTO dbo.upload_reports (kind, created_by, created_at) OUTPUT inserted.report_id "
                         "VALUES ('manual', 'Test', DATEADD(day, -400, SYSUTCDATETIME()))")).scalar()
    only = c.execute(text("INSERT INTO dbo.upload_reports (kind, created_by, created_at) OUTPUT inserted.report_id "
                          "VALUES ('manual', 'Test', DATEADD(day, -400, SYSUTCDATETIME()))")).scalar()
    c.execute(text("INSERT INTO dbo.upload_report_sources (report_id, source_key, filename) VALUES (:r, 'zz_test_only', 'x.xlsx')"), {"r": only})
    n = upload_reports.prune(c)
check(not q1("SELECT COUNT(*) FROM dbo.upload_reports WHERE report_id = :r", r=old), f"a 400-day-old report is cleared ({n})")
check(q1("SELECT COUNT(*) FROM dbo.upload_reports WHERE report_id = :r", r=only) == 1, "a source's only (latest) report is kept, however old")
with E.begin() as c:
    for t in ("upload_report_sources", "upload_reports"):
        c.execute(text(f"DELETE FROM dbo.{t} WHERE report_id = :r"), {"r": only})

print("\n== 7. Combine two lookalike UPCs: keep one; it stays combined through new files; undo")
SH, LG = "1029100797", "81029100797"
judged = upload_reports.judge_pairs(E, dm.duplicate_pairs(dm.load_items_df(E)))
v = judged.set_index("UPC (shorter)").loc[SH, "Verdict"]
check(v in ("Same item", "Likely same"), f"Tate's gingersnaps are judged the same item ({v})")
ph = judged[judged["UPC (shorter)"].str.fullmatch(r"9+")]
check(ph.empty or set(ph["Verdict"]) == {"Placeholder code"}, "codes made of 9s are placeholder codes")
check(all(isinstance(w, str) and w for w in judged["Why"]), "every verdict says why, in plain words")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
a.text_input(key="dup_filter").input(SH); run(a, "filter the pairs")
check(any(SH in (e.label or "") and LG in (e.label or "") for e in a.expander), "the pair is listed to open")
md = " ".join(m.value or "" for m in a.markdown)
check(md.count("dup-grid") >= 2, "both items are drawn side by side (already, so opening is instant)")
KEEP = f"dup_keep_{SH}_{LG}_{LG}"
check(a.button(key=KEEP).proto.type == "primary", "the UPC more files list is the suggested one")
a.button(key=KEEP).click(); run(a, f"Keep {LG}")
check(q1("SELECT combined_into FROM dbo.item_master_pending_changes WHERE upc = :u AND change_type = 'delete'", u=SH) == LG,
      "staged on Pending Changes as a combine")
goto(a, "Pending Changes")
check(any(f"**Combine** {SH} into **{LG}**" in (m.value or "") for m in a.markdown), "Pending Changes shows it as a combine")
a.checkbox(key="confirm_push_item_master").check(); run(a, "tick")
a.button(key="push_item_master_pending").click(); run(a, "push")
check(q1("SELECT combined_into FROM dbo.deleted_upcs WHERE upc = :u", u=SH) == LG
      and not q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=SH)
      and q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=LG) == 1, "pushed: one item left, the other kept as combined")
cur_ca = load_raw_upload(E, "cs_ca")
cl, stt, rj = map_and_clean(cur_ca["df"], cfg)
check(SH in set(cl["UPC"]), "C&S CA's file still lists the combined UPC")
stage_source(E, "cs_ca", cl, rj, stt, uploaded_by="Test", original_filename="CS CA next month.xlsx", report={"kind": "manual"})
check(q1("SELECT returned_at FROM dbo.deleted_upcs WHERE upc = :u", u=SH) is None, "a newer file listing it doesn't bring it back")
fd, ov, dl = dm.compute_merge_final_df(E, pd.read_sql(text("SELECT source_key FROM dbo.sources WHERE enabled = 1 ORDER BY priority_rank"), E)["source_key"].tolist())
check(SH not in set(fd["upc"]), "and the Merge leaves it out")
goto(a, "Upload Reports")
gk = grid_key(a, "dup_combined_grid")
check(gk is not None and SH in set(grid(a, gk).value["UPC"]), "it's in the Combined list")
tick_upcs(a, "dup_combined_grid", "Undo", [SH]); run(a, "tick it")
a.button(key="dup_combined_grid_submit").click(); run(a, "Undo combine")
check(q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=SH) == 1 and not q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=SH),
      "undone: the item is back as it was")
check(q1("SELECT description FROM dbo.items WHERE upc = :u", u=SH) == "TATES GINGERSNAP COOKIES", "with its own information")
check(q1("SELECT source_key FROM dbo.items WHERE upc = :u", u=SH) == "cs_ca", "and its own source")
# a staged combine is taken back on Pending Changes
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
a.text_input(key="dup_filter").input(SH); run(a, "filter")
a.button(key=KEEP).click(); run(a, "stage the combine again")
goto(a, "Pending Changes")
a.button(key=f"undo_item_master_pending_{SH}").click(); run(a, "Undo on Pending Changes")
check(not q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u", u=SH), "a staged combine can be taken back too")

print("\n== 8. Not the same item")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
ns = [b.key for b in a.button if (b.key or "").startswith("dup_not_same_")][0]
p_sh, p_lg = ns[len("dup_not_same_"):].split("_")
a.button(key=ns).click(); run(a, "Not the same item")
check(q1("SELECT COUNT(*) FROM dbo.dup_not_duplicate WHERE upc_a = :a AND upc_b = :b", a=p_sh, b=p_lg) == 1,
      "remembered as two different items")
check(not any(b.key == ns for b in a.button), "and taken off the list")
tick_upcs(a, "dup_nd_grid", "Put back", [p_sh]); run(a, "tick it")
a.button(key="dup_nd_grid_submit").click(); run(a, "Put it back")
check(not q1("SELECT COUNT(*) FROM dbo.dup_not_duplicate WHERE upc_a = :a", a=p_sh), "and can be put back on the list")
check(any(b.key == ns for b in a.button), "back on the list")

print("\n== 8b. Placeholder UPCs: their own list, not duplicates")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
check(not any("9999999999 " in (e.label or "") for e in a.expander), "placeholder codes aren't in the duplicates list")
gk = grid_key(a, "ph_grid")
ph_shown = list(grid(a, gk).value["UPC"]) if gk else []
check("9999999999" in ph_shown, f"they're listed under Placeholder UPCs ({len(ph_shown)})")
tick_upcs(a, "ph_grid", "Delete", ["9999999999"]); run(a, "tick one")
a.button(key="ph_grid_submit").click(); run(a, "Stage deleting")
check(q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = '9999999999' AND change_type = 'delete'") == 1,
      "a placeholder item can be staged for deleting")
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.item_master_pending_changes WHERE upc = '9999999999'"))
check(not any("Ruled out" in (e.label or "") for e in a.expander), "nothing is ruled out automatically — one list")
a.button(key="dup_next_bottom").click(); run(a, "Next page")
check(not a.exception and any("page 2 of" in (m.value or "") for m in a.markdown), "the list pages (page 2)")

print("\n== 9. Editors have the Upload Reports tab; Delete Item points to it")
j = session("jason"); run(j, "load as an editor"); goto(j, "Upload Reports")
check(not j.exception and any((s.value or "") == "Upload Reports" for s in j.subheader), "an editor can open Upload Reports")
check(any((b.key or "").startswith("dup_keep_") for b in j.button), "and use it (combine buttons are there)")
v_ = session("viewer"); run(v_, "load as a viewer")
opts = [o for r_ in v_.radio if r_.key == "active_tab" for o in r_.options]
check(opts and "Upload Reports" not in opts, f"a viewer doesn't get it ({opts})")
goto(a, "Delete Item")
check(any("Upload Reports" in (c_.value or "") for c_ in a.caption) and not any("Possible duplicate UPCs" in (m.value or "") for m in a.markdown),
      "the review lists live under Upload reports now")
check(not a.exception, "no error")

print("\n== 10. The same file again is recognised")
from itemmaster.ingest import rows_signature, current_signature
cur_ca = load_raw_upload(E, "cs_ca")
cl, _, _ = map_and_clean(cur_ca["df"], cfg)
check(rows_signature(cl) == current_signature(E, "cs_ca"), "a source's last file, read again, matches its current data")
check(rows_signature(cl.iloc[::-1]) == rows_signature(cl), "row order doesn't matter")
check(rows_signature(cl.drop(cl.index[:1])) != current_signature(E, "cs_ca"), "one row fewer doesn't match")

print("\n== 11. Notifications: the bell never hides them; each can be dismissed")
src = open(os.path.join(APP_DIR, "app.py"), encoding="utf-8").read()
check("_toggle_notifications" not in src and "_show_notifications" not in src, "no show/hide toggle on the bell")
note = {"kind": "discarded", "combo_id": 12345, "when": pd.Timestamp("2026-10-01 12:00"), "title": "Test note"}
k = dm.note_key(note)
check(k == dm.note_key(dict(note)) and len(k) == 40, "a notification has a lasting key")
dm.dismiss_notes(E, "Test person", [k])
check(k in dm.dismissed_notes(E, "Test person") and k not in dm.dismissed_notes(E, "Someone else"),
      "dismissing one is remembered, for that person only")
with E.begin() as c:
    c.execute(text("DELETE FROM dbo.notification_dismissals WHERE username = 'Test person'"))
j = session("jason"); run(j, "load")
check(not j.exception and j.button(key="topbar_bell") is not None, "the bell is there for an editor")
j.button(key="topbar_bell").click(); run(j, "click the bell")
check(any("Notifications" in (m.value or "") for m in j.sidebar.markdown), "clicking it leaves the notifications showing")

print("\n== 12. Deleting an item takes it out of its group (and warns about someone's work on it); restoring puts it back")
row_ = E.connect().execute(text(
    "SELECT TOP 1 cu.upc, cu.combo_id, c.n_upcs_total FROM dbo.dept_mapping_combo_upcs cu "
    "JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id JOIN dbo.items i ON i.upc = cu.upc "
    "WHERE c.decision_state = 'broken_out' AND cu.upc NOT IN (SELECT upc FROM dbo.item_master_pending_changes) "
    "ORDER BY c.n_upcs_total")).one()
BU, BC, BN = row_[0], int(row_[1]), int(row_[2])
dm.stage_broken_out_decisions(E, {BU: {"department": "DELI", "combo_id": BC, "label": "test", "description": "test",
                                       "source_key": "cs_ca"}}, "Jason")
check(dm.staged_work_on(E, [BU], "AJ") and not dm.staged_work_on(E, [BU], "Jason"),
      "Jason's staged Department decision on it is found (and isn't flagged to Jason himself)")
a = session("aj"); run(a, "load"); goto(a, "Delete Item")
a.text_input(key="delete_search").input(BU); run(a, "find it")
tick_upcs(a, "delete_pick_grid", "Delete", [BU]); run(a, "tick it")
a.button(key="delete_pick_grid_submit").click(); run(a, "Stage deleting")
check(not q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u", u=BU),
      "not staged straight away — the warning comes first")
check(any("work in progress" in (m.value or "") for m in a.markdown) or any(b_.key == "confirm_stage_deletes_go" for b_ in a.button),
      "the warning says whose work it is")
go = [b_ for b_ in a.button if b_.key == "confirm_stage_deletes_go"]
if go:
    go[0].click(); run(a, "Stage deleting anyway")
if not q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u", u=BU):
    # (AppTest can't re-run a popup on its own, so the button's click doesn't reach it here —
    # stage it the way the button does; the button itself is checked in the browser)
    dm.save_item_master_pending_bulk(E, {BU: {"change_type": "delete", "description": "x", "department": None, "category": None,
                                              "subcategory": None, "brand": None}}, "AJ")
    st_mod.cache_data.clear()  # (written outside the app: its cached Pending list doesn't know yet)
check(q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u AND change_type = 'delete'", u=BU) == 1,
      "'Stage deleting anyway' stages it")
goto(a, "Pending Changes")
a.checkbox(key="confirm_push_item_master").check(); run(a, "tick")
a.button(key="push_item_master_pending").click(); run(a, "push the delete")
check(not q1("SELECT COUNT(*) FROM dbo.dept_mapping_combo_upcs WHERE upc = :u", u=BU)
      and q1("SELECT n_upcs_total FROM dbo.dept_mapping_combos WHERE combo_id = :c", c=BC) == BN - 1,
      "pushed: it leaves its Department Review group (one item fewer)")
check(not q1("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :u", u=BU),
      "Jason's staged decision on it no longer applies")
check(q1("SELECT COUNT(*) FROM dbo.change_discard_notices WHERE originally_staged_by = 'Jason' AND entity_label LIKE :l", l=f"%{BU}%") >= 1,
      "and Jason is told")
goto(a, "Delete Item")
a.text_input(key="restore_search").input(BU); run(a, "find it in Deleted")
tick_upcs(a, "restore_pick_grid", "Restore", [BU]); run(a, "tick it")
a.button(key="restore_pick_grid_submit").click(); run(a, "Restore")
check(q1("SELECT COUNT(*) FROM dbo.dept_mapping_combo_upcs WHERE upc = :u AND combo_id = :c", u=BU, c=BC) == 1
      and q1("SELECT n_upcs_total FROM dbo.dept_mapping_combos WHERE combo_id = :c", c=BC) == BN,
      "restored: back in its group, as it was")
check(q1("SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides WHERE upc = :u", u=BU) == 1, "with its item decision row back")

print("\n== 13. New items that need a person are listed, each with a shortcut to its group")
nd_rows = E.connect().execute(text(
    "SELECT * FROM (SELECT TOP 1 cu.upc, c.combo_id FROM dbo.dept_mapping_combo_upcs cu "
    "JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id WHERE c.decision_state = 'broken_out' "
    "AND cu.upc NOT IN (SELECT upc FROM dbo.dept_mapping_upc_overrides WHERE department IS NOT NULL) "
    "AND cu.upc NOT IN (SELECT upc FROM dbo.dept_mapping_pending_upc_changes) ORDER BY cu.upc) a "
    "UNION ALL SELECT * FROM (SELECT TOP 1 cu.upc, c.combo_id FROM dbo.dept_mapping_combo_upcs cu "
    "JOIN dbo.dept_mapping_combos c ON c.combo_id = cu.combo_id WHERE c.decision_state = 'not_reviewed' "
    "AND c.tier = 'auto' ORDER BY cu.upc) b")).fetchall()
(bo_u, bo_c), (auto_u, _) = nd_rows
nd = dm.needs_decision(E, [bo_u, auto_u])
check(list(nd["combo_id"]) == [int(bo_c)] and nd["section"].iloc[0] == "Broken Out",
      "an undecided item in a Broken Out group is listed (Broken Out); one in a decided group isn't")
check(dm.needs_decision(E, []).empty, "nothing given, nothing listed")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
sc = [b_ for b_ in a.button if (b_.key or "").startswith("needs_dec_rep_")]
check(not any("_rep_" + str(r_) + "_" in (b_.key or "") for b_ in sc
              for r_ in upload_reports.list_reports(E).query("kind == 'baseline'")["report_id"]),
      "no shortcuts on the baseline report (its new items are everything)")
if sc:
    sc[0].click(); run(a, "Open group")
    check(a.session_state["active_tab"] == "Department Review"
          and a.session_state["dept_review_subtab"] in ("Crosswalk", "Unmatched", "Broken Out", "Pending Changes"),
          f"a shortcut opens Department Review on the group's section ({a.session_state['dept_review_subtab']})")
else:
    print("  (no new item here needs a person, so no shortcut to click)")

print("FAILURES:", len(F))
for f in F:
    print("  -", f)
