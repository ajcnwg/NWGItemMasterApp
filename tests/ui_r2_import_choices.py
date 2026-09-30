"""Old workbook vs the app: where they disagree about a group the import
touches, the import stages nothing on it and Pending Changes asks which is
right — every workbook decision and the app's own, one box each. Picking
one stages it; Push waits until every question is answered; Ask again (or
undoing the pick) takes it back and asks again. A group the workbook lists
twice where only one row decides is not a question, just a note."""
import io
import json
from uiharness import *
from testbase import BASE, V2
import streamlit as st_mod
from itemmaster import dept_mapping as dm, old_workbook_import as owi
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine(); F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def pc(a):
    st_mod.cache_data.clear()
    a.session_state["dept_shared_filter"] = {"search": "", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(a, "Department Review", "Pending Changes")
    return a


def state(cid):
    with E.connect() as c:
        return c.execute(text("SELECT decision_state FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": cid}).scalar()


def staged_items(cid):
    return sum(1 for c in dm.get_pending_upc_changes(E).values() if c["combo_id"] == cid)


def pending(cid):
    return dm.get_pending_changes(E).get(cid, {}).get("department")


def import_(sheets):
    dm.restore_snapshot(E, BASE, "AJ")
    p = owi.plan(E, owi.extract(sheets), dm.get_departments(E).iloc[:, 0].tolist(), "AJ")
    return owi.apply(E, p, "AJ", True, "department_workbook V2.xlsx")


def ok_page(a, where):
    check(not a.exception and not any("Something went wrong" in m.value for m in a.markdown), f"{where}: no errors")


def push_disabled(a):
    b = [b for b in a.button if b.key == "push_pending_changes"]
    return bool(b) and b[0].disabled


SHEETS = owi.read_workbook(io.BytesIO(open(V2, "rb").read()))
try:
    print("== A. The real V2 workbook")
    import_(SHEETS)
    open_ = dm.list_import_choices(E)
    notes = {c["label"]: c for c in dm.list_import_choices(E, "note")}
    print("  open:", [c["label"] for c in open_], "| notes:", list(notes))
    check(len(open_) == 1 and "SUPPLIES NOT FOR RESALE" in open_[0]["label"], "one real question: SUPPLIES (workbook Crosswalk vs app Broken Out)")
    sup = open_[0]
    S = sup["combo_id"]
    opts = json.loads(sup["options_json"])
    check([o["who"] for o in opts] == ["workbook", "app"] and "SUPPLIES" in opts[0]["text"]
          and "Pending Changes" in opts[0]["stages"] and opts[1]["stages"].startswith("Adds nothing"),
          "two options (the workbook has no item decisions for it): approve as SUPPLIES, or leave it in Broken Out")
    check(pending(S) is None and staged_items(S) == 0 and state(S) == "broken_out", "nothing staged on SUPPLIES until someone picks")
    soap = next(c for k, c in notes.items() if "SOAP BATH" in k)
    dough = next(c for k, c in notes.items() if "DOUGH" in k)
    check("“Health Body Care”" in soap["workbook_says"] and "“HEALTH BODY CARE”" in soap["workbook_says"]
          and "799-item row" in soap["workbook_says"], "SOAP BATH is a note (only one row decides): both spellings, the row that decided")
    check(state(soap["combo_id"]) == "broken_out", "…and the deciding row was followed: moved to Broken Out")
    check(pending(dough["combo_id"]) == "BAKERY", "DOUGH is a note too, BAKERY staged")

    a = session("aj"); run(a, "AJ"); pc(a)
    md = " ".join(m.value for m in a.main.markdown)
    caps = [c.value for c in a.main.caption]
    use = [b for b in a.button if (b.key or "").startswith(f"choice_use_{sup['choice_id']}_")]
    check("##### Needs your choice (1)" in md, "Pending Changes: Needs your choice (1)")
    check(len(use) == 2 and all(b.label == "Use this" for b in use), "one box per option, each with a Use this button")
    check("**Old workbook:**" not in md and "**In the app:**" not in md, "no repeated summary lines above the boxes")
    check(push_disabled(a) and any("Needs your choice" in e.value for e in a.main.error), "Push is blocked while a question is open")
    check(any("listed twice in the old workbook" in c for c in caps), "the listed-twice note shows on the group's card")
    check(any(c.endswith("Old workbook (Crosswalk sheet): approved the whole group") and "Staged by AJ" in c for c in caps),
          "group cards: one line with who staged it and what the workbook said, on which sheet")
    ok_page(a, "Pending Changes")

    print("\n== SUPPLIES: the workbook's, Ask again, again, Undo, the app's")
    a.button(key=f"choice_use_{sup['choice_id']}_0").click(); run(a, "use the Crosswalk sheet's")
    ok_page(a, "workbook's")
    check(pending(S) == "SUPPLIES" and not dm.list_import_choices(E), "SUPPLIES staged, question answered")
    pc(a)
    check(not any("Needs your choice" in e.value for e in a.main.error), "Push no longer blocked by the question")
    a.button(key=f"choice_reopen_{sup['choice_id']}").click(); run(a, "Ask again")
    check(len(dm.list_import_choices(E)) == 1 and pending(S) is None and state(S) == "broken_out",
          "Ask again takes it back (back in Broken Out) and asks again")
    pc(a)
    a.button(key=f"choice_use_{sup['choice_id']}_0").click(); run(a, "use the Crosswalk sheet's")
    ok_page(a, "whole group")
    check(state(S) == "not_reviewed" and pending(S) == "SUPPLIES" and staged_items(S) == 0,
          "out of Broken Out, whole group staged as SUPPLIES")
    check((dm.get_pending_changes(E).get(S, {}).get("origin_note") or "").startswith(owi.NOTE_PREFIX), "…marked as from the old workbook")
    a.button(key="topbar_undo").click(); run(a, "top-bar Undo")
    [b for b in a.button if (b.label or "").startswith("Confirm")][0].click(); run(a, "confirm")
    pc(a)
    check(state(S) == "broken_out" and pending(S) is None and len(dm.list_import_choices(E)) == 1,
          "top-bar Undo: back in Broken Out, nothing staged, and the question is back")
    a.button(key=f"choice_use_{sup['choice_id']}_1").click(); run(a, "the app's")
    ok_page(a, "app's")
    check(not dm.list_import_choices(E) and state(S) == "broken_out" and pending(S) is None and staged_items(S) == 0,
          "the app's: left as it is, question answered")
    pc(a)
    check("##### Needs your choice" not in " ".join(m.value for m in a.main.markdown), "no questions left")
    log = dm.list_activity(E)
    check((log["action"].str.startswith("Workbook vs app — chose:")).sum() >= 3, "Activity records each answer")

    print("\n== B. Two rows that decide different things (DOUGH's 1-item row made DELI)")
    sheets = {k: v.copy() for k, v in SHEETS.items()}
    d = sheets["Decided Broken Out Combos"]
    m = (d["Category"].str.upper() == "BKRY FOOD SERVICE") & (d["Subcategory"].str.upper() == "DOUGH")
    check(m.sum() == 1, "found DOUGH's second row")
    d.loc[m, "Manual Override Department"] = "DELI"
    import_(sheets)
    ch = [c for c in dm.list_import_choices(E) if "DOUGH" in c["label"]]
    check(len(ch) == 1, "DOUGH is a question now")
    c = ch[0]
    D = c["combo_id"]
    opts = json.loads(c["options_json"])
    print("  options:", [(o["who"], o["text"]) for o in opts])
    check([o["who"] for o in opts] == ["workbook", "workbook", "app"] and "BAKERY" in opts[0]["text"]
          and "DELI" in opts[1]["text"] and "GROCERY" in opts[2]["text"], "three options: BAKERY, DELI, or the app's GROCERY")
    check(pending(D) is None, "nothing staged on DOUGH until someone picks")
    a = session("aj"); run(a, "AJ"); pc(a)
    cid_ = c["choice_id"]
    check({f"choice_use_{cid_}_{i}" for i in range(3)} <= {b.key for b in a.button}, "three buttons on its card")
    a.button(key=f"choice_use_{cid_}_1").click(); run(a, "pick DELI")
    ok_page(a, "pick DELI")
    check(pending(D) == "DELI", "DELI staged")
    pc(a)
    a.button(key=f"choice_reopen_{cid_}").click(); run(a, "Ask again")
    check(pending(D) is None and len([x for x in dm.list_import_choices(E) if x["combo_id"] == D]) == 1,
          "Ask again takes DELI back and asks again")
    pc(a)
    a.button(key=f"choice_use_{cid_}_2").click(); run(a, "pick the app's")
    ok_page(a, "pick the app's")
    check(pending(D) is None and not [x for x in dm.list_import_choices(E) if x["combo_id"] == D],
          "the app's: nothing staged, it stays Decided as GROCERY")
finally:
    print("\n  restoring #0…"); dm.restore_snapshot(E, BASE, "AJ")
    with E.begin() as c_:
        c_.execute(text("DELETE FROM dbo.user_workspace"))
    print("  vs #0:", dm.compare_snapshot_to_live(E, BASE), "choices:", len(dm.list_import_choices(E, None)))
print("FAILURES:", len(F))
