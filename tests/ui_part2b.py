"""Part 2: Broken Out items across users — suggestions on someone else's
staged items (via the Pending Changes grid), notification -> View,
accept / deny, top-bar undo/redo of those, the group Undo… popup, Break Out
and Send Back popups, the Decided tab (send back, mark reviewed, grid)."""
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine()
FAIL = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def show(at, search):
    at.session_state["dept_shared_filter"] = {"search": search, "sort_label": None, "sort_desc": True, "page_size": 25}


def keyed(at, prefix):
    return [b for b in at.button if (b.key or "").startswith(prefix)]


def wb_ver(at, key):
    return at.session_state[f"wb_{key}"]["ver"] if f"wb_{key}" in at.session_state else 0


jason, kristi, aj = session("jason"), session("kristi"), session("aj")
for at in (jason, kristi, aj):
    run(at, "load " + at.session_state["name"])

# 6. Decided tab: send back a whole-group decision, and a Broken Out one (with/without auto)
dec = dm.get_decided_combos(E)
print("  decided statuses:", dec["status"].value_counts().to_dict())
whole = dec[dec["status"].str.startswith("Whole Group")].sort_values("n_upcs_total").iloc[0]
wc = int(whole.combo_id); wl = whole.raw_subcategory or whole.raw_category
show(aj, wl); goto(aj, "Department Review", "Decided")
b = [x for x in keyed(aj, f"revert_decided_{wc}") if x.key == f"revert_decided_{wc}"]
check(len(b) == 1, f"Decided whole group has a send-back button ({b and b[0].label})")
b[0].click(); run(aj, "aj sends whole group back")
sb = [x for x in aj.button if x.label == "Send it back"]
if sb:
    sb[0].click(); run(aj, "confirm")
with E.connect() as c:
    r = c.execute(text("SELECT decision_state, decided_department FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": wc}).one()
check(r[0] == "not_reviewed" and r[1] is None, f"whole group {wc} undecided again ({r})")

part = dec[dec["status"].isin(["Broken Out — Partially Auto", "Broken Out — Manually Decided"])].sort_values("n_upcs_total")
if not part.empty:
    pc = int(part.iloc[0].combo_id); pl = part.iloc[0].raw_subcategory or part.iloc[0].raw_category
    show(aj, pl); goto(aj, "Department Review", "Decided")
    keyed(aj, f"revert_decided_{pc}")[0].click(); run(aj, "aj ↩ Broken Out (partially auto)")
    labels = [x.label for x in aj.button]
    print("  reopen popup buttons:", [l for l in labels if l in ("Apply auto-decisions", "Start blank", "Send it back", "Reopen it", "Cancel") or "ack" in l])
    body = texts(aj)
    choice = next(x for x in aj.button if x.label in ("Apply auto-decisions", "Start blank") or x.label.startswith("Send"))
    choice.click(); run(aj, f"reopen via '{choice.label}'")
    with E.connect() as c:
        st_ = c.execute(text("SELECT decision_state FROM dbo.dept_mapping_combos WHERE combo_id=:c"), {"c": pc}).scalar()
    check(st_ == "broken_out", f"{pc} reopened in Broken Out")
full = dec[dec["status"] == "Broken Out — Fully Auto"].sort_values("n_upcs_total")
if not full.empty:
    fc = int(full.iloc[0].combo_id); fl = full.iloc[0].raw_subcategory or full.iloc[0].raw_category
    show(aj, fl); goto(aj, "Department Review", "Decided")
    keyed(aj, f"revert_decided_{fc}")[0].click(); run(aj, "aj ↩ Broken Out (fully auto)")
    labels = [x.label for x in aj.button]
    body = texts(aj)
    check("Apply auto-decisions" not in labels, "fully-auto group has no automation choice")
    check("re-applying auto-matching would just decide it all" in texts(aj), "fully-auto group shows the explanatory note")
    check(any("no decisions" in w.value for w in aj.warning), "send-back warns the group will have no decisions")
    print("  fully-auto popup buttons:", [l for l in labels if l in ("Send back", "Send it back", "Reopen", "Cancel") or "ack" in l][:5])
    cancel = [x for x in aj.button if x.label == "Cancel"]
    cancel[0].click(); run(aj, "cancel")

# 7. Decided: Mark as reviewed / confirm auto items; Decided grid stages a change
dec = dm.get_decided_combos(E)
auto_whole = dec[dec["status"] == "Whole Group — Auto"].sort_values("n_upcs_total").iloc[0]
ac = int(auto_whole.combo_id)
show(jason, auto_whole.raw_subcategory or auto_whole.raw_category); goto(jason, "Department Review", "Decided")
mb = keyed(jason, f"confirm_whole_{ac}")
check(len(mb) == 1, "auto whole group has Mark as reviewed")
mb[0].click(); run(jason, "jason Mark as reviewed")
jason.button(key="topbar_undo").click(); run(jason, "undo mark-reviewed")
c = [x for x in jason.button if x.label == "Confirm undo"]; c[0].click(); run(jason, "confirm")
check(dm.peek_undo_redo(E, "Jason")["redo"]["description"] == "Marked as reviewed", "Mark as reviewed undone (now on redo)")

dbo = dec[dec["status"].str.startswith("Broken Out")].sort_values("n_upcs_total")
dbo = dbo[dbo["n_upcs_total"] >= 3].iloc[0]
dc = int(dbo.combo_id)
show(kristi, dbo.raw_subcategory or dbo.raw_category); goto(kristi, "Department Review", "Decided")
k = f"dc_{dc}"
v = wb_ver(kristi, k)
now = dm.get_combo_upc_decisions(E, dc)["department"].value_counts()
target = next(d for d in ("DELI", "GROCERY", "FROZEN") if d not in set(now.index))  # a real change for every item
kristi.selectbox(key=f"wb_pick_{k}_{v}").select(target)
kristi.button(key=f"wb_setall_{k}_{v}").click(); run(kristi, "kristi sets all rows on Decided grid")
v = wb_ver(kristi, k)
kristi.button(key=f"wb_stage_{k}_{v}").click(); run(kristi, "kristi Stage changes")
staged = [u for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == dc]
check(len(staged) > 0, f"Decided-grid change staged to Pending ({len(staged)} item(s))")

print("\nFAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
