"""Everything a person can do in a Broken Out group, as Jason / Kristi / AJ,
with the top-bar Undo / Redo checked after each kind of step."""
import io
import pandas as pd
import streamlit as st_mod
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine(); CID = 197; F = []; N = [0]
LBL = "FS FROZ MEXICAN"
S = {"search": LBL, "sort_label": None, "sort_desc": True, "page_size": 25}


def check(ok, msg):
    N[0] += 1
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


orig = dm.get_combo_snapshot(E, CID)
review = dm.get_pending_upc_overrides(E, CID).sort_values("description").reset_index(drop=True)
auto = dm.get_auto_decided_upc_overrides(E, CID)
RU, AU = review["upc"].tolist(), auto["upc"].tolist()


def reset():
    with E.begin() as c:
        for t in ("dept_mapping_recent_moves",) + dm.STAGED_TABLES:
            c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": CID})
        dm._restore_combo_snapshot(c, CID, orig, "test")
        c.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
    for who in ("Jason", "Kristi", "AJ"):
        dm.release_broken_out_claim(E, CID, who, is_admin=True)
    st_mod.cache_data.clear()
    GRID_EDITS.clear()


def open_group(at):
    at.session_state["dept_shared_filter"] = dict(S)
    goto(at, "Department Review", "Broken Out")


def ver(at, g):
    return at.session_state[f"wb_{g}_{CID}"]["ver"]


def draft(at, g):
    return {u: d for u, d in at.session_state[f"wb_{g}_{CID}"]["draft"].items() if d}


def btn(at, name, g):
    return at.button(key=f"wb_{name}_{g}_{CID}_{ver(at, g)}")


def shown_upcs(at, g):
    """The grid's rows in shown order (the app filters/sorts before showing)."""
    st_ = at.session_state[f"wb_{g}_{CID}"]
    df = (review if g == "bo" else auto)
    ups = df["upc"].tolist()
    q = st_["filter"].strip().lower()
    if q:
        blob = df.fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        ups = [u for u, b in zip(ups, blob) if any(all(w in b for w in alt.split()) for alt in q.split(",") if alt.strip())]
    if st_["show"] == "Still blank":
        ups = [u for u in ups if not st_["draft"].get(u)]
    elif st_["show"] == "Filled in":
        ups = [u for u in ups if st_["draft"].get(u)]
    return ups


def topbar(at, kind, expect_in=None):
    at.button(key=f"topbar_{kind}").click(); run(at, f"top-bar {kind}")
    body = " | ".join(m.value for m in at.markdown if m.value.startswith("**") and " — " in m.value)
    c = [b for b in at.button if b.label == f"Confirm {kind}"]
    if expect_in:
        check(expect_in in body, f"{kind} dialog names the step: {expect_in!r}")
    c[0].click(); run(at, f"confirm {kind}")


def stage_count(at):
    return at.button(key=f"bo_stage_all_{CID}").label


def staged():
    return {u: c["department"] for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == CID}


