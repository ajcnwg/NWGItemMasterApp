"""Bad input is turned away with a plain message, and nothing is saved:
source files (unreadable, missing columns, wrong sheet, empty, no usable UPCs,
a name for the wrong source), the monthly all-at-once upload (unknown names,
two sources in one name, two usable files for one source, a rejected file
never blocking a good one), typed and spreadsheet UPCs, Item Master edits
surviving a filter change, and a source edit that doesn't fit its last file."""
import io

from uiharness import *
import pandas as pd
import streamlit as st_mod
from sqlalchemy import text
from itemmaster import dept_mapping as dm
from itemmaster import item_bulk, monthly_refresh
from itemmaster.db import get_engine
from itemmaster.ingest import FileProblem, load_raw_upload, map_and_clean, read_raw_file

E = get_engine()
F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


class Up(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name, self.size = name, len(data)


def xlsx(df, name, sheet="ALL ITEMS"):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=sheet, index=False)
    return Up(buf.getvalue(), name)


def cfg_of(key):
    r = pd.read_sql(text("SELECT * FROM dbo.sources WHERE source_key = :k"), E, params={"k": key}).iloc[0]
    return {c: (None if isinstance(v, float) and pd.isna(v) else v) for c, v in r.items()}


cfg = cfg_of("cs_ca")
base = load_raw_upload(E, "cs_ca")["df"].head(150).copy()
no_upcs = base.copy(); no_upcs[cfg["upc_column"]] = "n/a"
BAD = {
    "fake": Up(b"UPC,Description\n1,a\n", "CS CA fake.xlsx"),
    "missing": xlsx(base.drop(columns=[cfg["description_column"], cfg["brand_column"]]), "CS CA missing columns.xlsx"),
    "sheet": xlsx(base, "CS CA wrong sheet.xlsx", sheet="Sheet1"),
    "empty": xlsx(base.head(0), "CS CA headers only.xlsx"),
    "no_upcs": xlsx(no_upcs, "CS CA no upcs.xlsx"),
    "not_sheet": Up(b"hello", "notes.txt"),
}
GOOD = xlsx(base, "CS CA Daily Order Guide test.xlsx")  # (a small file is fine — size is never a reason to turn one away)


def reason(f):
    f.seek(0)
    try:
        raw = read_raw_file(f, cfg)
        cleaned, stats, _ = map_and_clean(raw, cfg)
        return None if stats["rows_staged"] else "no usable rows"
    except FileProblem as e:
        return str(e)


print("\n== 1. Reading a source file: every problem is a plain FileProblem")
r = reason(BAD["fake"]); check(r and "isn't a readable Excel file" in r, f"a non-Excel .xlsx: {r}")
r = reason(BAD["missing"]); check(r and "missing 2 column" in r and "Item Description" in r and "Trade Brand" in r, f"missing columns named: {r}")
r = reason(BAD["sheet"]); check(r and "no sheet named" in r and "Sheet1" in r, f"wrong sheet, and the sheets it has: {r}")
r = reason(BAD["empty"]); check(r and "no rows under its header row" in r, f"headers only: {r}")
r = reason(BAD["not_sheet"]); check(r and "isn't a spreadsheet" in r, f"not a spreadsheet: {r}")
check(reason(BAD["no_upcs"]) == "no usable rows", "no usable UPCs: read, but nothing to keep")
check(reason(GOOD) is None, "the real layout reads fine")
sci = base.copy(); sci[cfg["upc_column"]] = ["7.06128E+11"] * len(sci)
r = reason(xlsx(sci, "CS CA sci.xlsx")); check(r and "scientific notation" in r, f"UPCs Excel shortened (7.06E+11): {r}")
lett = base.copy(); lett.loc[lett.index[:2], cfg["upc_column"]] = ["12AB34", "UPC-X1"]
f_ = xlsx(lett, "CS CA letters.xlsx"); f_.seek(0)
_, st_, _ = map_and_clean(read_raw_file(f_, cfg), cfg)
check(st_["letter_upcs"] == 2 and st_["rows_staged"] > 0, f"UPCs with letters are counted and left out, the rest kept ({st_['letter_upcs']})")

print("\n== 2. Upload & Ingest, one file: a bad file is turned away with why, and nothing to save")
real = st_mod.file_uploader
CURRENT = {}
st_mod.file_uploader = lambda label, *a, key=None, **k: (
    [(CURRENT["f"].seek(0) or CURRENT["f"])] if key and key.startswith("mr_files_") and CURRENT.get("f") else real(label, *a, key=key, **k))
