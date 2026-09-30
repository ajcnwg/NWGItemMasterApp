import os
"""Edge-case test of Break Out / Send Back / undo-to-stage, run against real
combos and fully restored afterward. After EVERY action it asserts the combo
is visible in exactly one review location — never zero (disappeared)."""
import sys, traceback
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


def pick(sql):
    with e.connect() as c:
        r = c.execute(text(sql)).mappings().first()
    assert r, sql
    return dict(r)


BASE = """SELECT TOP 1 c.combo_id, c.tier, c.n_evidence, c.source_key, c.raw_subcategory AS label, c.n_upcs_total
    FROM dbo.dept_mapping_combos c WHERE {w}
      AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_pending_changes p WHERE p.combo_id = c.combo_id)
      AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_combo_suggestions s WHERE s.combo_id = c.combo_id)
      AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_pending_upc_changes u WHERE u.combo_id = c.combo_id)
      AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_recent_moves m WHERE m.combo_id = c.combo_id)
    ORDER BY c.n_upcs_total"""
crosswalk = pick(BASE.format(w="c.tier='review' AND c.decision_state='not_reviewed' AND c.decided_department IS NULL AND c.n_upcs_total BETWEEN 3 AND 40"))
auto_ev = pick(BASE.format(w="c.tier='auto' AND c.decision_state='not_reviewed' AND c.decided_department IS NOT NULL AND c.n_evidence > 0 AND c.n_upcs_total BETWEEN 3 AND 40"))
auto_noev = pick(BASE.format(w="c.tier='auto' AND c.decision_state='not_reviewed' AND c.decided_department IS NOT NULL AND c.n_evidence = 0 AND c.n_upcs_total BETWEEN 3 AND 40"))
dec = dm.get_decided_combos(e)
fa_ids = dec[(dec["status"] == "Broken Out — Fully Auto") & (dec["n_upcs_total"] >= 5)].sort_values("n_upcs_total")["combo_id"].tolist()
fully_auto = None
for fid in fa_ids:
    try:
        fully_auto = pick(BASE.format(w=f"c.combo_id = {int(fid)}"))
        break
    except AssertionError:
        continue
COMBOS = {"crosswalk": crosswalk, "auto_ev": auto_ev, "auto_noev": auto_noev, "fully_auto": fully_auto}
for k, v in COMBOS.items():
    print(k, v and (v["combo_id"], v["tier"], v["n_evidence"], v["label"], v["n_upcs_total"]))

originals = {}
with e.connect() as c:
    for k, v in COMBOS.items():
        if v:
            cid = v["combo_id"]
            meta = c.execute(text("SELECT last_decided_by, last_decided_at FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": cid}).mappings().first()
            ov = c.execute(text("SELECT upc, updated_by, updated_at, pushed_by, pushed_at FROM dbo.dept_mapping_upc_overrides WHERE combo_id=:c"), {"c": cid}).mappings().all()
            originals[cid] = (dm.get_combo_snapshot(e, cid), dict(meta), [dict(o) for o in ov])


def locations(cid):
    with e.connect() as c:
        pend = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes WHERE combo_id=:c"), {"c": cid}).scalar()
        sugg = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_combo_suggestions WHERE combo_id=:c"), {"c": cid}).scalar()
        staged = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes WHERE combo_id=:c"), {"c": cid}).scalar()
    on_pending = bool(pend or sugg)
    locs = set()
    if not on_pending:
        if cid in set(dm.get_review_queue(e, "review")["combo_id"]):
            locs.add("Crosswalk")
        if cid in set(dm.get_review_queue(e, "unmatched")["combo_id"]):
            locs.add("Unmatched")
        d = dm.get_decided_combos(e)
        if cid in set(d["combo_id"]):
            locs.add("Decided")
    b = dm.get_broken_out_combos(e)
    row = b[b["combo_id"] == cid]
    if not row.empty and not on_pending:
        r = row.iloc[0]
        left = max(0, r.override_count - r.decided_count + r.auto_count - staged)
        if not (left == 0 and staged > 0):
            locs.add("Broken Out")
    if on_pending or staged:
        locs.add("Pending")
    return locs


def assert_visible(cid, expect_main=None, note=""):
    locs = locations(cid)
    main = locs - {"Pending"}
    ok = bool(locs) and len(main) <= 1 and (expect_main is None or expect_main in locs)
    check(ok, f"{note} -> visible in {sorted(locs) or 'NOWHERE'}" + (f" (expected {expect_main})" if expect_main else ""))
    return locs


