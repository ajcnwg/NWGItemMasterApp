"""Part 1: Crosswalk approve, I-also-agree, dispute, notifications -> View,
vote, top-bar undo/redo via the confirm popup, admin override popup, admin
Team notifications. Three accounts: jason, kristi (editors), aj (admin)."""
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine

E = get_engine()
FAIL = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAIL.append(msg)


def show(at, prefix):
    at.session_state["dept_shared_filter"] = {"search": prefix, "sort_label": None, "sort_desc": True, "page_size": 25}


q = dm.get_review_queue(E, "review")
q = q[q["suggested_department"].notna()].sort_values("n_upcs_total")
A, B, C = [q.iloc[i] for i in range(3)]
print("groups:", [(int(r.combo_id), r.raw_subcategory, r.suggested_department, int(r.n_upcs_total)) for r in (A, B, C)])
lab = lambda r: r.raw_subcategory or r.raw_category

jason, kristi, aj = session("jason"), session("kristi"), session("aj")
for at in (jason, kristi, aj):
    run(at, "load " + at.session_state["name"])

# 1. jason approves A and B on Crosswalk
for r in (A, B):
    show(jason, lab(r))
    goto(jason, "Department Review", "Crosswalk")
    jason.button(key=f"approve_review_{int(r.combo_id)}").click()
    run(jason, f"jason approves {lab(r)}")
pend = dm.get_pending_changes(E)
check(int(A.combo_id) in pend and int(B.combo_id) in pend, "A and B staged by Jason")

# 2. kristi: I also agree on A
goto(kristi, "Department Review", "Pending Changes")
kristi.button(key=f"dept_pending_agree_{int(A.combo_id)}").click()
run(kristi, "kristi: I also agree on A")
backers = dm.get_combo_backers(E, [int(A.combo_id)])
check("Kristi" in backers.get(int(A.combo_id), set()), f"Kristi recorded as agreeing on A ({backers})")

# 3. kristi suggests a different dept on B -> dispute
other = next(d for d in ["FROZEN", "DELI", "GROCERY", "DAIRY"] if d != B.suggested_department)
kristi.selectbox(key=f"dept_pending_suggest_{int(B.combo_id)}").select(other)
run(kristi, "pick other dept")
kristi.button(key=f"dept_pending_update_{int(B.combo_id)}").click()
run(kristi, f"kristi suggests {other} on B")
sugg = dm.get_combo_suggestions(E, [int(B.combo_id)])
check(len({s["department"] for s in sugg.get(int(B.combo_id), [])}) == 2, "B is now disputed (2 departments)")

# 4. jason's notifications show the dispute; View takes him to it
jason = session("jason"); run(jason, "jason reload")
side = [m.value for m in jason.sidebar.markdown] + [c.value for c in jason.sidebar.caption]
check(any(lab(B) in x for x in side), "Jason's sidebar lists group B")
target = view_button_for(jason, lab(B))
check(target is not None, "found a View button for B")
if target:
    target.click(); run(jason, "jason clicks View")
    check(jason.session_state["active_tab"] == "Department Review" and jason.session_state["dept_review_subtab"] == "Pending Changes",
          "View opened Department Review / Pending Changes")
    body = texts(jason)
    check(lab(B) in body, "group B is visible on the page after View")

# 5. jason votes for kristi's department on B (resolves it)
b = jason.button(key=f"agree_combo_{int(B.combo_id)}_{other}")
b.click(); run(jason, f"jason votes {other} on B")
sugg = dm.get_combo_suggestions(E, [int(B.combo_id)])
check(len({s["department"] for s in sugg.get(int(B.combo_id), [])}) <= 1, f"B resolved after Jason's vote ({sugg.get(int(B.combo_id))})")
pend = dm.get_pending_changes(E)
check(pend.get(int(B.combo_id), {}).get("department") == other, f"B now staged as {other}")

# 6. top-bar undo (confirm popup) then redo
before_undo = dm.peek_undo_redo(E, "Jason")["undo"]
check(before_undo and before_undo["combo_id"] == int(B.combo_id), f"Jason's last action is the vote on B ({before_undo and before_undo['description']})")
jason.button(key="topbar_undo").click(); run(jason, "click top-bar Undo")
confirm = [x for x in jason.button if x.label == "Confirm undo"]
check(len(confirm) == 1, "Undo confirm popup shows 'Confirm undo'")
if confirm:
    confirm[0].click(); run(jason, "Confirm undo")
