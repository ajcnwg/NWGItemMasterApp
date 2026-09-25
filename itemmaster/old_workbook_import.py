"""
Admin-only: bring the decisions PEOPLE made in an old department workbook
(the Excel process from before this app — "..._department_workbook.xlsx")
into the app. Nothing auto-applied is taken, only what someone decided:

  * Crosswalk / Unmatched groups someone approved (their Manual Override
    Department if they typed one, otherwise New Department);
  * Decided groups someone manually overrode;
  * items someone manually reviewed or approved (Department UPC Overrides,
    Decided UPC Overrides) and anything on Final UPC Overrides;
  * the moves someone asked for (Break Out, Return to Crosswalk / Unmatched,
    Send to Broken Out, Move to Undecided).

Each one lands where it belongs in the app: an app group whose items ALL
get one Department gets a group decision, an item in a Broken Out group
gets that group's item decision, anything else gets a UPC override of just
its Department. Anything the app ALREADY does — the group already decided
that way, the item already has that Department, the move already made, or
the same thing already staged — is skipped and shown as "Already done".
Everything else is staged to Pending Changes (moves happen straight away,
as they do in the app), and every line is in the report.
"""

import io
from collections import defaultdict

import openpyxl
import pandas as pd
from sqlalchemy import text

from itemmaster import dept_mapping as dm
from itemmaster.ingest import clean_upc

SOURCE_KEYS = {"KEHE": "kehe", "SPINS": "spins", "C&S PNW": "cs_pnw", "C&S CA": "cs_ca", "URM": "urm",
               "SCAN ADVANTAGE": "nwg", "NWG": "nwg"}
SHEETS = ["Department Mapping Crosswalk", "Department Mapping Unmatched", "Department Mapping Broken Out",
          "Department Mapping Decided", "Decided Broken Out Combos", "Department UPC Overrides",
          "Decided UPC Overrides", "Final UPC Overrides", "Department Mapping Detail"]
KEY = ["Source", "Old Department", "Category", "Subcategory"]
DONE = "Already done"
STAGE = "Will be staged"
MOVE = "Will be moved now"
SKIP = "Skipped"


