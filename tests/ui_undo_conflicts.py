"""Undo / Redo when other people change the same groups — Jason, Kristi, AJ in separate sessions."""
from testbase import BASE, IMPORT_SNAP, KEEP
import io
import threading
import pandas as pd
import streamlit as st_mod
from uiharness import *
from itemmaster import dept_mapping as dm
from itemmaster.db import get_engine
from sqlalchemy import text

E = get_engine(); F = []; N = [0]


def check(ok, msg):
    N[0] += 1
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        F.append(msg)


q = dm.get_review_queue(E, "review")
q = q[q["suggested_department"].notna() & q["n_upcs_total"].between(1, 20) & ~q["combo_id"].isin(list(dm.get_pending_changes(E)))].sort_values("combo_id").head(6)
G = [dict(r) for r in q.to_dict("records")]
BO = 197
ids = [int(g["combo_id"]) for g in G] + [BO]
orig = {c: dm.get_combo_snapshot(E, c) for c in ids}
lab = lambda g: max([g["raw_category"] or "", g["raw_subcategory"] or ""], key=len)
other = lambda g: "DELI" if g["suggested_department"] != "DELI" else "FROZEN"


def reset():
    with E.begin() as c:
        for cid in ids:
            for t in ("dept_mapping_recent_moves",) + dm.STAGED_TABLES:
                c.execute(text(f"DELETE FROM dbo.{t} WHERE combo_id=:c"), {"c": cid})
            dm._restore_combo_snapshot(c, cid, orig[cid], "test")
        c.execute(text("DELETE FROM dbo.dept_mapping_action_log"))
        c.execute(text("DELETE FROM dbo.user_workspace WHERE username IN ('Jason','Kristi')"))
        c.execute(text("DELETE FROM dbo.dept_push_approvals"))
    for who in ("Jason", "Kristi", "AJ"):
        dm.release_broken_out_claim(E, BO, who, is_admin=True)
    st_mod.cache_data.clear(); GRID_EDITS.clear()


def fresh(user):
    at = session(user); run(at, f"load {user}")
    return at


