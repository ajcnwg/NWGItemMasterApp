import os
"""From the baseline: push real decisions, run a real Merge (engine re-run +
push to dbo.items), check everything landed, then do Decided-tab actions and
LEAVE them in place for the user to inspect. Roll back with snapshot 38."""
import sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from itemmaster.db import get_engine
from itemmaster import dept_mapping as dm

e = get_engine()
FAILS = []


def check(ok, msg):
    print(("   PASS " if ok else "   FAIL ") + msg)
    if not ok:
        FAILS.append(msg)


def pick(where):
    with e.connect() as c:
        return dict(c.execute(text(
            "SELECT TOP 1 combo_id, tier, source_key, raw_department, raw_category, raw_subcategory, n_upcs_total, n_evidence, decided_department "
            f"FROM dbo.dept_mapping_combos WHERE {where} ORDER BY n_upcs_total DESC")).mappings().first())


def label(g):
    return " / ".join(b for b in (g["raw_department"], g["raw_category"], g["raw_subcategory"]) if b)


def where_is(cid):
    locs = []
    for tier, name in (("review", "Crosswalk"), ("unmatched", "Unmatched")):
        if cid in set(dm.get_review_queue(e, tier)["combo_id"]):
            locs.append(name)
    d = dm.get_decided_combos(e)
    row = d[d["combo_id"] == cid]
    if not row.empty:
        locs.append(f"Decided ({row.iloc[0]['status']})")
    if cid in set(dm.get_broken_out_combos(e)["combo_id"]):
        locs.append("Broken Out")
    if cid in dm.get_pending_changes(e) or cid in dm.get_combo_suggestions(e):
        # the app shows a group with a staged decision only on Pending Changes
        locs = [x for x in locs if x == "Broken Out"] + ["Pending"]
    bo = dm.get_broken_out_combos(e)
    bo = bo[bo["combo_id"] == cid]
    n_staged = sum(1 for c in dm.get_pending_upc_changes(e).values() if c["combo_id"] == cid)
    if not bo.empty and n_staged:
        r = bo.iloc[0]
        if n_staged >= r["override_count"] - r["decided_count"] + r["auto_count"]:
            # every item left is staged: it's off Broken Out, on Pending Changes
            locs = [x for x in locs if x != "Broken Out"] + ([] if "Pending" in locs else ["Pending"])
    return locs


def live_departments(cid):
    with e.connect() as c:
        return {r[0]: (r[1], r[2]) for r in c.execute(text(
            "SELECT i.upc, i.department, i.source_key FROM dbo.dept_mapping_combo_upcs cu "
            "JOIN dbo.items i ON i.upc = cu.upc WHERE cu.combo_id = :c"), {"c": cid}).fetchall()}


def push_combo(cid, actor="AJ"):
    ch = dm.get_pending_changes(e)[cid]
    dm.approve_combo(e, cid, ch["department"], ch["staged_by"], pushed_by=actor)
    dm.delete_pending_change(e, cid)
    dm.clear_recent_moves_for_combo(e, cid)


small = "n_upcs_total BETWEEN 5 AND 40"
CW = pick(f"tier='review' AND decision_state='not_reviewed' AND decided_department IS NULL AND {small}")
UM = pick(f"tier='unmatched' AND decision_state='not_reviewed' AND decided_department IS NULL AND {small}")
BO = pick(f"tier='review' AND decision_state='not_reviewed' AND decided_department IS NULL AND {small} AND combo_id <> {CW['combo_id']}")
DEC = pick(f"tier='auto' AND decision_state='not_reviewed' AND decided_department IS NOT NULL AND {small}")
for k, g in (("Crosswalk", CW), ("Unmatched", UM), ("to break out", BO), ("auto-decided", DEC)):
    print(f"{k:14s} combo {g['combo_id']}: {g['source_key'].upper()} - {label(g)} ({g['n_upcs_total']} items)")

