"""Round 2 — Sources (edit a row, add a new source, take back, push), items
(Add / Delete / UPC override / take back / push / restore), a manual upload
of one source and a monthly refresh of all of the month's files, each
followed by a Merge push; the Activity report records all of it."""
import io
import random
from uiharness import *
from testbase import BASE
import pandas as pd
import streamlit as st_mod
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from itemmaster.ingest import load_raw_upload, map_and_clean
from sqlalchemy import text

E = get_engine(); F = []
real_uploader = st_mod.file_uploader
UPLOAD = {}  # label prefix / key prefix -> file object(s)


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def clean(at, where):
    bad = [x.value for x in at.markdown if "Something went wrong" in x.value]
    check(len(at.exception) == 0 and not bad, f"{where}: no errors")


class Up(io.BytesIO):
    def __init__(self, data, name):
        super().__init__(data)
        self.name, self.size = name, len(data)

    def getvalue(self):
        return super().getvalue()


def fake_uploader(label, *a, key=None, **k):
    for pref, f in UPLOAD.items():
        if (key and key.startswith(pref)) or label.startswith(pref):
            if isinstance(f, list):
                return [Up(x.getvalue(), x.name) for x in f]
            return Up(f.getvalue(), f.name)
    return real_uploader(label, *a, key=key, **k)


st_mod.file_uploader = fake_uploader


def q(sql, **p):
    with E.connect() as c:
        return c.execute(text(sql), p).all()


def src_cfg(key):
    r = pd.read_sql(text("SELECT * FROM dbo.sources WHERE source_key = :k"), E, params={"k": key}).iloc[0]
    return {c: (None if pd.isna(v) else v) for c, v in r.items()}


def file_with_new_items(key, n_new, name):
    """The source's last file as read, plus n_new new items (copies of its
    first rows with fresh UPCs), as an .xlsx laid out like the real one."""
    cfg, up = src_cfg(key), load_raw_upload(E, key)
    df = up["df"].copy()
    existing = {r[0] for r in q("SELECT upc FROM dbo.items")}
    new_rows, new_upcs = [], []
    rnd = random.Random(key)
    while len(new_rows) < n_new:
        u = "99" + "".join(rnd.choice("0123456789") for _ in range(8))
        if u in existing or u in new_upcs:
            continue
        r = df.iloc[len(new_rows)].copy()
        r[cfg["upc_column"]] = u
        new_rows.append(r); new_upcs.append(u)
    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
    cleaned, _, _ = map_and_clean(df, cfg)
    got = set(cleaned["UPC"]) & set(new_upcs)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name=(cfg["sheet_name"] or "Sheet1").split(",")[0], index=False,
                    startrow=int(cfg["header_row"] or 1) - 1)
    return Up(buf.getvalue(), name), sorted(got)


def merge_push(a):
    goto(a, "Merge")
    b = [x for x in a.button if x.label == "Compute Merge"]
    if b:
        b[0].click(); run(a, "Compute Merge")
    ack = [c for c in a.checkbox if c.key == "confirm_override_pending_work"]
    if ack:
        ack[0].check()
    a.checkbox(key="confirm_push_merge").check(); run(a, "tick")
    [x for x in a.button if x.label == "Push Items to Database"][0].click(); run(a, "Push the Merge")


SRCS = "('cs_ca', 'cs_pnw')"


def backup_raw():
    """Snapshots don't hold the raw distributor files — keep these two
    sources' raw rows / stored files / upload log to put back afterwards."""
    with E.begin() as c:
        if not c.execute(text("SELECT OBJECT_ID('dbo.zz_bak_raw_items')")).scalar():
            c.execute(text(f"SELECT * INTO dbo.zz_bak_raw_items FROM dbo.raw_items WHERE source_key IN {SRCS}"))
            c.execute(text(f"SELECT * INTO dbo.zz_bak_raw_uploads FROM dbo.source_raw_uploads WHERE source_key IN {SRCS}"))
            c.execute(text("SELECT MAX(id) AS log0 INTO dbo.zz_bak_log0 FROM dbo.ingestion_log"))