try:
    a = session("aj"); run(a, "load"); goto(a, "Upload & Ingest")
    for k in ("fake", "missing", "sheet", "empty", "no_upcs"):
        CURRENT["f"] = BAD[k]; run(a, f"upload {BAD[k].name}")
        errs = [e.value for e in a.error]
        saves = [b for b in a.button if (b.label or "").startswith("Save ") and not b.disabled]
        check(bool(errs) and not saves and not a.exception, f"{k}: rejected with a message, nothing to save ({errs[:1]})")
    CURRENT["f"] = GOOD; run(a, "upload the good file")
    saves = [b for b in a.button if (b.label or "").startswith("Save 1 file")]
    check(bool(saves) and not a.error, "the good file: preview and a Save button")
    check(any("No new items" in c.value for c in a.caption), "a file with nothing new says so")
    fresh = base.head(5).copy(); fresh[cfg["upc_column"]] = ["990000000001", "990000000002", *base[cfg["upc_column"]].head(3)]
    NEWF = xlsx(fresh, "CS CA with 2 new.xlsx"); CURRENT["f"] = NEWF; run(a, "a file with 2 new UPCs")
    lists = [d.value for d in a.dataframe if hasattr(d.value, "columns") and list(d.value.columns[:2]) == ["UPC", "Description"]]
    check(lists and sorted(lists[0]["UPC"]) == ["990000000001", "990000000002"], "exactly its 2 new items are listed")
    check(any(m.label == "New items" and m.value == "2" for m in a.metric), "the New items figure is 2")
    check(not any("replaces" in (b.label or "") for b in a.button), "Save no longer says it replaces anything")
    full = load_raw_upload(E, "cs_ca")["df"]
    gone3 = full.drop(full.index[100:103])
    CURRENT["f"] = xlsx(gone3, "CS CA missing 3.xlsx"); run(a, "a file missing 3 of the current UPCs")
    exps = [e.label for e in a.expander if (e.label or "").startswith("Not in this file")]
    check(exps and "· 3 " in exps[0], f"the 3 no longer in the file are listed for comparison ({exps[:1]})")
    check(not any("_gone_grid" in (b.key or "") for b in a.button),
          "the preview only lists them — removing is done on the Upload Reports tab (tested in ui_upload_reports)")
    wrong = xlsx(base, "SPINS weekly.xlsx"); CURRENT["f"] = wrong; run(a, "a C&S CA file named for SPINS")
    check(any("won't be saved" in e.value for e in a.error), "read as SPINS (its name), it doesn't fit — turned away with why")
    pick = [s for s in a.selectbox if s.label == "Which source?"]
    check(bool(pick), "and its source can be picked by hand")
    pick[0].select("cs_ca"); run(a, "pick C&S CA for it")
    check(any("looks like it's for" in w.value for w in a.warning) and any((b.label or "").startswith("Save 1 file") for b in a.button),
          "picked as C&S CA it's ready — with a note that its name says SPINS")
finally:
    st_mod.file_uploader = real

print("\n== 3. Monthly all-at-once: each file's own reason; a rejected file never blocks a good one")
sources = monthly_refresh.load_sources(E)
reps = {f.name: monthly_refresh.check_file(E, (f.seek(0) or f), sources) for f in
        (GOOD, BAD["fake"], BAD["missing"], xlsx(base, "random export.xlsx"), xlsx(base, "CS CA and KEHE.xlsx"))}
check(reps[GOOD.name]["status"] == "ready", "the good C&S CA file is ready")
check(reps["CS CA fake.xlsx"]["status"] == "error" and "readable Excel" in reps["CS CA fake.xlsx"]["note"], "fake → rejected with why")
check(reps["CS CA missing columns.xlsx"]["status"] == "error" and "missing" in reps["CS CA missing columns.xlsx"]["note"], "missing columns → rejected with why")
check(reps["random export.xlsx"]["status"] == "skipped", "a name with no source keyword is skipped")
check("more than one source" in reps["CS CA and KEHE.xlsx"]["note"], "a name with two sources' keywords is skipped")
UP = {}
st_mod.file_uploader = lambda label, *a, key=None, **k: (
    [(f.seek(0) or f) for f in UP["files"]] if key and key.startswith("mr_files_") and UP.get("files") else real(label, *a, key=key, **k))