def _s(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def read_workbook(file) -> dict:
    """The sheets this needs, as text DataFrames (headers are on row 2 of
    every sheet). Missing sheets come back empty."""
    wb = openpyxl.load_workbook(file, read_only=True, data_only=True)
    out = {}
    for name in SHEETS:
        if name not in wb.sheetnames:
            out[name] = pd.DataFrame()
            continue
        rows = wb[name].iter_rows(min_row=2, values_only=True)
        header = next(rows, None) or ()
        width = max((i + 1 for i, h in enumerate(header) if h is not None), default=0)
        cols = [_s(h) for h in header[:width]]
        data = [[_s(v) for v in r[:width]] for r in rows if any(v not in (None, "") for v in r[:width])]
        out[name] = pd.DataFrame(data, columns=cols)
    wb.close()
    if all(out[n].empty for n in SHEETS[:8]):
        raise ValueError("This doesn't look like an old department workbook — none of its Department Mapping sheets were found.")
    return out


def _key(r) -> tuple:
    return tuple(_s(r.get(k)).upper() for k in KEY)


def _label(key) -> str:
    src, *bits = key
    bits = [b for b in bits if b]
    return f"{src} — {' / '.join(bits) if bits else '(blank)'}"


def extract(sheets: dict) -> dict:
    """Only what a person decided. Returns {"groups": [...], "items": [...],
    "moves": [...]} in the old workbook's own terms."""
    groups, items, moves = [], [], []
    for sheet, area in (("Department Mapping Crosswalk", "Crosswalk"), ("Department Mapping Unmatched", "Unmatched")):
        for r in sheets[sheet].to_dict("records"):
            action = _s(r.get("Action"))
            if action == "Approve" or _s(r.get("Approved?")) == "Yes":
                dept = _s(r.get("Manual Override Department")) or _s(r.get("New Department"))
                if dept:
                    groups.append({"key": _key(r), "department": dept, "from": f"Old {area}: approved group"})
            elif action.startswith("Break Out") or _s(r.get("Break Out to UPC-Level?")) == "Yes":
                moves.append({"key": _key(r), "move": "to_broken_out", "from": f"Old {area}: Break Out to UPC-Level"})
    for r in sheets["Department Mapping Decided"].to_dict("records"):
        action, manual = _s(r.get("Action")), _s(r.get("Manual Override Department"))
        if manual and action in ("", "Keep (No Change)", "Manually Overridden"):
            groups.append({"key": _key(r), "department": manual, "from": "Old Decided: manually overridden group"})
        elif action == "Send to Broken Out":
            moves.append({"key": _key(r), "move": "to_broken_out", "from": "Old Decided: Send to Broken Out"})
        elif action.startswith("Return to"):
            moves.append({"key": _key(r), "move": "to_review", "from": f"Old Decided: {action}"})
    for r in sheets["Department Mapping Broken Out"].to_dict("records"):
        if _s(r.get("Action")).startswith("Return to"):
            moves.append({"key": _key(r), "move": "to_review", "from": "Old Broken Out: Return to Crosswalk/Unmatched"})
    for r in sheets["Decided Broken Out Combos"].to_dict("records"):
        action = _s(r.get("Action"))
        if action.startswith("Return to"):
            moves.append({"key": _key(r), "move": "to_review", "from": f"Old Decided Broken Out: {action}"})
        elif action.startswith("Move to Undecided"):
            moves.append({"key": _key(r), "move": "reopen", "from": "Old Decided Broken Out: Move to Undecided"})
        elif action.startswith("Move to Decided Combos") and _s(r.get("New Department")):
            moves.append({"key": _key(r), "move": "to_review", "from": "Old Decided Broken Out: Move to Decided Combos"})
            groups.append({"key": _key(r), "department": _s(r.get("New Department")),
                           "from": "Old Decided Broken Out: Move to Decided Combos"})
    for sheet in ("Department UPC Overrides", "Decided UPC Overrides"):
        for r in sheets[sheet].to_dict("records"):
            via, manual = _s(r.get("Decided Via")), _s(r.get("Manual Override Department"))
            if via.startswith("Manually") or manual:
                dept = manual or _s(r.get("Final Department")) or _s(r.get("New Department"))
                if dept:
                    items.append({"upc": clean_upc(r.get("UPC")), "department": dept,
                                  "from": f"Old {sheet}: {via or 'manual override'}", "rank": 2})
    for r in sheets["Final UPC Overrides"].to_dict("records"):
        if _s(r.get("UPC")) and _s(r.get("New Department")):
            items.append({"upc": clean_upc(r.get("UPC")), "department": _s(r.get("New Department")),
                          "from": "Old Final UPC Overrides", "rank": 3})
    # A group's items, from the old workbook's own Detail sheet.
    detail = sheets["Department Mapping Detail"]
    members = defaultdict(list)
    if not detail.empty:
        for r in detail.to_dict("records"):
            members[_key(r)].append(clean_upc(r.get("UPC")))
    for g in groups:
        g["upcs"] = members.get(g["key"], [])
    return {"groups": groups, "items": items, "moves": moves}


def _app_groups(engine) -> pd.DataFrame:
    return pd.read_sql(text(
        "SELECT combo_id, source_key, raw_department, raw_category, raw_subcategory, tier, n_evidence, "
        "decision_state, decided_department, manual_department, n_upcs_total FROM dbo.dept_mapping_combos"), engine)


def _where(state: str, tier, n_evidence) -> str:
    if state in ("broken_out",):
        return "Broken Out"
    if state in ("decided", "decided_broken_out"):
        return "Decided"
    return "Unmatched" if dm.origin_tier(tier, n_evidence) == "unmatched" else "Crosswalk"


def plan(engine, extracted: dict, departments: list, actor: str) -> dict:
    """Works out, line by line, what importing would do — nothing is written."""
    canon = {d.upper(): d for d in departments}
    combos = _app_groups(engine)
    combos["label"] = combos.apply(lambda c: f"{(c.source_key or '').upper()} — " + (
        " / ".join(b for b in (c.raw_department, c.raw_category, c.raw_subcategory) if b) or "(blank)"), axis=1)
    by_key = {}
    records = [{**c, "decided_department": _s(c["decided_department"]) or None,  # database NULLs, not NaN
                "manual_department": _s(c["manual_department"]) or None} for c in combos.to_dict("records")]
    for c in records:
        by_key[(c["source_key"], _s(c["raw_department"]).upper(), _s(c["raw_category"]).upper(),
                _s(c["raw_subcategory"]).upper())] = c
    state = {c: s for c, s in zip(combos.combo_id, combos.decision_state)}
    info = {c["combo_id"]: c for c in records}

    def app_group(old_key):
        src, *rest = old_key
        return by_key.get((SOURCE_KEYS.get(src, src.lower()), *rest))

    with engine.connect() as conn:
        mem = pd.read_sql(text("SELECT upc, combo_id FROM dbo.dept_mapping_combo_upcs"), conn)
        pend_groups = {r[0]: (r[1], r[2]) for r in conn.execute(text(
            "SELECT combo_id, department, staged_by FROM dbo.dept_mapping_pending_changes")).all()}
    combo_of = dict(zip(mem.upc, mem.combo_id))

    # 1. Moves
    move_rows = []
    for m in extracted["moves"]:
        c = app_group(m["key"])
        row = {"Group (old workbook)": _label(m["key"]), "Asked for": m["from"], "App group": "", "Now": "",
               "Result": "", "combo_id": None, "move": m["move"]}
        if c is None:
            row.update(Result=f"{SKIP} — no matching group in the app")
        else:
            s = state[c["combo_id"]]
            row.update({"App group": c["label"], "Now": _where(s, c["tier"], c["n_evidence"]), "combo_id": c["combo_id"]})
            done = ((m["move"] == "to_broken_out" and s in ("broken_out", "decided_broken_out"))
                    or (m["move"] == "to_review" and s == "not_reviewed" and not c["decided_department"])
                    or (m["move"] == "reopen" and s == "broken_out"))
            if m["move"] == "reopen" and s != "decided_broken_out" and not done:
                row["Result"] = f"{SKIP} — it isn't a finished Broken Out group in the app"
            elif done:
                row["Result"] = f"{DONE} — already {row['Now']}"
            else:
                row["Result"] = MOVE
                state[c["combo_id"]] = {"to_broken_out": "broken_out", "to_review": "not_reviewed",
                                        "reopen": "broken_out"}[m["move"]]
        move_rows.append(row)

    # 2. Every item's wanted Department (item-level beats group-level, the last sheet wins ties)
    want, why = {}, {}
    for g in extracted["groups"]:
        for u in g["upcs"]:
            if u not in want or why[u][1] < 1:
                want[u], why[u] = g["department"], (g["from"], 1)
    for it in sorted(extracted["items"], key=lambda x: x["rank"]):
        want[it["upc"]], why[it["upc"]] = it["department"], (it["from"], it["rank"])
    bad_dept = {u for u, d in want.items() if d.upper() not in canon}
    want = {u: canon.get(d.upper(), d) for u, d in want.items()}

    # 3. Whole app groups where every item gets the same Department
    group_rows, whole = [], set()
    members = mem[mem.upc.isin(want)].groupby("combo_id").size()
    sizes = mem.groupby("combo_id").size()
    for cid in members.index:
        if members[cid] != sizes[cid]:
            continue
        ups = mem.upc[mem.combo_id == cid]
        depts = {want[u] for u in ups}
        s = state[cid]
        if len(depts) != 1 or s not in ("not_reviewed", "decided") or ups.isin(bad_dept).any():
            continue
        d = depts.pop()
        whole |= set(ups)
        c = info[cid]
        now = c["decided_department"] if s == "decided" or c["decided_department"] else None
        pend = pend_groups.get(cid)
        row = {"Area": _where(s, c["tier"], c["n_evidence"]), "Group": c["label"], "Items": int(sizes[cid]),
               "Now": now or "(undecided)", "Will be": d, "From (old workbook)": why[ups.iloc[0]][0],
               "combo_id": int(cid), "tier": c["tier"], "n_evidence": c["n_evidence"],
               "source_key": c["source_key"], "n_upcs_total": int(c["n_upcs_total"])}
        if now == d:
            how = "by a person" if (s == "decided" or c["manual_department"]) else "automatically"
            row["Result"] = f"{DONE} — already decided as {d} ({how})"
        elif pend and pend[0] == d:
            row["Result"] = f"{DONE} — already staged ({pend[1]})"
        elif pend and pend[1] != actor:
            row["Result"] = f"{SKIP} — {pend[1]} already has {pend[0]} staged for this group"
        else:
            row["Result"] = STAGE
        group_rows.append(row)

    # 4. Every other item
    rest = [u for u in want if u not in whole]
    with engine.connect() as conn:
        live = dm._in_chunks(conn, "SELECT upc, description, department FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", rest)
        bo = dm._in_chunks(conn, "SELECT upc, combo_id, department FROM dbo.dept_mapping_upc_overrides WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", rest)
        pu = dm._in_chunks(conn, "SELECT upc, department, staged_by FROM dbo.dept_mapping_pending_upc_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", rest)
        pi = dm._in_chunks(conn, "SELECT upc, department, staged_by FROM dbo.item_master_pending_changes WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", rest)
    live = live.set_index("upc").to_dict("index") if not live.empty else {}
    bo = bo.set_index("upc").to_dict("index") if not bo.empty else {}
    pu = pu.set_index("upc").to_dict("index") if not pu.empty else {}
    pi = pi.set_index("upc").to_dict("index") if not pi.empty else {}
    item_rows = []
    for u in rest:
        d = want[u]
        cid = combo_of.get(u)
        c = info.get(cid) if cid is not None else None
        s = state.get(cid)
        it = live.get(u)
        row = {"UPC": u, "Description": (it or {}).get("description"), "Group": c["label"] if c else "(no group)",
               "Area": _where(s, c["tier"], c["n_evidence"]) if c else "Item master", "Now": (it or {}).get("department"),
               "Will be": d, "From (old workbook)": why[u][0], "Goes to": "", "Result": "", "combo_id": cid}
        if it is None:
            row.update(Result=f"{SKIP} — not in the item master")
        elif u in bad_dept:
            row.update(Result=f"{SKIP} — “{d}” isn't one of the app's Departments (add it in Settings first)")
        elif s in ("broken_out", "decided_broken_out") and (u in bo or s == "broken_out"):
            row["Goes to"] = "Broken Out item decision"
            current = (pu.get(u) or {}).get("department") or (None if state.get(cid) != info[cid]["decision_state"]
                                                              else (bo.get(u) or {}).get("department"))
            row["Now"] = current or "(undecided)"
            if current == d:
                row["Result"] = f"{DONE} — already {'staged' if u in pu else 'decided'} as {d}"
            else:
                row["Result"] = STAGE
        else:
            row["Goes to"] = "UPC override"
            p = pi.get(u)
            if it["department"] == d and not p:
                row["Result"] = f"{DONE} — already {d}"
            elif p and p["department"] == d:
                row["Result"] = f"{DONE} — already staged ({p['staged_by']})"
            elif p and p["staged_by"] != actor:
                row["Result"] = f"{SKIP} — {p['staged_by']} already has a change staged for this item"
            else:
                row["Result"] = STAGE
        item_rows.append(row)

    cols_m = ["Group (old workbook)", "Asked for", "App group", "Now", "Result", "combo_id", "move"]
    cols_g = ["Area", "Group", "Items", "Now", "Will be", "Result", "From (old workbook)", "combo_id", "tier",
              "n_evidence", "source_key", "n_upcs_total"]
    cols_i = ["UPC", "Description", "Area", "Group", "Now", "Will be", "Goes to", "Result", "From (old workbook)", "combo_id"]
    return {"moves": pd.DataFrame(move_rows, columns=cols_m), "groups": pd.DataFrame(group_rows, columns=cols_g),
            "items": pd.DataFrame(item_rows, columns=cols_i)}


def summary(p: dict) -> pd.DataFrame:
    rows = []
    for what, df, area_col in (("Moves", p["moves"], "Now"), ("Group decisions", p["groups"], "Area"),
                               ("Item decisions / UPC overrides", p["items"], "Area")):
        if df.empty:
            continue
        g = df.assign(Outcome=df["Result"].str.split(" — ").str[0]).groupby([area_col, "Outcome"]).size()
        for (area, outcome), n in g.items():
            rows.append({"What": what, "Where in the app": area or "(not found)", "Outcome": outcome, "Count": int(n)})
    return pd.DataFrame(rows, columns=["What", "Where in the app", "Outcome", "Count"])


def report_excel(p: dict, title: str) -> bytes:
    """Everything in one file: a summary, then every line by area."""
    buf = io.BytesIO()
    hide = ["combo_id", "move", "tier", "n_evidence", "source_key", "n_upcs_total"]
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"": [title]}).to_excel(xw, sheet_name="Summary", index=False, header=False)
        summary(p).to_excel(xw, sheet_name="Summary", index=False, startrow=2)
        p["moves"].drop(columns=hide, errors="ignore").to_excel(xw, sheet_name="Moves", index=False)
        p["groups"].drop(columns=hide, errors="ignore").to_excel(xw, sheet_name="Group decisions", index=False)
        items = p["items"].drop(columns=hide, errors="ignore")
        for area in ["Crosswalk", "Unmatched", "Broken Out", "Decided", "Item master"]:
            part = items[items["Area"] == area]
            if not part.empty:
                part.to_excel(xw, sheet_name=f"Items - {area}", index=False)
        for ws in xw.sheets.values():
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, *(len(str(c.value or "")) for c in col[:200])))
    return buf.getvalue()