try:
    print("\n=== 1. Approve + push ===")
    dm.upsert_combo_suggestion(e, CW["combo_id"], "review", "GROCERY", CW["source_key"], label(CW), CW["n_upcs_total"], "Kristi")
    check(where_is(CW["combo_id"]) == ["Pending"], f"Crosswalk approve staged -> {where_is(CW['combo_id'])}")
    push_combo(CW["combo_id"])
    check(where_is(CW["combo_id"]) == ["Decided (Whole Group — Manual)"], f"pushed -> {where_is(CW['combo_id'])}")
    dm.upsert_combo_suggestion(e, UM["combo_id"], "unmatched", "HOUSEHOLD CARE", UM["source_key"], label(UM), UM["n_upcs_total"], "Jason")
    push_combo(UM["combo_id"])
    check(where_is(UM["combo_id"]) == ["Decided (Whole Group — Manual)"], f"Unmatched approved + pushed -> {where_is(UM['combo_id'])}")

    print("\n=== 2. Break out, decide every item, push ===")
    snap = dm.get_combo_snapshot(e, BO["combo_id"])
    dm.break_out_combo(e, BO["combo_id"], "Kristi", upc_decisions={})
    dm.record_recent_move(e, BO["combo_id"], BO["source_key"], label(BO), BO["n_upcs_total"], "Broken Out to UPC-Level", snap, "Kristi")
    upcs = dm.get_pending_upc_overrides(e, BO["combo_id"])["upc"].tolist()
    half = len(upcs) // 2
    decisions = {u: {"department": "GROCERY" if i < half else "DELI", "combo_id": BO["combo_id"], "label": label(BO),
                     "description": None, "source_key": BO["source_key"]} for i, u in enumerate(upcs)}
    dm.stage_broken_out_decisions(e, decisions, "Kristi")
    check(where_is(BO["combo_id"]) == ["Pending"], f"all {len(upcs)} items staged -> off Broken Out, only on Pending (where_is={where_is(BO['combo_id'])})")
    staged = {u: c for u, c in dm.get_pending_upc_changes(e).items() if c["combo_id"] == BO["combo_id"]}
    dm.apply_upc_decisions(e, {u: {"department": c["department"], "staged_by": c["staged_by"]} for u, c in staged.items()}, "AJ")
    dm.delete_pending_upc_changes(e, list(staged))
    dm.clear_recent_moves_for_combo(e, BO["combo_id"])
    check(where_is(BO["combo_id"]) == ["Decided (Broken Out — Manually Decided)"], f"pushed -> {where_is(BO['combo_id'])}")
    # the app's Department push syncs the pushed groups' items (Merge only syncs the items it adds)
    dm.sync_item_departments(e, set(dm.combo_member_upcs(e, [CW["combo_id"], UM["combo_id"]])) | set(staged))

    print("\n=== 3. Real Merge: engine re-run + push to the live item master ===")
    t = time.time()
    with e.connect() as c:
        order = c.execute(text("SELECT source_key FROM dbo.sources WHERE enabled = 1 ORDER BY priority_rank")).scalars().all()
    final_df, overrides_applied, deleted = dm.compute_merge_final_df(e, order)
    dm.save_merge_compute(e, final_df, "AJ", overrides_applied, deleted)
    result = dm.push_merge_compute(e, "AJ", is_admin=True)
    print(f"   merge pushed in {time.time()-t:.0f}s: {', '.join(f'{k}={v}' for k, v in result.items() if isinstance(v, (int, str)))}")
    for g, want in ((CW, "GROCERY"), (UM, "HOUSEHOLD CARE")):
        live = live_departments(g["combo_id"])
        non_nwg = {u: d for u, (d, src) in live.items() if src != "nwg"}
        check(non_nwg and all(d == want for d in non_nwg.values()),
              f"combo {g['combo_id']}: all {len(non_nwg)} items live as {want}")
        check(where_is(g["combo_id"]) == ["Decided (Whole Group — Manual)"], f"   ...and still Decided after the engine re-run")
    live = live_departments(BO["combo_id"])
    exp = {u: d["department"] for u, d in decisions.items()}
    got = {u: d for u, (d, src) in live.items() if src != "nwg"}
    check(got and all(got[u] == exp[u] for u in got), f"combo {BO['combo_id']}: all {len(got)} items live with their own item decision (GROCERY/DELI)")
    check(where_is(BO["combo_id"]) == ["Decided (Broken Out — Manually Decided)"], "   ...and still Decided after the engine re-run")

    print("\n=== 4. Decided-tab actions (left in place for you) ===")
    dm.upsert_combo_suggestion(e, CW["combo_id"], "review", "DELI", CW["source_key"], label(CW), CW["n_upcs_total"], "Kristi")
    check(where_is(CW["combo_id"]) == ["Pending"], f"Crosswalk group: Change department -> DELI staged by Kristi -> {where_is(CW['combo_id'])}")
    snap = dm.get_combo_snapshot(e, UM["combo_id"])
    dm.revert_combo(e, UM["combo_id"], "Jason"); dm.clear_pending_for_combo(e, UM["combo_id"])
    dm.record_recent_move(e, UM["combo_id"], UM["source_key"], label(UM), UM["n_upcs_total"], "Send Back to Unmatched", snap, "Jason")
    check(where_is(UM["combo_id"]) == ["Unmatched"], f"Unmatched group: sent back by Jason -> {where_is(UM['combo_id'])} (undoable in Recent moves)")
    snap = dm.get_combo_snapshot(e, BO["combo_id"])
    dm.reopen_broken_out(e, BO["combo_id"], "Kristi", upc_decisions={}); dm.clear_pending_for_combo(e, BO["combo_id"])
    dm.record_recent_move(e, BO["combo_id"], BO["source_key"], label(BO), BO["n_upcs_total"], "Send Back to Broken Out", snap, "Kristi")
    check(where_is(BO["combo_id"]) == ["Broken Out"], f"Broken Out group: sent back to Broken Out by Kristi -> {where_is(BO['combo_id'])} (undoable)")
    dm.upsert_combo_suggestion(e, DEC["combo_id"], "auto", "GROCERY" if DEC["decided_department"] != "GROCERY" else "DELI",
                               DEC["source_key"], label(DEC), DEC["n_upcs_total"], "Jason")
    check(where_is(DEC["combo_id"]) == ["Pending"], f"auto-decided group: Change department staged by Jason -> {where_is(DEC['combo_id'])}")
except Exception:
    traceback.print_exc()
    FAILS.append("exception")
print(f"\n{len(FAILS)} failure(s)" + (": " + "; ".join(FAILS) if FAILS else ""))