try:
    a = session("aj"); run(a, "load"); goto(a, "Upload & Ingest")
    UP["files"] = [GOOD, BAD["fake"], BAD["missing"], xlsx(base, "random export.xlsx")]; run(a, "drop 4 files")
    ing = [b for b in a.button if (b.label or "").startswith("Save ")]
    check(ing and ing[0].label.startswith("Save 1 file") and not ing[0].disabled, f"the good file can still be saved ({ing[0].label if ing else None})")
    tbl = [d.value for d in a.dataframe if hasattr(d.value, "columns") and "New items" in d.value.columns]
    check(bool(tbl) and int(tbl[0].loc[tbl[0]["File"] == GOOD.name, "New items"].iloc[0]) == 0, "the files table has a New items column")
    errs = [e.value for e in a.error]
    check(sum("won't be saved" in e for e in errs) == 2 and not any("more than one file" in e for e in errs)
          and any("doesn't say which source" in (m.value or "") for m in a.markdown),
          f"the 2 bad files say why, and the unnamed one asks for its source: {len(errs)} message(s)")
    UP["files"] = [GOOD, xlsx(base, "CS CA Daily Order Guide copy.xlsx")]; run(a, "two good files for one source")
    ing = [b for b in a.button if b.key and b.key.startswith("mr_go_")]
    check(ing and ing[0].label == "Nothing to save" and ing[0].disabled, "two usable files for one source: neither is taken")
finally:
    st_mod.file_uploader = real
logs = []
out = monthly_refresh.run(E, [(GOOD.seek(0) or GOOD), xlsx(base, "CS CA Daily Order Guide copy.xlsx")], "Test", log=logs.append)
check(all(f["status"] == "error" for f in out["files"]) and "draft" not in out,
      "the unattended script holds both back too (and ingests nothing)")

print("\n== 4. UPCs people type or paste — the same cleaning as source files; letters make it invalid")
from itemmaster.ingest import clean_upc, generic_clean_upc, INVALID_UPC
check(clean_upc("12AB34") == INVALID_UPC and generic_clean_upc("12AB34") == INVALID_UPC and clean_upc("0 1234-5678") == "12345678",
      "one cleaning rule for every UPC: letters make it invalid; spaces and dashes are stripped")
a = session("aj"); run(a, "load"); goto(a, "Add Item")
existing = E.connect().execute(text("SELECT TOP 1 upc FROM dbo.items")).scalar()
for typed, want in (("12AB34", "digits only"), (existing, "already in the item master"), ("0", "isn't a valid UPC"),
                    ("7.06128E+11", "digits only")):
    f = [t for t in a.text_input if t.label == "UPC *"][0]; f.input(typed)
    [t for t in a.text_input if t.label == "Description *"][0].input("CHECK TEST")
    [b for b in a.button if b.label == "Add Item"][0].click(); run(a, f"Add Item with UPC {typed}")
    errs = [e.value for e in a.error]
    check(any(want in e for e in errs), f"{typed!r} → {errs[:1]}")
    check([t for t in a.text_input if t.label == "UPC *"][0].value == typed, f"{typed!r}: what was typed is still in the form")
with E.connect() as c:
    check(c.execute(text("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE staged_by = 'AJ' AND description = 'CHECK TEST'")).scalar() == 0,
          "none of them was staged")
prev, _, _ = item_bulk.check_upload("add", pd.DataFrame([{"upc": "12AB34", "description": "x"}, {"upc": "7.06128E+11", "description": "y"}]),
                                    {}, [], {}, "AJ")
check("Not a valid UPC" in prev["Status"][0], f"spreadsheet: letters in a UPC ({prev['Status'][0]})")
check("Excel shortened" in prev["Status"][1], f"spreadsheet: a UPC Excel turned into 7.06E+11 ({prev['Status'][1]})")

for tab, sub, key in (("UPC Overrides", None, "upc_override_search"), ("Department Review", "Crosswalk", None)):
    a = session("aj"); run(a, "load"); goto(a, tab, sub)
    box = a.text_input(key=key) if key else [t for t in a.text_input if (t.key or "").endswith("_search")][0]
    box.input("(bad [search*"); run(a, f"{tab}: search with ( [ *")
    check(not a.exception, f"{tab}: a search with ( [ * is read as plain text, no error")