def stack(cid):
    return dm.get_combo_undo_path(e, cid)["moves"]


def send_back(v, actor="AJ"):
    cid = v["combo_id"]
    snap = dm.get_combo_snapshot(e, cid)
    st = snap["combo"]["decision_state"]
    if st in ("broken_out", "decided_broken_out"):
        dm.revert_broken_out_combo(e, cid, actor)
    else:
        dm.revert_combo(e, cid, actor)
    dm.clear_pending_for_combo(e, cid)
    dm.record_recent_move(e, cid, v["source_key"], v["label"], v["n_upcs_total"], "Send Back", snap, actor)


def break_out(v, auto=False, actor="AJ"):
    cid = v["combo_id"]
    snap = dm.get_combo_snapshot(e, cid)
    ud = dm.compute_upc_decisions_for_combo(e, cid) if auto else {}
    dm.break_out_combo(e, cid, actor, upc_decisions=ud)
    dm.record_recent_move(e, cid, v["source_key"], v["label"], v["n_upcs_total"], "Break Out", snap, actor)
    return len(ud)


def to_broken_out(v, actor="AJ"):
    cid = v["combo_id"]
    snap = dm.get_combo_snapshot(e, cid)
    dm.reopen_broken_out(e, cid, actor)
    dm.record_recent_move(e, cid, v["source_key"], v["label"], v["n_upcs_total"], "Send Back to Broken Out", snap, actor)


def stage_items(v, n, actor="Kristi"):
    cid = v["combo_id"]
    df = dm.get_pending_upc_overrides(e, cid)
    if df.empty:
        df = dm.get_auto_decided_upc_overrides(e, cid)
    upcs = df["upc"].tolist()[:n]
    dm.stage_broken_out_decisions(e, {u: {"department": "GROCERY", "combo_id": cid, "label": v["label"],
                                          "description": None, "source_key": v["source_key"]} for u in upcs}, actor)
    return len(upcs)


def restore_all():
    for cid, (snap, meta, ov) in originals.items():
        with e.begin() as c:
            dm._restore_combo_snapshot(c, cid, snap, "test")
            c.execute(text("UPDATE dbo.dept_mapping_combos SET last_decided_by=:b, last_decided_at=:a WHERE combo_id=:c"),
                      {"b": meta["last_decided_by"], "a": meta["last_decided_at"], "c": cid})
            if ov:
                c.execute(text("UPDATE dbo.dept_mapping_upc_overrides SET updated_by=:updated_by, updated_at=:updated_at, "
                               "pushed_by=:pushed_by, pushed_at=:pushed_at WHERE upc=:upc"), ov)
            for t in ("dept_mapping_recent_moves", "dept_mapping_pending_changes", "dept_mapping_combo_suggestions",
                      "dept_mapping_pending_upc_changes", "dept_mapping_upc_change_suggestions", "dept_mapping_broken_out_claims"):
                c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": cid})
            c.execute(text("DELETE FROM dbo.dept_mapping_undo_requests WHERE entity_type IN ('combo','upc_group') AND entity_id=:c"), {"c": str(cid)})