def restore_raw():
    with E.begin() as c:
        if not c.execute(text("SELECT OBJECT_ID('dbo.zz_bak_raw_items')")).scalar():
            return
        log0 = c.execute(text("SELECT log0 FROM dbo.zz_bak_log0")).scalar()
        c.execute(text(f"DELETE FROM dbo.raw_items WHERE source_key IN {SRCS}"))
        c.execute(text("INSERT INTO dbo.raw_items SELECT * FROM dbo.zz_bak_raw_items"))
        c.execute(text(f"DELETE FROM dbo.source_raw_uploads WHERE source_key IN {SRCS}"))
        c.execute(text("INSERT INTO dbo.source_raw_uploads SELECT * FROM dbo.zz_bak_raw_uploads"))
        c.execute(text("DELETE FROM dbo.ingestion_rejected_rows WHERE log_id > :l"), {"l": log0})
        c.execute(text("DELETE FROM dbo.ingestion_log WHERE id > :l"), {"l": log0})
        c.execute(text("DELETE FROM dbo.merge_added_items"))
        for t in ("zz_bak_raw_items", "zz_bak_raw_uploads", "zz_bak_log0"):
            c.execute(text(f"DROP TABLE dbo.{t}"))
    print("  raw data for C&S CA / PNW put back:", q(f"SELECT source_key, COUNT(*) FROM dbo.raw_items WHERE source_key IN {SRCS} GROUP BY source_key"))