print("\n== 5. Item Master: an unstaged edit survives a filter change, and Discard clears it")
a = session("aj"); run(a, "load"); goto(a, "Item Master")
key = next(el.key for el in walk(a.main) if getattr(el, "type", "") == "dataframe" and (el.key or "").startswith("items_editor_"))
edit_grid(a, key, {0: {"Category": "INPUT CHECK TEST"}}); run(a, "edit a cell")
bar = lambda: " ".join(m.value for m in a.markdown if "edited row" in m.value)
check("1 edited row" in bar(), f"the edit bar counts it ({bar()!r})")
[t for t in a.text_input if t.label.startswith("Search description")][0].input("zzzz-nothing"); run(a, "search for something else")
check("1 edited row" in bar(), "still counted after the search changed")
[b for b in a.button if b.label == "Discard"][0].click(); run(a, "Discard")
check("edited row" not in bar(), "Discard clears it")

print("\n== 6. A source edit that doesn't fit its last file")
a = session("aj"); run(a, "load"); goto(a, "Sources")
pos = [i for i, k in enumerate(pd.read_sql(text("SELECT source_key FROM dbo.sources ORDER BY priority_rank"), E)["source_key"]) if k == "cs_ca"][0]
edit_grid(a, "sources_editor", {pos: {"upc_column": "UPC NUMBER", "Apply Now": True}}); run(a, "point UPC Column at a column the file doesn't have")
[b for b in a.button if b.label == "Stage Source Changes"][0].click(); run(a, "Stage")
check(any("has no UPC" in w.value for w in a.warning), "staging says its last file has no such column")
goto(a, "Pending Changes")
a.checkbox(key="confirm_push_source_changes").check(); run(a, "tick")
[b for b in a.button if (b.label or "").startswith("Push 1 Source")][0].click(); run(a, "Push")
check(any("not pushed" in w.value for w in a.warning), "Push holds it back and says why")
check(cfg_of("cs_ca")["upc_column"] == cfg["upc_column"], "the live settings are unchanged")
check("cs_ca" in dm.get_source_pending_changes(E), "it's still on Pending Changes")
dm.delete_source_pending_change(E, "cs_ca")

print("\n== 7. Settings → Add a Department")
a = session("aj"); run(a, "load"); goto(a, "Department Review", "Settings")
before = pd.read_sql(text("SELECT department FROM dbo.dept_mapping_departments"), E)["department"].tolist()
for typed, want in (("", "Type the new Department"), ("grocery", "already in the list"), ("BAD<DEPT>", "letters, numbers"),
                    ("X" * 70, "too long")):
    a.text_input(key="new_department_input").input(typed); run(a, f"type {typed[:12]!r}")
    a.button(key="add_department_btn").click(); run(a, "Add")
    msgs = [m.value for m in list(a.error) + list(a.info)]
    check(any(want in m for m in msgs), f"{typed[:12]!r} → {msgs[:1]}")
after = pd.read_sql(text("SELECT department FROM dbo.dept_mapping_departments"), E)["department"].tolist()
check(sorted(before) == sorted(after), "none of them was added")

print("\n== 8. Items that stopped appearing: counted per upload, removable only when no file has them")
from itemmaster.ingest import load_upc_seen, stage_source
orig = load_raw_upload(E, "cs_ca")
drop3 = orig["df"].drop(orig["df"].index[100:103])
gone3 = set(map_and_clean(orig["df"], cfg)[0]["UPC"]) - set(map_and_clean(drop3, cfg)[0]["UPC"])
def ingest(df, name, new_file=True):
    cl, stt, rj = map_and_clean(df, cfg)
    stage_source(E, "cs_ca", cl, rj, stt, uploaded_by="Test", original_filename=name, new_file=new_file)
def missed():
    s_ = load_upc_seen(E, "cs_ca"); return dict(zip(s_["upc"], s_["missed_uploads"]))