try:
    reset()
    # the grid's own order (it sorts by description in the query)
    j = session("jason"); run(j, "load Jason"); open_group(j)
    k = session("kristi"); run(k, "load Kristi"); open_group(k)
    a = session("aj"); run(a, "load AJ"); open_group(a)

    print("\n== 1. Claims")
    check(any(b.key == f"claim_broken_out_{CID}" for b in k.button), "Kristi sees a Claim button while it's unclaimed")
    j.button(key=f"claim_broken_out_{CID}").click(); run(j, "Jason claims")
    check(any(b.key == f"bo_stage_all_{CID}" for b in j.button), "Jason gets the grids and the group Stage button")
    k = session("kristi"); run(k, "reload Kristi"); open_group(k)
    check(any("Jason is working on this" in i.value for i in k.caption) and not any(b.key == f"bo_stage_all_{CID}" for b in k.button),
          "Kristi sees 'Jason is working on this' and no grids")
    check(not any((b.key or "").startswith(f"force_release_claim_{CID}") for b in k.button), "Kristi has no Force release")
    a = session("aj"); run(a, "reload AJ"); open_group(a)
    check(any(b.key == f"force_release_claim_{CID}" for b in a.button), "AJ (admin) has Force release")
    j2 = session("jason"); run(j2, "Jason reload")
    check(any("working on this Broken Out group" in m.value for m in list(j2.sidebar.markdown) + list(j2.sidebar.caption)),
          "Jason's notifications list the group he's working on")
    j.button(key=f"release_claim_{CID}").click(); run(j, "Jason releases")
    check(any(b.key == f"claim_broken_out_{CID}" for b in j.button), "released: Claim button back")
    j.button(key=f"claim_broken_out_{CID}").click(); run(j, "Jason claims again")

    print("\n== 2. Review grid: cell picks, ✓ rows, filter, show, fill, clears")
    check(stage_count(j) == "Stage all 0 decision(s) in this group", "starts at 0")
    rows = shown_upcs(j, "bo")
    edit_grid(j, f"wb_grid_bo_{CID}_{ver(j, 'bo')}", {0: {"Department": "DELI"}, 1: {"Department": "FROZEN"}})
    run(j, "pick 2 cells")
    check(draft(j, "bo") == {rows[0]: "DELI", rows[1]: "FROZEN"}, "2 cell picks land in the right rows")
    check(stage_count(j) == "Stage all 2 decision(s) in this group", "live count 2")
    topbar(j, "undo", "Edited 2 cell(s)")
    check(draft(j, "bo") == {}, "undo takes the cell picks back")
    topbar(j, "redo", "Edited 2 cell(s)")
    check(len(draft(j, "bo")) == 2, "redo puts them back")

    rows = shown_upcs(j, "bo")
    edit_grid(j, f"wb_grid_bo_{CID}_{ver(j, 'bo')}", {2: {"✓": True}, 3: {"✓": True}, 4: {"✓": True}})
    run(j, "tick 3 rows")
    j.selectbox(key=f"wb_pick_bo_{CID}_{ver(j, 'bo')}").select("DAIRY")
    btn(j, "setchk", "bo").click(); run(j, "Set ✓ rows")
    d = draft(j, "bo")
    check(all(d.get(u) == "DAIRY" for u in rows[2:5]) and len(d) == 5, "Set ✓ rows: the 3 ticked rows DAIRY, others untouched")
    topbar(j, "undo", "Set 3 checked row(s) to DAIRY")
    check(len(draft(j, "bo")) == 2, "undo Set ✓ rows")
    topbar(j, "redo")

    btn(j, "setchk", "bo").click(); run(j, "Set ✓ rows with nothing ticked")
    check(len(draft(j, "bo")) == 5, "Set ✓ rows with nothing ticked changes nothing")
    j.selectbox(key=f"wb_pick_bo_{CID}_{ver(j, 'bo')}").select("")
    btn(j, "setall", "bo").click(); run(j, "Set all without picking")
    check(len(draft(j, "bo")) == 5, "Set all without picking a Department changes nothing")

    q = str(review.loc[0, "description"]).split()[0].lower()
    j.text_input(key=f"wb_filter_bo_{CID}_{ver(j, 'bo')}").input(q); run(j, f"filter '{q}'")
    shown = shown_upcs(j, "bo")
    check(btn(j, "setall", "bo").label == f"Set all {len(shown)} shown", f"filter narrows to {len(shown)} row(s)")
    j.selectbox(key=f"wb_pick_bo_{CID}_{ver(j, 'bo')}").select("GROCERY")
    btn(j, "setall", "bo").click(); run(j, "Set all shown")
    check(all(draft(j, "bo").get(u) == "GROCERY" for u in shown), "Set all shown fills only the filtered rows")
    topbar(j, "undo", f"Set {len(shown)} shown row(s) to GROCERY")
    j.text_input(key=f"wb_filter_bo_{CID}_{ver(j, 'bo')}").input(""); run(j, "clear filter")
    j.selectbox(key=f"wb_show_bo_{CID}_{ver(j, 'bo')}").select("Still blank"); run(j, "Show: still blank")
    check(btn(j, "setall", "bo").label == f"Set all {6 - len(draft(j, 'bo'))} shown", "Show 'Still blank' shows only blank rows")
    j.selectbox(key=f"wb_show_bo_{CID}_{ver(j, 'bo')}").select("Filled in"); run(j, "Show: filled in")
    check(btn(j, "setall", "bo").label == f"Set all {len(draft(j, 'bo'))} shown", "Show 'Filled in' shows only filled rows")
    j.selectbox(key=f"wb_show_bo_{CID}_{ver(j, 'bo')}").select("All"); run(j, "Show: all")

    fb = btn(j, "fill", "bo")
    blanks = 6 - len(draft(j, "bo"))
    check(fb.label.startswith(f"Fill {blanks} blank(s) with suggested:"), f"Fill button names the suggestion ({fb.label})")
    before = draft(j, "bo")
    fb.click(); run(j, "Fill blanks with suggested")
    d = draft(j, "bo")
    check(len(d) == 6 and all(d[u] == before[u] for u in before), "fill only touches blanks")
    topbar(j, "undo", "Filled")
    check(draft(j, "bo") == before, "undo the fill")

    rows = shown_upcs(j, "bo")
    edit_grid(j, f"wb_grid_bo_{CID}_{ver(j, 'bo')}", {0: {"✓": True}})
    run(j, "tick row 0")
    btn(j, "clear", "bo").click(); run(j, "Clear ✓ rows")
    check(rows[0] not in draft(j, "bo") and len(draft(j, "bo")) == len(before) - 1, "Clear ✓ rows blanks just that row")
    topbar(j, "undo", "Cleared 1 row(s)")
    btn(j, "clearall", "bo").click(); run(j, "Clear all")
    check(draft(j, "bo") == {}, "Clear all empties the grid")
    topbar(j, "undo", "Cleared all")
    check(draft(j, "bo") == before, "undo Clear all")

    print("\n== 3. Auto grid + one Stage for the group")
    check(btn(j, "fill", "ba").label == f"Fill {len(AU)} blank(s) with auto", f"auto Fill label ({btn(j, 'fill', 'ba').label})")
    btn(j, "fill", "ba").click(); run(j, "Fill with auto")
    arows = shown_upcs(j, "ba")
    edit_grid(j, f"wb_grid_ba_{CID}_{ver(j, 'ba')}", {0: {"Department": "DELI"}})
    run(j, "change one auto row by cell")
    check(draft(j, "ba")[arows[0]] == "DELI" and len(draft(j, "ba")) == len(AU), "auto grid: filled + one changed")
    total = len(draft(j, "bo")) + len(draft(j, "ba"))
    check(stage_count(j) == f"Stage all {total} decision(s) in this group", f"group count {total}")
    j.button(key=f"bo_stage_all_{CID}").click(); run(j, "Stage all")
    st_ = staged()
    check(len(st_) == total and st_[arows[0]] == "DELI", f"{total} staged in one click, the changed auto row as DELI")
    k = session("kristi"); run(k, "Kristi"); goto(k, "Department Review", "Pending Changes")
    check(any(LBL in m.value for m in k.markdown), "the group shows on Pending Changes for others")
    topbar(j, "undo", "Staged")
    check(not staged() and len(draft(j, "bo")) + len(draft(j, "ba")) == total, "undo stage: back in the grids, filled as before")
    topbar(j, "redo")
    check(len(staged()) == total, "redo re-stages")
    topbar(j, "undo")

    print("\n== 4. Excel: download, upload, into grids / stage")
    check(any(b.key == f"grp_dl_197_items" or (b.key or "").startswith("grp_dl_") for b in j.get("download_button")), "Download as Excel button")
    btn(j, "clearall", "bo").click(); run(j, "clear review grid")
    btn(j, "clearall", "ba").click(); run(j, "clear auto grid")
    rows_x = ([{"UPC": u, "Department": "USE AUTO/SUGGESTED"} for u in AU[:4]] + [{"UPC": AU[4], "Department": "DELI"}]
              + [{"UPC": RU[0], "Department": "use auto/suggested"}, {"UPC": RU[1], "Department": "DAIRY"},
                 {"UPC": RU[2], "Department": "NOT A DEPT"}, {"UPC": "999999999999", "Department": "DELI"}])
    buf = io.BytesIO(); pd.DataFrame(rows_x).to_excel(buf, index=False); buf.name = "my group.xlsx"
    real = st_mod.file_uploader
    st_mod.file_uploader = lambda label, *a_, key=None, **kw: (buf.seek(0) or buf) if key and key.startswith("grp_up_") else real(label, *a_, key=key, **kw)
    run(j, "upload file")
    cap = [c.value for c in j.caption if "have a Department" in c.value]
    print("  ", cap)
    check(cap and cap[0].startswith("7 row(s) have a Department") and "1 UPC(s) aren't in this group" in cap[0] and "1 aren't a real Department" in cap[0],
          "file check: 7 usable, 1 foreign UPC, 1 bad Department")
    gb = next(b for b in j.button if (b.key or "").startswith("grp_apply_"))
    gb.click(); run(j, "Put into the grids")
    exp_auto = {**{u: auto.set_index("upc").loc[u, "department"] for u in AU[:4]}, AU[4]: "DELI"}
    exp_rev = {RU[0]: review.set_index("upc").loc[RU[0], "suggested_department"], RU[1]: "DAIRY"}
    check(draft(j, "ba") == exp_auto and draft(j, "bo") == exp_rev, "into grids: right values in the right grid")
    topbar(j, "undo", "from my group.xlsx")
    check(draft(j, "ba") == {} and draft(j, "bo") == {} and any("not used yet" in m.value for m in j.markdown),
          "undo the Excel import: both grids empty again, the file back with its choices")
    topbar(j, "redo", "from my group.xlsx")
    check(draft(j, "ba") == exp_auto and draft(j, "bo") == exp_rev, "redo the Excel import")
    topbar(j, "undo")
    sb = next(b for b in j.button if (b.key or "").startswith("grp_stage_"))
    sb.click(); run(j, "Stage these (from Excel)")
    check(staged() == {**exp_auto, **exp_rev}, "stage from Excel: exactly the file's 7 decisions")
    topbar(j, "undo", "Staged 7")
    check(not staged() and draft(j, "ba") == {} and any("not used yet" in m.value for m in j.markdown),
          "undo the stage: nothing staged, the file back with its choices")
    topbar(j, "redo")
    check(staged() == {**exp_auto, **exp_rev}, "redo: staged again")

    print("\n== 5. Send Back with staged work, then Undo… from Pending")
    open_group(j)
    j.button(key=f"revert_broken_out_{CID}").click(); run(j, "Send Back to Crosswalk")
    check(any("no decisions" in w.value for w in j.warning), "send-back warns the group will have no decisions")
    next(b for b in j.button if b.label == "Send it back").click(); run(j, "confirm")
    check(not staged() and dm.get_combo_snapshot(E, CID)["combo"]["decision_state"] == "not_reviewed", "in Crosswalk, staged work cleared")
    j.session_state["dept_shared_filter"] = dict(S); goto(j, "Department Review", "Crosswalk")
    j.selectbox(key=f"dept_choice_review_{CID}").select("FROZEN"); run(j, "pick"); j.button(key=f"approve_review_{CID}").click(); run(j, "approve")
    goto(j, "Department Review", "Pending Changes")
    j.button(key=f"undo_pending_{CID}").click(); run(j, "Undo…")
    r = next(x for x in j.radio if (x.key or "") == f"undo_auto_{CID}_1")
    check(r.options[0].startswith("Everything as it was — 7 staged decision(s)"), f"Undo… offers everything back ({r.options[0]})")
    j.button(key=f"undo_to_{CID}_1").click(); run(j, "Confirm undo (everything)")
    check(staged() == {**exp_auto, **exp_rev} and dm.get_combo_snapshot(E, CID)["combo"]["decision_state"] == "broken_out",
          "Broken Out again with all 7 staged decisions back")
    topbar(j, "undo", "Undid to")
    check(CID in dm.get_pending_changes(E), "top-bar Undo reverses that too (back to the Crosswalk approval)")
finally:
    st_mod.file_uploader = real if "real" in dir() else st_mod.file_uploader
    reset()
    print("\n  restored exactly:", dm._redo_state_key(dm.get_combo_snapshot(E, CID)) == dm._redo_state_key(orig))
print(f"\n{N[0] - len(F)} of {N[0]} passed. FAILURES: {len(F)}")
for f in F:
    print(" -", f)