RAW0 = None
try:
    dm.restore_snapshot(E, BASE, "AJ")
    backup_raw()
    RAW0 = q(f"SELECT source_key, COUNT(*), MAX(loaded_at) FROM dbo.raw_items WHERE source_key IN {SRCS} GROUP BY source_key")
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.activity_log"))
    a = session("aj"); run(a, "AJ signs in")
    j = session("jason"); run(j, "Jason signs in")

    print("\n== 1. Sources: edit a row, take it back, stage again, push")
    goto(a, "Sources")
    order = pd.read_sql(text("SELECT source_key FROM dbo.sources ORDER BY priority_rank"), E)["source_key"].tolist()
    pos = order.index("cs_ca")
    edit_grid(a, "sources_editor", {pos: {"notes": "R2 test note"}}); run(a, "edit notes")
    [x for x in a.button if x.label == "Stage Source Changes"][0].click(); run(a, "Stage Source Changes")
    GRID_EDITS.clear()
    pend = dm.get_source_pending_changes(E)
    check(pend.get("cs_ca", {}).get("notes") == "R2 test note", "staged, not applied yet")
    check(src_cfg("cs_ca")["notes"] != "R2 test note", "dbo.sources unchanged until pushed")
    goto(a, "Pending Changes")
    a.button(key="undo_source_pending_cs_ca").click(); run(a, "take it back")
    check("cs_ca" not in dm.get_source_pending_changes(E), "taken back")
    goto(a, "Sources")
    edit_grid(a, "sources_editor", {pos: {"notes": "R2 test note"}}); run(a, "edit again")
    [x for x in a.button if x.label == "Stage Source Changes"][0].click(); run(a, "Stage again")
    GRID_EDITS.clear()
    goto(a, "Pending Changes")
    a.checkbox(key="confirm_push_source_changes").check(); run(a, "tick")
    a.button(key="push_source_pending").click(); run(a, "Push source changes")
    check(src_cfg("cs_ca")["notes"] == "R2 test note", "pushed: the source's notes changed")
    clean(a, "Pending Changes")

    print("\n== 2. Add a new source (blank form), push")
    goto(a, "Sources")
    [x for x in a.button if x.label == "+ Add a blank source manually (no file)"][0].click(); run(a, "blank source form")
    keys = [t.key for t in a.text_input if (t.key or "").endswith("_source_key") and (t.key or "").startswith("src_")]
    eid = keys[0][len("src_"):-len("_source_key")]
    a.text_input(key=f"src_{eid}_source_key").input("zz_r2"); a.text_input(key=f"src_{eid}_source_label").input("ZZ R2 Test")
    a.text_input(key=f"src_{eid}_upc_column").input("UPC"); a.text_input(key=f"src_{eid}_description_column").input("Description")
    a.number_input(key=f"src_{eid}_priority_rank").set_value(99)
    [x for x in a.button if x.label == "Stage New Source"][0].click(); run(a, "Stage New Source")
    check(dm.get_source_pending_changes(E).get("zz_r2", {}).get("change_type") == "add", "new source staged")
    goto(a, "Pending Changes")
    a.checkbox(key="confirm_push_source_changes").check(); run(a, "tick")
    a.button(key="push_source_pending").click(); run(a, "Push")
    check(bool(q("SELECT 1 FROM dbo.sources WHERE source_key = 'zz_r2'")), "the new source exists")
    goto(a, "Upload & Ingest")
    check("zz_r2" in [s_ for s_ in a.selectbox if s_.label == "Source"][0].options, "listed on Upload & Ingest")
    clean(a, "Upload & Ingest")

    print("\n== 3. Items: add, delete, UPC override — take one back, push the rest, restore")
    goto(j, "Add Item")
    ti = {t.label: t for t in j.text_input}
    ti["UPC *"].input("999000111222"); ti["Description *"].input("R2 TEST ITEM")
    [x for x in j.button if x.label == "Add Item"][0].click(); run(j, "Jason adds an item")
    victim = q("SELECT TOP 1 upc, description FROM dbo.items WHERE source_key = 'cs_ca' ORDER BY upc")[0]
    goto(j, "Delete Item")
    [t for t in j.text_input if t.label.startswith("Search by description or UPC to find")][0].input(victim[0]); run(j, "search")
    [x for x in j.button if x.label == "Delete Item"][0].click(); run(j, "stage the delete")
    ov = q("SELECT TOP 1 i.upc, i.department FROM dbo.items i WHERE i.department = 'GROCERY' AND i.upc NOT IN "
           "(SELECT upc FROM dbo.manual_overrides) ORDER BY i.upc DESC")[0]
    goto(j, "UPC Overrides")
    j.text_input(key="upc_override_search").input(ov[0]); run(j, "search")
    j.selectbox(key=f"ov_dept_{ov[0]}").select("DELI")
    [x for x in j.button if x.label == "Stage Change"][0].click(); run(j, "stage an override")
    pend = dm.get_item_master_pending(E)
    check({"999000111222", victim[0], ov[0]} <= set(pend), f"3 staged ({len(pend)})")
    goto(j, "Pending Changes")
    j.button(key=f"undo_item_master_pending_{victim[0]}").click(); run(j, "Jason takes back the delete")
    check(victim[0] not in dm.get_item_master_pending(E), "the delete is taken back")
    goto(a, "Pending Changes")
    a.checkbox(key="confirm_push_item_master").check(); run(a, "tick")
    a.button(key="push_item_master_pending").click(); run(a, "AJ pushes")
    check(bool(q("SELECT 1 FROM dbo.items WHERE upc = '999000111222'")), "added item is live")
    check(q("SELECT department FROM dbo.items WHERE upc = :u", u=ov[0])[0][0] == "DELI"
          and bool(q("SELECT 1 FROM dbo.manual_overrides WHERE upc = :u", u=ov[0])), "the override is live and pinned")
    check(bool(q("SELECT 1 FROM dbo.items WHERE upc = :u", u=victim[0])), "the taken-back delete didn't happen")
    goto(j, "Delete Item")
    [t for t in j.text_input if t.label.startswith("Search by description or UPC to find")][0].input("999000111222"); run(j, "search")
    [x for x in j.button if x.label == "Delete Item"][0].click(); run(j, "stage deleting the test item")
    goto(a, "Pending Changes")
    a.checkbox(key="confirm_push_item_master").check(); run(a, "tick")
    a.button(key="push_item_master_pending").click(); run(a, "push the delete")
    check(not q("SELECT 1 FROM dbo.items WHERE upc = '999000111222'"), "deleted")
    goto(a, "Delete Item")
    rs = [t for t in a.text_input if t.key == "restore_search"]
    if rs:
        rs[0].input("999000111222"); run(a, "find it in Deleted items")
    rb = [x for x in a.button if x.label == "Restore Item"]
    if rb:
        rb[0].click(); run(a, "Restore Item")
    check(bool(q("SELECT 1 FROM dbo.items WHERE upc = '999000111222'")), "restored")
    clean(a, "Delete Item")

    print("\n== 4. Manual upload of one source (C&S CA, 3 new items) → Merge push")
    f, new_ca = file_with_new_items("cs_ca", 3, "CS CA Daily Order Guide 9.28.26.xlsx")
    check(len(new_ca) == 3, f"test file has 3 new items after cleaning ({new_ca})")
    goto(a, "Upload & Ingest")
    [s for s in a.selectbox if s.label == "Source"][0].select("cs_ca"); run(a, "pick C&S CA")
    UPLOAD["Upload the file for"] = f; run(a, "upload the file"); del UPLOAD["Upload the file for"]
    save = [x for x in a.button if (x.label or "").startswith("Save ")]
    check(bool(save), "file read — Save button shown")
    UPLOAD["Upload the file for"] = f
    save[0].click(); run(a, "Save to raw_items"); del UPLOAD["Upload the file for"]
    merge_push(a)
    live = {r[0]: r[1] for r in q("SELECT upc, department FROM dbo.items WHERE upc IN ('%s')" % "','".join(new_ca))}
    check(set(live) == set(new_ca), f"the 3 new C&S CA items are in the item master ({live})")
    clean(a, "Merge")

    print("\n== 5. Monthly refresh: every file for the month at once")
    f2, new_pnw = file_with_new_items("cs_pnw", 2, "CS PNW Daily Order Guide 9.28.26.xlsx")
    f3 = Up(b"not a real file", "random notes.xlsx")
    goto(a, "Upload & Ingest")
    UPLOAD["mr_files_"] = [f2, f3]; run(a, "drop 2 files")
    tbl = [d.value for d in a.dataframe if hasattr(d.value, "columns") and "Status" in d.value.columns]
    status = dict(zip(tbl[0]["File"], tbl[0]["Status"])) if tbl else {}
    check(status.get(f2.name) == "Ready" and "No matching source" in status.get(f3.name, ""), f"statuses: {status}")
    [x for x in a.button if (x.label or "").startswith("Ingest 1 file")][0].click(); run(a, "Ingest + compute the draft")
    del UPLOAD["mr_files_"]
    merge_push(a)
    check(len(q("SELECT upc FROM dbo.items WHERE upc IN ('%s')" % "','".join(new_pnw))) == 2, "C&S PNW's 2 new items are live")

    print("\n== 6. Activity report has all of it")
    log = dm.list_activity(E)
    areas = set(log["area"])
    check({"Sources", "Items", "Pushed live", "Uploads & Merge"} <= areas, f"areas: {sorted(areas)}")
    check(any(log["action"].str.startswith("Uploaded CS CA")) and any(log["action"] == "Pushed a Merge"), "uploads and Merge pushes recorded")
    check(any((log["actor"] == "Jason") & log["action"].str.contains("Took back")), "Jason's take-back recorded")
    goto(a, "Activity"); clean(a, "Activity")
finally:
    st_mod.file_uploader = real_uploader
    print("\n  restoring #0…"); dm.restore_snapshot(E, BASE, "AJ")
    restore_raw()
    with E.begin() as c:
        c.execute(text("DELETE FROM dbo.user_workspace"))
    print("  vs #0:", dm.compare_snapshot_to_live(E, BASE), q("SELECT source_key FROM dbo.sources"))
    check(q(f"SELECT source_key, COUNT(*), MAX(loaded_at) FROM dbo.raw_items WHERE source_key IN {SRCS} GROUP BY source_key") == RAW0,
          "C&S CA / PNW raw data is back exactly as before the test")
print("FAILURES:", len(F))