ingest(drop3, "month 2.xlsx"); m = missed()
check(all(m[u] == 1 for u in gone3), f"a file without 3 of them: each has missed 1 upload ({[m[u] for u in gone3]})")
ingest(drop3, "month 2.xlsx", new_file=False); m = missed()
check(all(m[u] == 1 for u in gone3), "re-reading the same file (Apply immediately) doesn't count as a missed upload")
ingest(drop3, "month 3.xlsx"); m = missed()
check(all(m[u] == 2 for u in gone3), "the next month's file without them: 2")
a = session("aj"); run(a, "load"); goto(a, "Upload Reports")
others = set()
for k in pd.read_sql(text("SELECT source_key FROM dbo.sources"), E)["source_key"]:
    if k != "cs_ca":
        others |= set(pd.read_sql(text("SELECT upc FROM dbo.raw_items WHERE source_key = :k"), E, params={"k": k})["upc"])
only_here = {u for u in gone3 if u not in others}
listed = set()
for el in walk(a.main):
    if getattr(el, "type", "") == "dataframe" and (el.key or "").startswith("stale_gone_grid"):
        listed = set(el.value["UPC"])
check(listed == only_here, f"Upload reports lists exactly the ones no file has ({len(only_here)}), not ones another source still has")
ingest(orig["df"], orig["filename"]); m = missed()
check(all(m[u] == 0 for u in gone3), "back in the file: missed resets to 0")

print("\n== 9. Deleted items: restore by hand, or a newer file brings them back (checked against how they were)")


def stage_and_push_delete(at, upcs):
    for u in upcs:
        goto(at, "Delete Item")
        at.text_input(key="delete_search").input(u); run(at, f"find {u}")
        check(tick_upcs(at, "delete_pick_grid", "Delete", [u]) == [u] and list(grid(at, grid_key(at, "delete_pick_grid")).value["UPC"])[0] == u,
              f"searching a whole UPC lists that item first ({u})")
        run(at, "tick it")
        at.button(key="delete_pick_grid_submit").click(); run(at, "Stage deleting")
    goto(at, "Pending Changes")
    at.checkbox(key="confirm_push_item_master").check(); run(at, "tick")
    pb = at.button(key="push_item_master_pending")
    print(f"     (push button: {pb.label!r}, disabled={pb.disabled})")
    pb.click(); run(at, "push the delete(s)")
    got = [u for u in upcs if q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=u)]
    check(got == list(upcs), f"typing each whole UPC deletes exactly those items ({got})")


def q1(sql, **p):
    with E.connect() as c:
        return c.execute(text(sql), p).scalar()


picks = [r[0] for r in E.connect().execute(text(
    "SELECT TOP 4 i.upc FROM dbo.items i JOIN dbo.raw_items r ON r.upc = i.upc AND r.source_key = 'cs_ca' "
    "WHERE i.source_key = 'cs_ca' AND i.department IS NOT NULL AND i.upc NOT IN (SELECT upc FROM dbo.manual_overrides) "
    "ORDER BY i.upc")).all()]
A, B, C, D = picks
saved_A = q1("SELECT department FROM dbo.items WHERE upc = :u", u=A)
a = session("aj"); run(a, "load")

# A: deleted, restored by hand before any new file
stage_and_push_delete(a, [A])
check(q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=A) == 1, f"deleted ({A})")
goto(a, "Delete Item")
check(not any("came back from a file" in m.value for m in a.markdown), "nothing 'came back' just because the file it was in still lists it")
a.text_input(key="restore_search").input(A); run(a, "search Deleted")
tick_upcs(a, "restore_pick_grid", "Restore", [A]); run(a, "tick it")
a.button(key="restore_pick_grid_submit").click(); run(a, "Restore")
check(q1("SELECT department FROM dbo.items WHERE upc = :u", u=A) == saved_A and not q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=A),
      "restored by hand: back straight away, as it was")

# B, C, D: deleted; C and D had different information when deleted (as if edited first)
stage_and_push_delete(a, [B, C, D])
alt = {}
for u in (C, D):
    cur_d = q1("SELECT department FROM dbo.deleted_upcs WHERE upc = :u", u=u)
    alt[u] = "FROZEN" if cur_d == "DELI" else "DELI"
    with E.begin() as c:
        c.execute(text("UPDATE dbo.deleted_upcs SET department = :d WHERE upc = :u"), {"d": alt[u], "u": u})
# the upload preview says the file lists them
CURRENT2 = {"f": xlsx(load_raw_upload(E, "cs_ca")["df"], "CS CA weekly.xlsx")}
st_mod.file_uploader = lambda label, *a_, key=None, **k: (
    [(CURRENT2["f"].seek(0) or CURRENT2["f"])] if key and key.startswith("mr_files_") else real(label, *a_, key=key, **k))
