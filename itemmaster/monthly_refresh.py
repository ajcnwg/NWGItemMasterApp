"""
The monthly refresh, usable from the app (Upload & Ingest -> "Monthly
refresh") and from scripts/monthly_refresh.py (for running unattended once
the app is hosted, e.g. on a schedule against an inbox folder).

For each file: work out which source it belongs to from the source's File
Keyword, read and clean it with that source's settings (the same code the
Upload & Ingest tab uses), and sanity-check it against that source's last
upload before replacing anything. Then compute a Merge draft — which runs
every Department Review decision over the new data — and, only if asked,
push it (safety snapshot first, then the engine carries every decision
forward, then this month's snapshot is saved).
"""

import re
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from itemmaster import dept_mapping
from itemmaster.ingest import FileProblem, map_and_clean, read_raw_file, save_raw_upload, stage_source



def _norm(s: str) -> str:
    return " " + re.sub(r"[^A-Z0-9]+", " ", str(s).upper()).strip() + " "


def load_sources(engine) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text("SELECT * FROM dbo.sources ORDER BY priority_rank"), conn)


def match_source(filename: str, sources: pd.DataFrame) -> tuple:
    """(source_key, reason). The source whose File Keyword appears in the
    file name as whole words (so "CS CA" is never mistaken for "CS PNW"). None if none match, or if
    keywords of two different sources both appear (ambiguous)."""
    name = _norm(Path(filename).stem)
    hits = []
    for _, s in sources.iterrows():
        for kw in {s["file_keyword"], s["source_label"], s["source_key"]}:
            if kw and _norm(kw).strip() and _norm(kw) in name:
                hits.append((len(_norm(kw)), s["source_key"], kw))
    if not hits:
        return None, "no source's File Keyword is in the file name"
    hits.sort(reverse=True)
    keys = sorted({h[1] for h in hits})
    if len(keys) > 1:
        return None, f"file name matches more than one source ({', '.join(keys)}) — rename it or upload it on its own"
    return hits[0][1], f"file name contains “{hits[0][2]}”"


def _previous_rows(engine, source_key: str):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT TOP 1 rows_staged FROM dbo.ingestion_log WHERE source_key = :s ORDER BY id DESC"),
            {"s": source_key}).scalar()


def check_file(engine, file, sources: pd.DataFrame, source_key: str = None) -> dict:
    """Reads and cleans one file without saving anything. `file` is a path
    or an uploaded file object. Returns a report with the cleaned data."""
    name = getattr(file, "name", None) or Path(file).name
    rep = {"file": name, "source_key": source_key, "status": "ready", "note": ""}
    if source_key is None:
        source_key, why = match_source(name, sources)
        rep["source_key"], rep["note"] = source_key, why
        if source_key is None:
            rep["status"] = "skipped"
            return rep
    src = sources[sources["source_key"] == source_key].iloc[0].to_dict()
    src = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in src.items()}
    if not src.get("enabled"):
        rep.update(status="skipped", note=f"{source_key} is disabled on the Sources tab")
        return rep
    try:
        if hasattr(file, "seek"):
            file.seek(0)
        raw = read_raw_file(file if hasattr(file, "read") else _PathFile(file), src)
        cleaned, stats, rejected = map_and_clean(raw, src)
    except FileProblem as e:
        rep.update(status="error", note=str(e))
        return rep
    except Exception as e:
        rep.update(status="error", note=f"couldn't read it with {source_key}'s settings: {e}")
        return rep
    prev = _previous_rows(engine, source_key)
    rep.update(rows=stats["rows_staged"], previous_rows=prev, stats=stats,
               _raw=raw, _cleaned=cleaned, _rejected=rejected, _src=src)
    if stats.get("letter_upcs"):
        rep["note"] = ((rep["note"] + "; ") if rep["note"] else "") + (
            f"{stats['letter_upcs']:,} row(s) with letters in the UPC left out (e.g. “{stats['letter_upc_examples'][0]}”)")
    if stats["rows_staged"] == 0:
        rep.update(status="error", note=f"none of its {stats['rows_parsed']:,} rows has a usable UPC in column "
                                        f"“{src.get('upc_column')}” — check it's the right file, and the header row "
                                        "and UPC column on the Sources tab")
        return rep
    # The same rows as the source has now (the same file again, maybe renamed): nothing to do.
    from itemmaster.ingest import current_signature, rows_signature
    if rows_signature(cleaned) == current_signature(engine, source_key):
        rep.update(status="same", note=f"every row matches {source_key}'s current data — nothing new, nothing dropped")
    return rep


