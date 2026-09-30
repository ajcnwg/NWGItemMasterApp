import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from itemmaster.db import get_engine
from itemmaster import dept_mapping as dm

e = get_engine()
with e.connect() as c:
    cid, n, label, src = c.execute(text("""
        SELECT TOP 1 c.combo_id, c.n_upcs_total, c.raw_subcategory, c.source_key FROM dbo.dept_mapping_combos c
        WHERE c.decision_state = 'not_reviewed' AND c.decided_department IS NOT NULL AND c.tier = 'auto'
          AND c.n_upcs_total BETWEEN 3 AND 60
          AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_pending_changes p WHERE p.combo_id = c.combo_id)
          AND NOT EXISTS (SELECT 1 FROM dbo.dept_mapping_recent_moves m WHERE m.combo_id = c.combo_id)
        ORDER BY c.combo_id""")).one()
print(f"combo {cid} ({src} / {label}, {n} items)")
original = dm.get_combo_snapshot(e, cid)


def state():
    s = dm.get_combo_snapshot(e, cid)["combo"]
    return s["decision_state"], s["decided_department"]


def build_scenario():
    snap = dm.get_combo_snapshot(e, cid)
    dm.revert_combo(e, cid, "AJ")
    dm.record_recent_move(e, cid, src, label, n, "Send Back to Review", snap, "AJ")
    snap = dm.get_combo_snapshot(e, cid)
    dm.break_out_combo(e, cid, "AJ", upc_decisions={})
    dm.record_recent_move(e, cid, src, label, n, "Broken Out to UPC-Level", snap, "AJ")
    upcs = dm.get_pending_upc_overrides(e, cid)["upc"].tolist()[:2]
    dm.stage_broken_out_decisions(e, {u: {"department": "GROCERY", "combo_id": cid, "label": label,
                                          "description": None, "source_key": src} for u in upcs}, "Kristi")


def show_path():
    p = dm.get_combo_undo_path(e, cid)
    chain = [m["before"] for m in reversed(p["moves"])] + [p["current"]]
    print("   history:", " -> ".join(chain), "| staged:", p["staged"], "| authorized:", p["authorized"])
    return p


def check(ok, msg):
    print(("   PASS " if ok else "   FAIL ") + msg)


print("\n1) build scenario: Decided -> Crosswalk -> Broken Out + 2 staged")
build_scenario()
p = show_path()
check(len(p["moves"]) == 2 and p["has_staged"], "picker has 3 targets (staged only / Crosswalk / Decided)")

print("\n2) undo staged only (n=0)")
dm.undo_combo_to_stage(e, cid, 0, "Kristi")
p = show_path()
check(state()[0] == "broken_out" and not p["has_staged"] and len(p["moves"]) == 2, "still Broken Out, staged gone, moves intact")

print("\n3) undo all the way back to Decided (n=2)")
dm.undo_combo_to_stage(e, cid, 2, "AJ")
p = show_path()
check(state() == (original["combo"]["decision_state"], original["combo"]["decided_department"]), f"back to original {state()}")
check(not p["moves"] and not p["has_staged"], "no moves left")

print("\n4) rebuild, then undo only back to Crosswalk (n=1)")
build_scenario()
dm.undo_combo_to_stage(e, cid, 1, "Kristi")
p = show_path()
check(state() == ("not_reviewed", None), "back to undecided (pre-Break-Out state)")
check(len(p["moves"]) == 1 and not p["has_staged"], "one move left (the Send Back), staged gone")
with e.connect() as c:
    ov = c.execute(text("SELECT COUNT(*) FROM dbo.dept_mapping_upc_overrides WHERE combo_id=:c"), {"c": cid}).scalar()
check(ov == len(original["overrides"]), f"per-UPC rows match pre-break-out ({ov})")

print("\n5) undo remaining move -> Decided")
dm.undo_combo_to_stage(e, cid, 1, "AJ")
final = dm.get_combo_snapshot(e, cid)
check(final == original, "combo fully restored to its original state")