def crosswalk_approve(at, g, dept=None):
    at.session_state["dept_shared_filter"] = {"search": lab(g), "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(at, "Department Review", "Crosswalk")
    if dept:
        at.selectbox(key=f"dept_choice_review_{g['combo_id']}").select(dept); run(at, "pick")
    at.button(key=f"approve_review_{g['combo_id']}").click(); run(at, f"approve {lab(g)}")


def pending_suggest(at, g, dept):
    goto(at, "Department Review", "Pending Changes")
    at.selectbox(key=f"dept_pending_suggest_{g['combo_id']}").select(dept); run(at, "pick")
    at.button(key=f"dept_pending_update_{g['combo_id']}").click(); run(at, f"suggest {dept}")


def topbar(at, kind):
    at = fresh(at.session_state["username"])
    at.button(key=f"topbar_{kind}").click(); run(at, f"top-bar {kind}")
    c = [b for b in at.button if b.label == f"Confirm {kind}"]
    if not c:
        return at, [i.value for i in at.info]
    c[0].click(); run(at, "confirm")
    return at, [t.value for t in at.toast]


def votes(g):
    return sorted((s["staged_by"], s["department"]) for s in dm.get_combo_suggestions(E, [int(g["combo_id"])]).get(int(g["combo_id"]), []))


def pend(g):
    return dm.get_pending_changes(E).get(int(g["combo_id"]))


try:
    reset()
    print("\n== 1. Someone else votes on my group after I approved it")
    j = fresh("jason"); crosswalk_approve(j, G[0])
    k = fresh("kristi"); pending_suggest(k, G[0], other(G[0]))
    before = votes(G[0])
    j, msg = topbar(j, "undo")
    print("   ", msg)
    check(msg and "Can't undo" in msg[0] and "Kristi changed it since" in msg[0], "Jason's Undo is refused and names Kristi")
    check(votes(G[0]) == before, "Kristi's vote (and Jason's) untouched")
    j, msg = topbar(j, "undo")
    check(msg and "Nothing to undo" in msg[0], "that step is retired — nothing left to undo")

    print("\n== 2. A conflict on one group doesn't block Undo on another")
    reset()
    j = fresh("jason"); crosswalk_approve(j, G[1]); crosswalk_approve(j, G[2])
    k = fresh("kristi"); pending_suggest(k, G[2], other(G[2]))
    j, msg = topbar(j, "undo")
    check(msg and "Kristi changed it since" in msg[0], "Undo on the changed group refused")
    j, msg = topbar(j, "undo")
    check(msg and msg[0].startswith("Undone") and pend(G[1]) is None, "the next Undo (other group) goes through")
    check(len(votes(G[2])) == 2, "the refused group is left exactly as the others left it")

    print("\n== 3. Redo after someone else acted")
    k = fresh("kristi"); crosswalk_approve(k, G[1], dept=other(G[1]))
    j, msg = topbar(j, "redo")
    print("   ", msg)
    check(msg and "Can't redo" in msg[0] and "Kristi" in msg[0], "Jason's Redo refused, names Kristi")
    check(pend(G[1]) and pend(G[1])["staged_by"] == "Kristi", "Kristi's staging untouched")

    print("\n== 4. Undo after an admin pushed it live")
    reset()
    j = fresh("jason"); crosswalk_approve(j, G[3])
    a = fresh("aj"); goto(a, "Department Review", "Pending Changes")
    a.checkbox(key="confirm_push_dept_changes").check(); run(a, "tick")
    a.button(key="push_pending_changes").click(); run(a, "AJ pushes")
    check(dm.get_combo_snapshot(E, int(G[3]["combo_id"]))["combo"]["decided_department"] == G[3]["suggested_department"], "pushed live")
    j, msg = topbar(j, "undo")
    print("   ", msg)
    check(msg == ['Nothing to undo.'], "after the push, Jason's Undo has nothing to take back (the push retired it)")
    check(dm.get_combo_snapshot(E, int(G[3]["combo_id"]))["combo"]["decided_department"] == G[3]["suggested_department"], "pushed decision untouched")

    print("\n== 5. Broken Out: my staged items, then another person's suggestion / an admin override")
    reset()
    j = fresh("jason")
    j.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(j, "Department Review", "Broken Out")
    j.button(key=f"claim_broken_out_{BO}").click(); run(j, "claim")
    v = j.session_state[f"wb_bo_{BO}"]["ver"]
    j.selectbox(key=f"wb_pick_bo_{BO}_{v}").select("FROZEN"); j.button(key=f"wb_setall_bo_{BO}_{v}").click(); run(j, "set all")
    j.button(key=f"bo_stage_all_{BO}").click(); run(j, "stage")
    staged = {u: c for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == BO}
    k = fresh("kristi"); k.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(k, "Department Review", "Pending Changes")
    kv = k.session_state[f"wb_pc_ready_{BO}"]["ver"] if f"wb_pc_ready_{BO}" in k.session_state else 0
    edit_grid(k, f"wb_grid_pc_ready_{BO}_{kv}", {0: {"New Department": "DAIRY"}}); run(k, "Kristi changes one of Jason's items")
    k.button(key=f"wb_stage_pc_ready_{BO}_{k.session_state[f'wb_pc_ready_{BO}']['ver']}").click(); run(k, "Kristi Apply changes")
    check(len(dm.get_upc_change_suggestions(E, [BO])) == 1, "Kristi's change became a suggestion for Jason")
    j, msg = topbar(j, "undo")
    print("   ", msg)
    check(msg and "Kristi changed it since" in msg[0], "Jason's Undo of the stage refused (Kristi)")
    check(len({u for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == BO}) == len(staged)
          and len(dm.get_upc_change_suggestions(E, [BO])) == 1, "Jason's staged items and Kristi's suggestion both intact")
    a = fresh("aj"); a.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(a, "Department Review", "Pending Changes")
    next(b for b in a.button if (b.key or "").startswith("admin_override_btn_group_") and b.key.endswith(f"_{BO}")).click(); run(a, "admin popup")
    a.selectbox(key=f"admin_override_dialog_group_dept_{BO}").select("GROCERY"); run(a, "pick")
    next(b for b in a.button if b.label == "Override whole group").click(); run(a, "AJ overrides whole group")
    k, msg = topbar(k, "undo")
    print("   ", msg)
    check(msg and "AJ changed it since" in msg[0], "Kristi's Undo refused (AJ's override)")
    check(set(c["department"] for u, c in dm.get_pending_upc_changes(E).items() if c["combo_id"] == BO) == {"GROCERY"}, "AJ's override untouched")
    a, msg = topbar(a, "undo")
    check(msg and msg[0].startswith("Undone"), "AJ can still undo his own override (nobody changed it after)")

    print("\n== 6. Excel staged, then someone else changes the group")
    reset()
    AU = dm.get_auto_decided_upc_overrides(E, BO)["upc"].tolist()
    buf = io.BytesIO(); pd.DataFrame([{"UPC": u, "Department": "USE AUTO/SUGGESTED"} for u in AU[:3]]).to_excel(buf, index=False)
    real = st_mod.file_uploader
    st_mod.file_uploader = lambda label, *a_, key=None, **kw: type("U", (io.BytesIO,), {"name": "x.xlsx"})(buf.getvalue()) if key and key.startswith("grp_up_") else real(label, *a_, key=key, **kw)
    j = fresh("jason"); j.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(j, "Department Review", "Broken Out")
    j.button(key=f"claim_broken_out_{BO}").click(); run(j, "claim + upload")
    st_mod.file_uploader = real
    next(b for b in j.button if (b.key or "").startswith("grp_stage_")).click(); run(j, "Stage from Excel")
    a = fresh("aj"); a.session_state["dept_shared_filter"] = {"search": "FS FROZ MEXICAN", "sort_label": None, "sort_desc": True, "page_size": 25}
    goto(a, "Department Review", "Pending Changes")
    next(b for b in a.button if (b.key or "").startswith("admin_override_btn_group_") and b.key.endswith(f"_{BO}")).click(); run(a, "admin popup")
    a.selectbox(key=f"admin_override_dialog_group_dept_{BO}").select("GROCERY"); run(a, "pick")
    next(b for b in a.button if b.label == "Override whole group").click(); run(a, "AJ overrides")
    j, msg = topbar(j, "undo")
    check(msg and "Can't undo" in msg[0], "Jason's Undo of the Excel stage refused")
    check(not any("x.xlsx" in m.value and "not used yet" in m.value for m in j.markdown), "…and the file is NOT re-offered (nothing was undone)")

    print("\n== 7. Undo… popup open while someone else changes the group")
    reset()
    j = fresh("jason"); crosswalk_approve(j, G[4])
    goto(j, "Department Review", "Pending Changes")
    j.button(key=f"undo_pending_{G[4]['combo_id']}").click(); run(j, "Jason opens Undo…")
    k = fresh("kristi"); pending_suggest(k, G[4], other(G[4]))
    j.button(key=f"undo_to_{G[4]['combo_id']}_0").click(); run(j, "Jason confirms (stale popup)")
    print("   ", [t.value for t in j.toast])
    check(len(votes(G[4])) == 2, "popup undo refused — Kristi's vote intact")

    print("\n== 8. Two Undo clicks at the same instant")
    reset()
    j = fresh("jason"); crosswalk_approve(j, G[5]); crosswalk_approve(j, G[0])
    out = []
    ths = [threading.Thread(target=lambda: out.append(dm.undo_last_action(E, "Jason"))) for _ in range(2)]
    [t.start() for t in ths]; [t.join() for t in ths]
    print("   ", [(r["ok"], r.get("reason"), r.get("entry", {}).get("combo_id")) for r in out])
    check(all(r["ok"] for r in out) and {r["entry"]["combo_id"] for r in out} == {int(G[5]["combo_id"]), int(G[0]["combo_id"])},
          "both went through, one step each — no step wrongly retired")
    with E.connect() as c:
        st_ = c.execute(text("SELECT status FROM dbo.dept_mapping_action_log WHERE actor='Jason'")).scalars().all()
    check(sorted(st_) == ["undone", "undone"], f"both steps can be redone ({st_})")
    r1, r2 = dm.redo_last_action(E, "Jason"), dm.redo_last_action(E, "Jason")
    check(r1["ok"] and r2["ok"] and pend(G[0]) and pend(G[5]), "Redo twice puts both back")

    print("\n== 9. A snapshot restore wipes everyone's undo history")
    j = fresh("jason"); crosswalk_approve(j, G[1])
    check(dm.peek_undo_redo(E, "Jason")["undo"] is not None, "Jason has something to undo")
finally:
    reset()
    print("\n  restoring #51 (also the restore test)…")
    dm.restore_snapshot(E, BASE, "AJ")
    check(dm.peek_undo_redo(E, "Jason") == {"undo": None, "redo": None}, "after a restore, nobody has Undo steps pointing at old states")
    print("  live vs #51:", dm.compare_snapshot_to_live(E, BASE))
print(f"\n{N[0] - len(F)} of {N[0]} passed. FAILURES: {len(F)}")
for f in F:
    print(" -", f)