sugg = dm.get_combo_suggestions(E, [int(B.combo_id)])
check(len({s["department"] for s in sugg.get(int(B.combo_id), [])}) == 2, "after undo B is disputed again")
jason.button(key="topbar_redo").click(); run(jason, "click top-bar Redo")
confirm = [x for x in jason.button if x.label == "Confirm redo"]
check(len(confirm) == 1, "Redo confirm popup shows 'Confirm redo'")
if confirm:
    confirm[0].click(); run(jason, "Confirm redo")
pend = dm.get_pending_changes(E)
check(pend.get(int(B.combo_id), {}).get("department") == other, "after redo B is staged as the voted dept again")

# 7. kristi's undo of her "I also agree" (her last action is the suggestion on B, then agree on A)
k_undo = dm.peek_undo_redo(E, "Kristi")["undo"]
print("  kristi's last action:", k_undo and (k_undo["label"], k_undo["description"]))
kristi = session("kristi"); run(kristi, "kristi reload")
kristi.button(key="topbar_undo").click(); run(kristi, "kristi top-bar Undo")
c = [x for x in kristi.button if x.label == "Confirm undo"]
if c:
    c[0].click(); run(kristi, "kristi Confirm undo")
print("  toast:", [t.value for t in kristi.toast])
# Her suggestion on B was superseded by Jason's vote, so it must be refused, not clobber Jason
pend = dm.get_pending_changes(E)
check(pend.get(int(B.combo_id), {}).get("department") == other, "Kristi's stale undo didn't overwrite Jason's later vote")
kristi.button(key="topbar_undo").click(); run(kristi, "kristi Undo again (next action back)")
c = [x for x in kristi.button if x.label == "Confirm undo"]
if c:
    c[0].click(); run(kristi, "kristi Confirm undo")
backers = dm.get_combo_backers(E, [int(A.combo_id)])
check("Kristi" not in backers.get(int(A.combo_id), set()), "Kristi's I-also-agree on A undone")
check("Jason" in backers.get(int(A.combo_id), set()) or int(A.combo_id) in dm.get_pending_changes(E), "Jason's approval of A untouched")

# 8. aj: admin override popup on C
show(aj, lab(C)); goto(aj, "Department Review", "Crosswalk")
aj.button(key=f"approve_review_{int(C.combo_id)}").click(); run(aj, "aj approves C")
goto(aj, "Department Review", "Pending Changes")
ov = aj.button(key=f"admin_override_btn_ready_{int(C.combo_id)}")
ov.click(); run(aj, "aj opens admin override popup")
sb = [s for s in aj.selectbox if s.key == f"admin_override_dialog_dept_{int(C.combo_id)}"]
check(len(sb) == 1, "admin override popup has a department picker")
if sb:
    sb[0].select("GROCERY" if C.suggested_department != "GROCERY" else "FROZEN"); run(aj, "pick override dept")
    btns = [x.label for x in aj.button]
    print("  popup buttons:", [x for x in btns if "verride" in x or x in ("Cancel",)])
    go = [x for x in aj.button if x.label.lower().startswith("override") or x.label.lower().startswith("lock")]
    if go:
        go[0].click(); run(aj, "confirm override")
pend = dm.get_pending_changes(E)
check(pend.get(int(C.combo_id), {}).get("overridden_by") == "AJ", f"C overridden by AJ ({pend.get(int(C.combo_id))})")

# 9. aj: Team notifications
aj.session_state["notif_view"] = "Team"; run(aj, "aj Team view")
exp = [e.label for e in aj.sidebar.expander]
print("  team expanders:", exp)
check(any(x.startswith("Jason") for x in exp) and any(x.startswith("Kristi") for x in exp), "Team view has a section per person")
aj.session_state["notif_search"] = lab(B); run(aj, "aj searches Team")
print("  after search:", [e.label for e in aj.sidebar.expander])

# 10. editors don't see admin-only things
body = [x.key for x in jason.button if x.key]
check(not any(k.startswith("admin_override_btn") for k in body), "Jason sees no admin override buttons")
goto(jason, "Department Review", "Settings")
check(not any((x.label or "").startswith("Save") for x in jason.button), "Jason sees no Save buttons in Settings")
check(any(m.value == "#### Request a change" for m in jason.markdown), "Jason gets the request form instead of the admin settings")

print("\nFAILURES:", len(FAIL))
for f in FAIL:
    print(" -", f)