class _PathFile:
    """A path that looks like an uploaded file to ingest.read_raw_file."""
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name

    def __fspath__(self):
        return str(self.path)


def ingest_checked(engine, rep: dict, actor: str, report: dict = None) -> None:
    """Saves a file checked by check_file: replaces the source's raw rows,
    logs the upload, and keeps the as-read file for later re-runs. report:
    the upload report the file goes into (shared by every file of one run)."""
    from itemmaster.dept_mapping import activity_via
    with activity_via("Monthly refresh"):
        stage_source(engine, rep["source_key"], rep["_cleaned"], rep["_rejected"], rep["stats"],
                     uploaded_by=actor, original_filename=rep["file"],
                     report=report if report is not None else {"kind": "manual"})
    save_raw_upload(engine, rep["source_key"], rep["_raw"], rep["file"], actor)
    rep["status"] = "ingested"


def compute_draft(engine, actor: str) -> dict | None:
    """The Merge draft for the current data (same as the Merge tab's Compute)."""
    sources = load_sources(engine)
    enabled = sources[sources["enabled"] == True]["source_key"].tolist()  # noqa: E712
    final_df, overrides_applied, deleted = dept_mapping.compute_merge_final_df(engine, enabled)
    if final_df is None:
        return None
    meta = dept_mapping.save_merge_compute(engine, final_df, actor, overrides_applied, deleted)
    return {"item_count": len(final_df), **meta}


def run(engine, files: list, actor: str, push: bool = False, allow_suspicious: bool = False, log=print) -> dict:  # noqa: ARG001 (allow_suspicious: no longer used)
    """The whole refresh. A file that can't be read, doesn't fit its source's
    settings, or shares a source with another file is never ingested; with
    push=True nothing is pushed at all if any file was held back, so a bad
    export can't go live unattended."""
    sources = load_sources(engine)
    reports = []
    for f in files:
        rep = check_file(engine, f, sources)
        log(f"{rep['file']}: {rep['status']} — {rep.get('source_key') or '?'} {rep['note']}".rstrip())
        reports.append(rep)
    # Two usable files for the same source: which one is right can't be
    # guessed (the second would silently replace the first) — hold both back.
    # (a source's current file plus a different one is just as ambiguous)
    usable = [r["source_key"] for r in reports if r["status"] in ("ready", "same")]
    for r in reports:
        if r["status"] in ("ready", "same") and usable.count(r["source_key"]) > 1:
            r.update(status="error", note=f"more than one file for {r['source_key']} — keep only one of them")
            log(f"{r['file']}: held back — {r['note']}")
    held = [r for r in reports if r["status"] == "error"]
    for r in reports:
        if r["status"] == "same":
            log(f"  {r['file']}: same as {r['source_key']}'s current data — left as it is")
    report = {"kind": "auto", "title": "Monthly refresh (script)"}
    for r in reports:
        if r["status"] == "ready":
            ingest_checked(engine, r, actor, report)
            log(f"  ingested {r['rows']:,} rows into {r['source_key']}")
    out = {"files": [{k: v for k, v in r.items() if not k.startswith("_")} for r in reports], "report_id": report.get("id")}
    if not any(r["status"] == "ingested" for r in reports):
        log("Nothing ingested — no Merge draft computed.")
        return out
    log("Computing the Merge draft (runs every Department Review decision over the new data)...")
    out["draft"] = compute_draft(engine, actor)
    d = out["draft"] or {}
    log(f"  draft: {d.get('added_count', 0):,} new item(s) to add; existing items untouched "
        f"({d.get('changed_count', 0):,} differ in the files — ignored; {d.get('removed_count', 0):,} no longer in any file — kept)")
    if push and held:
        log("Not pushing: some files were held back. Review them, then push from the Merge tab.")
    elif push:
        log("Pushing the Merge (safety snapshot, then Department Review carries decisions forward)...")
        out["push"] = dept_mapping.push_merge_compute(engine, actor, is_admin=True,
                                                     on_progress=lambda label, frac: log(f"  {label}"))
        p = out["push"]
        log(f"  pushed {p.get('item_count', 0):,} items; safety snapshot #{p.get('safety_snapshot_id')}, "
            f"monthly snapshot #{p.get('monthly_snapshot_id')}"
            + (f"; engine problem: {p['engine_error']}" if p.get("engine_error") else ""))
    else:
        log("Draft is ready — review and push it on the Merge tab.")
    return out