try:
    goto(a, "Upload & Ingest")
    run(a, "upload a file that lists the deleted items")
    labs = [e.label for e in a.expander]
    check(any((e or "").startswith("Deleted items this file lists · 3") for e in labs),
          f"the upload preview says this file lists 3 deleted items, which come back with it ({labs[:4]})")
finally:
    st_mod.file_uploader = real
# a newer file is saved, then merged
cur = load_raw_upload(E, "cs_ca")
cl, stt, rj = map_and_clean(cur["df"], cfg)
stage_source(E, "cs_ca", cl, rj, stt, uploaded_by="Test", original_filename="CS CA newer file.xlsx")
check(q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE returned_at IS NOT NULL AND upc IN (:b, :c, :d)", b=B, c=C, d=D) == 3,
      "saving a newer file that lists them marks them as coming back")
fd, ov, dl = dm.compute_merge_final_df(E, pd.read_sql(text("SELECT source_key FROM dbo.sources WHERE enabled = 1 ORDER BY priority_rank"), E)["source_key"].tolist())
check(all(u in set(fd["upc"]) for u in (B, C, D)), "the Merge no longer leaves them out")
dm.save_merge_compute(E, fd, "AJ", ov, dl)
res = dm.push_merge_compute(E, "AJ", is_admin=True)
check(all(q1("SELECT COUNT(*) FROM dbo.items WHERE upc = :u", u=u) == 1 for u in (B, C, D)), "after the push they're back in the item master")
check(not q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=B), "B came back the same as it was deleted: its deleted record is cleared")
goto(a, "Delete Item")
msgs = [m.value for m in a.markdown]
check(any("came back from a file — different" in m for m in msgs), "C and D came back different: listed on Delete Item")
cards = [el for el in walk(a.main) if getattr(el, "type", "") == "dataframe" and "When deleted" in getattr(el.value, "columns", [])]
check(len(cards) == 2 and all("Department" in set(cd.value["Field"]) for cd in cards), "each shows its Department: when deleted vs now")
a.checkbox(key=f"returned_pick_{C}").check(); run(a, "tick C: put the deleted info back")
a.button(key="returned_apply").click(); run(a, "Use the deleted info")
staged = q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u AND department = :d", u=C, d=alt[C]) + \
    q1("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE upc = :u AND department = :d", u=C, d=alt[C])
check(staged == 1, "C: the deleted Department is staged on Pending Changes (override or Broken Out decision)")
check(not q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=C), "C: done — its deleted record is cleared")
a.button(key="returned_keep").click(); run(a, "keep D as it came back")
check(not q1("SELECT COUNT(*) FROM dbo.deleted_upcs WHERE upc = :u", u=D)
      and not q1("SELECT COUNT(*) FROM dbo.item_master_pending_changes WHERE upc = :u", u=D), "D: kept as it came back, nothing staged")

print("\n== 10. Possible duplicate UPCs (a file that drops a UPC's first digit)")
# the pieces, on made-up UPCs
check(dm.lookalike_upcs(["1029100797", "81029100797", "555"], {"81029100797", "1029100797"}) ==
      {"1029100797": "81029100797", "81029100797": "1029100797"}, "lookalike_upcs: a digit more or fewer at the front")
check(dm.lookalike_upcs(["0123456789"], {"123456789"}) == {}, "lookalike_upcs: a leading 0 isn't read as a dropped digit")
toy = pd.DataFrame({"UPC": ["1029100797", "81029100797", "99999999999", "123456789012"],
                    "Description": ["TATES GINGERSNAP", "TATES GINGERSNAP BOX", "OTHER", "X"],
                    "SourceKey": ["cs_ca", "kehe", "kehe", "nwg"], "Brand": ["TATES", "TATES", None, None]})
dp = dm.duplicate_pairs(toy)
check(list(zip(dp["UPC (shorter)"], dp["UPC (longer)"])) == [("1029100797", "81029100797")], "duplicate_pairs finds exactly the pair")
big = pd.Series([str(10**10 + i) for i in range(50000)])
vals = set(big.iloc[::3])
check(dm.isin_fast(big, vals).equals(big.isin(vals)), "isin_fast gives the same answer as isin")

# (the duplicate review itself is tested in ui_upload_reports)

print("FAILURES:", len(F))
for f in F:
    print("  -", f)