try:
    cw = crosswalk; cid = cw["combo_id"]
    print(f"\n=== A. Crosswalk combo {cid}: back-and-forth collapses ===")
    assert_visible(cid, "Crosswalk", "start")
    break_out(cw); assert_visible(cid, "Broken Out", "break out")
    check(len(stack(cid)) == 1, f"stack 1 after break out ({len(stack(cid))})")
    send_back(cw); assert_visible(cid, "Crosswalk", "send back")
    check(len(stack(cid)) == 0, f"round trip collapsed to empty stack ({len(stack(cid))})")
    for i in range(4):
        break_out(cw, auto=(i % 2 == 0)); send_back(cw)
    assert_visible(cid, "Crosswalk", "4 more round trips (mixed auto/blank)")
    check(len(stack(cid)) == 0, f"still empty after 4 round trips ({len(stack(cid))})")

    print(f"\n=== B. Crosswalk: break out, stage, undo each stage ===")
    break_out(cw); n = stage_items(cw, 2)
    assert_visible(cid, "Broken Out", f"broken out + {n} staged")
    p = dm.get_combo_undo_path(e, cid)
    check(p["has_staged"] and len(p["moves"]) == 1 and p["authorized"] == "Kristi", f"picker: staged + 1 move, authorized Kristi ({p['authorized']})")
    dm.undo_combo_to_stage(e, cid, 0, "Kristi"); assert_visible(cid, "Broken Out", "undo staged only")
    check(len(stack(cid)) == 1, "move still undoable")
    dm.undo_combo_to_stage(e, cid, 1, "AJ"); assert_visible(cid, "Crosswalk", "undo back to Crosswalk")
    check(dm.get_combo_snapshot(e, cid) == originals[cid][0], "Crosswalk combo exactly original")

    print(f"\n=== C. Crosswalk: stage on broken out, then Send Back discards it ===")
    break_out(cw); stage_items(cw, 2); send_back(cw)
    locs = assert_visible(cid, "Crosswalk", "send back while items staged")
    check("Pending" not in locs, "staged items discarded, nothing orphaned on Pending")
    check(len(stack(cid)) == 2, "NOT collapsed — the staged work stays recoverable by undo")
    dm.undo_combo_to_stage(e, cid, 2, "AJ")

    if auto_ev:
        a = auto_ev; aid = a["combo_id"]
        print(f"\n=== D. Auto-decided WITH evidence {aid}: send back -> Crosswalk, never lost ===")
        assert_visible(aid, "Decided", "start")
        send_back(a); assert_visible(aid, "Crosswalk", "send back")
        with e.connect() as c:
            rej = c.execute(text("SELECT rejected FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": aid}).scalar()
        check(bool(rej), "flagged rejected (engine won't silently re-decide it)")
        break_out(a); assert_visible(aid, "Broken Out", "break out from Crosswalk")
        send_back(a); assert_visible(aid, "Crosswalk", "send back from Broken Out (rejected kept)")
        check(len(stack(aid)) == 1, f"BO round trip collapsed, only the original Send Back left ({len(stack(aid))})")
        dm.upsert_combo_suggestion(e, aid, "auto", "GROCERY", a["source_key"], a["label"], a["n_upcs_total"], "Kristi")
        assert_visible(aid, "Pending", "approve staged from Crosswalk")
        p = dm.get_combo_undo_path(e, aid)
        check(p["staged"]["combo_decision"] == "GROCERY" and p["authorized"] == "Kristi" and len(p["moves"]) == 1,
              "picker: staged approve + Send Back, authorized Kristi")
        check(p["moves"][0]["before"].startswith("Decided as"), f"oldest stage reads '{p['moves'][0]['before']}'")
        dm.undo_combo_to_stage(e, aid, 1, "Kristi"); assert_visible(aid, "Decided", "undo all the way to Decided")
        check(dm.get_combo_snapshot(e, aid) == originals[aid][0], "auto combo exactly original (rejected cleared)")

    if auto_noev:
        a = auto_noev; aid = a["combo_id"]
        print(f"\n=== E. Auto-decided with NO evidence {aid}: send back -> Unmatched ===")
        send_back(a); assert_visible(aid, "Unmatched", "send back")
        send_back(a); assert_visible(aid, "Unmatched", "second send back (no-op) stays put")
        check(len(stack(aid)) == 1, f"no-op move not recorded ({len(stack(aid))})")
        dm.undo_combo_to_stage(e, aid, 1, "AJ"); assert_visible(aid, "Decided", "undo -> Decided")
        check(dm.get_combo_snapshot(e, aid) == originals[aid][0], "exactly original")

    print("\n=== G. Authorization ===")
    break_out(cw); stage_items(cw, 1, actor="Kristi")
    p = dm.get_combo_undo_path(e, cid)
    check(p["authorized"] == "Kristi", "only Kristi (or admin) may discard Kristi's staged work")
    r = dm.request_undo_upc_group_all(e, cid, "Jason")
    check(not r["executed"], "Jason's request doesn't execute")
    check(dm.get_combo_undo_path(e, cid)["has_staged"], "Kristi's work still there after Jason's request")
    dm.undo_combo_to_stage(e, cid, 1, "Kristi")
    with e.connect() as c:
        reqs = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_undo_requests WHERE entity_id=:c"), {"c": str(cid)}).scalar()
    check(reqs == 0, "undo clears the pending request too")
    assert_visible(cid, "Crosswalk", "back to Crosswalk")
except Exception:
    traceback.print_exc()
    FAILS.append("exception")
finally:
    restore_all()
    for cid, (snap, _, _) in originals.items():
        ok = dm.get_combo_snapshot(e, cid) == snap and not stack(cid)
        check(ok, f"restored combo {cid}")
    print(f"\n{len(FAILS)} failure(s)" + (": " + "; ".join(FAILS) if FAILS else ""))