def apply(engine, p: dict, actor: str, is_admin: bool, file_name: str) -> dict:
    """Does everything marked to be done, the same way the app's own buttons
    do it (each group's step is on the top-bar Undo like any other action).
    A safety snapshot is taken first."""
    import uuid
    click = "oldwb-" + uuid.uuid4().hex[:8]
    snap = dm.take_snapshot(engine, actor, label=f"Before importing {file_name}", kind="manual")
    done = {"snapshot": snap, "moved": 0, "groups": 0, "item_decisions": 0, "suggested": 0, "overrides": 0, "blocked": 0}

    def logged(cid, label, desc, fn):
        before = dm.capture_combo_state(engine, cid)
        fn()
        dm.log_action(engine, actor, cid, label, desc, before, click_id=f"{click}-{cid}")

    for m in p["moves"][p["moves"]["Result"] == MOVE].to_dict("records"):
        cid = int(m["combo_id"])
        snapshot = dm.get_combo_snapshot(engine, cid)
        desc = {"to_broken_out": "Broken Out to UPC-Level", "to_review": "Sent back for review",
                "reopen": "Send Back to Broken Out"}[m["move"]]

        def do(m=m, cid=cid):
            if m["move"] == "to_broken_out":
                dm.break_out_combo(engine, cid, actor, upc_decisions=None)
            elif m["move"] == "reopen":
                dm.reopen_broken_out(engine, cid, actor)
            elif dm.get_combo_snapshot(engine, cid).get("decision_state") in ("broken_out", "decided_broken_out"):
                dm.revert_broken_out_combo(engine, cid, actor)
            else:
                dm.revert_combo(engine, cid, actor)
            dm.clear_pending_for_combo(engine, cid)
        logged(cid, m["App group"], desc, do)
        src, label = m["App group"].split(" — ", 1)
        dm.record_recent_move(engine, cid, src.lower(), label, 0, desc, snapshot, actor)
        done["moved"] += 1

    for g in p["groups"][p["groups"]["Result"] == STAGE].to_dict("records"):
        cid = int(g["combo_id"])
        label = g["Group"].split(" — ", 1)[1]
        logged(cid, g["Group"], f"Suggested {g['Will be']} (from {file_name})", lambda g=g, cid=cid, label=label:
               dm.upsert_combo_suggestion(engine, cid, dm.origin_tier(g["tier"], g["n_evidence"]), g["Will be"],
                                          g["source_key"], label, int(g["n_upcs_total"]), actor, is_admin=is_admin))
        done["groups"] += 1

    items = p["items"][p["items"]["Result"] == STAGE]
    for cid, grp in items[items["Goes to"] == "Broken Out item decision"].groupby("combo_id"):
        cid = int(cid)
        glabel = grp["Group"].iloc[0]
        src, label = glabel.split(" — ", 1)
        changes = {r["UPC"]: {"department": r["Will be"], "combo_id": cid, "label": label,
                              "description": r["Description"], "source_key": src.lower()} for r in grp.to_dict("records")}
        res = {}
        logged(cid, glabel, f"Set {len(changes)} item(s) from {file_name}",
               lambda: res.update(dm.stage_broken_out_decisions(engine, changes, actor, is_admin=is_admin)))
        done["item_decisions"] += sum(1 for r in res.values() if r["status"] == "decided")
        done["suggested"] += sum(1 for r in res.values() if r["status"] == "suggested")

    ov = items[items["Goes to"] == "UPC override"]
    if len(ov):
        with engine.connect() as conn:
            cur = dm._in_chunks(conn, "SELECT upc, description, category, subcategory, brand, pack, size, uom, source_key "
                                      "FROM dbo.items WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(400) '$'))", ov["UPC"].tolist()).set_index("upc")
        changes = {}
        for r in ov.to_dict("records"):
            c = cur.loc[r["UPC"]]
            changes[r["UPC"]] = {"change_type": "edit", "department": r["Will be"], "source_key": c["source_key"],
                                 **{f: (None if pd.isna(c[f]) else c[f]) for f in
                                    ("description", "category", "subcategory", "brand", "pack", "size", "uom")}}
        blocked = dm.save_item_master_pending_bulk(engine, changes, actor)
        done["overrides"] = len(changes) - len(blocked)
        done["blocked"] = len(blocked)
    return done
