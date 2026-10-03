"""Notifications are personal: each person hears only about work their account
touched (staged, voted on, claimed), never their own actions, and never other
people's work they had no part in. Admins hear about pushes. Every note's Open
goes straight to what it's about."""
import uuid
from uiharness import *
import streamlit as st_mod
from sqlalchemy import text
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine

E = get_engine()
F = []


def check(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


def notes(user, kinds=None):
    with E.connect() as c:
        rows = c.execute(text("SELECT kind, combo_id, detail, actor, link FROM dbo.user_notifications WHERE username = :u "
                              "AND note_id > :n ORDER BY note_id"), {"u": user, "n": START}).mappings().all()
    return [dict(r) for r in rows if kinds is None or r["kind"] in kinds]


with E.begin() as c:
    dm._ensure_notes(c)
    START = c.execute(text("SELECT ISNULL(MAX(note_id), 0) FROM dbo.user_notifications")).scalar()

q = dm.get_review_queue(E, "review")
q = q[q["suggested_department"].notna() & ~q["combo_id"].isin(set(dm.get_pending_changes(E)))].head(3)
A, B, C = [r for _, r in q.iterrows()]
lab = lambda r: " / ".join(b for b in (r["raw_department"], r["raw_category"], r["raw_subcategory"]) if b)
DEPTS = dm.get_departments(E).iloc[:, 0].tolist()
other = lambda d: next(x for x in DEPTS if x != d)


def vote(who, r, dept):
    return dm.upsert_combo_suggestion(E, int(r["combo_id"]), "review", dept, r["source_key"], lab(r), int(r["n_upcs_total"]), who)


print("\n== 1. A vote against you; nobody else hears")
vote("Jason", A, A["suggested_department"])
check(not notes("Jason") and not notes("Kristi"), "staging your own group tells nobody")
vote("Kristi", A, other(A["suggested_department"]))
j = notes("Jason")
check(any(n["kind"] == "vote" and "needs agreement" in n["detail"] and n["actor"] == "Kristi" for n in j),
      f"Jason hears Kristi voted against him ({[n['detail'] for n in j]})")
check(not notes("Kristi"), "Kristi hears nothing about her own vote")
check(not notes("Eric") and not notes("AJ"), "Eric (not involved) and AJ hear nothing")
live = dm.get_notifications(E, "Jason", dm.start_visit(E, "Jason"))
check(any(n["kind"] == "dispute" and n["combo_id"] == int(A["combo_id"]) for n in live["action"]),
      "Jason: 'Needs agreement' is waiting on him")
check(not any(n["kind"] == "vote" and n["combo_id"] == int(A["combo_id"]) for n in live["updates"]),
      "…and not said twice (the vote note is folded into it while it's in dispute)")

print("\n== 2. Agreement")
vote("Jason", A, other(A["suggested_department"]))
k = notes("Kristi", {"agreed"})
check(any(n["actor"] == "Jason" for n in k), f"Kristi hears it came to agreement ({[n['detail'] for n in k]})")

print("\n== 3. A push: whose work went live hears; the pusher and uninvolved don't; admins get the report")
vote("Jason", B, B["suggested_department"])
vote("Kristi", C, C["suggested_department"])
for who in ("kristi", "eric"):
    at = session(who); run(at, f"{who} load"); goto(at, "Department Review", "Pending Changes")
    at.button(key="approve_dept_push").click(); run(at, f"{who} approves the batch")
st_mod.cache_data.clear()
jp = session("jason"); run(jp, "Jason load"); goto(jp, "Department Review", "Pending Changes")
before = {u: len(notes(u)) for u in ("Jason", "Kristi", "Eric", "AJ")}
jp.checkbox(key="confirm_push_dept_changes").check(); run(jp, "tick")
push = [b for b in jp.button if b.key == "push_pending_changes"]
check(bool(push) and not push[0].disabled, "Jason (with 2 approvals) can push")
if push:
    push[0].click(); run(jp, "Jason pushes")
kp = [n for n in notes("Kristi", {"pushed"}) if n["combo_id"] == int(C["combo_id"])]
check(bool(kp) and kp[0]["actor"] == "Jason", f"Kristi hears her staged group went live in Jason's push ({[n['detail'] for n in kp]})")
check(not notes("Jason", {"pushed"}), "Jason hears nothing about his own push")
check(len(notes("Eric", {"pushed"})) == 0, "Eric (none of his work in it) hears nothing")
ap = notes("AJ", {"push_report"})
check(len(ap) == 1 and '"push"' in (ap[0]["link"] or ""), f"AJ (admin) gets one note for the push, opening its report ({ap})")

print("\n== 4. Open goes straight there")
st_mod.cache_resource.clear()
a = session("aj"); run(a, "AJ load")
target = view_button_for(a, "Jason pushed")
check(target is not None, "AJ's push note has an Open button")
if target:
    target.click(); run(a, "AJ opens the push note")
    check(a.session_state["active_tab"] == "Upload Reports", "…which opens Upload Reports")
    body = " ".join(m.value for m in a.main.markdown)
    check("Jason** pushed" in body, "…on that push's report")
st_mod.cache_resource.clear()
k2 = session("kristi"); run(k2, "Kristi load")
side = " ".join(m.value for m in k2.sidebar.markdown) + " ".join(c.value for c in k2.sidebar.caption)
check("pushed your decision" in side, "Kristi's sidebar says her decision went live")
ko = view_button_for(k2, lab(C))
check(ko is not None, "…with an Open button")
if ko:
    ko.click(); run(k2, "Kristi opens it")
    check(k2.session_state["active_tab"] == "Department Review" and k2.session_state["dept_review_subtab"] == "Decided",
          "…which takes her to that group on Decided")

print("\n== 5. A claim that lapses tells its holder")
bo = dm.get_broken_out_combos(E) if hasattr(dm, "get_broken_out_combos") else None
with E.begin() as c:
    cid = c.execute(text("SELECT TOP 1 combo_id FROM dbo.dept_mapping_combos WHERE decision_state = 'broken_out' "
                         "AND combo_id NOT IN (SELECT combo_id FROM dbo.dept_mapping_broken_out_claims)")).scalar()
dm.claim_broken_out_group(E, int(cid), "Eric")
with E.begin() as c:
    c.execute(text("UPDATE dbo.dept_mapping_broken_out_claims SET last_activity_at = DATEADD(hour, -3, SYSUTCDATETIME()) "
                   "WHERE combo_id = :c"), {"c": int(cid)})
dm.get_broken_out_claims(E)
check(any(n["kind"] == "claim" and "lapsed" in n["detail"] for n in notes("Eric")), "Eric hears his claim lapsed")
check(not any(n["kind"] == "claim" for n in notes("Jason") + notes("Kristi")), "…and nobody else does")

print(f"\n{len(F)} failure(s)" + (": " + "; ".join(F) if F else ""))
print("FAILURES:", len(F))
