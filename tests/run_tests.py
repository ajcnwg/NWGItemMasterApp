"""Runs the whole UI / regression test suite against the app's database.

    python tests/run_tests.py                            # every suite
    python tests/run_tests.py ui_system ui_r2_journey    # just these
    python tests/run_tests.py --list                     # what's there
    python tests/run_tests.py --restore                  # finish a run that was stopped part-way

The suites drive the real app.py (headless, signed in as the demo accounts —
no password is typed) and work from snapshot #0, the Item Master Baseline.
So it's safe on a database people are using:

  1. everything as it is now is saved first: a "Before tests" snapshot, plus
     copies of what a snapshot doesn't hold (the activity log, settings
     requests, app errors, people's unsaved work, and every source's raw
     distributor data and upload log);
  2. each suite runs from #0 (the old-workbook import state is rebuilt from
     the workbook in tests/data/ for the suites that need it);
  3. at the end the "Before tests" snapshot is restored, every snapshot the
     tests made is deleted, and the copies are put back exactly as they were.

If a run is stopped part-way (closed window, crash), nothing new can start
until `--restore` has put everything back. Nobody should work in the app
while it runs (about 1–2 hours for all). Results: tests/logs/summary.txt,
one log per suite in tests/logs/.
"""
import os
import re
import subprocess
import sys
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(TESTS)
sys.path.insert(0, APP)
sys.path.insert(0, TESTS)

SUITES = [
    'ui_smoke',
    'ui_r2_journey',
    'ui_part1',
    'ui_part2b',
    'ui_part3s',
    'ui_part6',
    'ui_part7',
    'ui_url',
    'ui_bo_full',
    'ui_clearall',
    'ui_excel',
    'ui_excel_undo',
    'ui_refresh',
    'ui_limits',
    'ui_undo_conflicts',
    'test_undo_picker',
    'test_moves',
    'ui_r2_settings',
    'ui_settings_requests',
    'ui_r2_items_sources',
    'ui_part4',
    'ui_part5',
    'ui_pending_bulk',
    'ui_dept_upload',
    'ui_rules',
    'test_push',
    'test_newonly',
    'test_reports',
    'ui_workbook_section',
    'ui_workbook_process',
    'ui_r2_import_choices',
    'ui_stale_workbook',
    'test_export_roundtrip',
    'ui_import_review',
    'ui_system',
    'ui_errors',
    'ui_input_checks',
    'ui_upload_reports',
    'ui_notifications',  # personal notifications: who hears what, and where Open goes
]

# What a snapshot doesn't hold, in parent → child order (an upload log row
# before its rejected rows).
KEEP_TABLES = ["activity_log", "dept_settings_requests", "app_errors", "user_workspace", "user_last_seen",
               "raw_items", "source_raw_uploads", "ingestion_log", "ingestion_rejected_rows", "merge_added_items",
               "source_upc_seen", "upload_reports", "upload_report_sources", "upload_report_items", "dup_not_duplicate", "notification_dismissals",
               "user_notifications"]
BAK = "zz_testbak_"
BEFORE_LABEL = "Before tests (restored when they finish)"


def db():
    from itemmaster.db import get_engine
    return get_engine()


def _has(c, t) -> bool:
    from sqlalchemy import text
    return bool(c.execute(text(f"SELECT OBJECT_ID('dbo.{t}')")).scalar())


def leftover_backup(E) -> bool:
    with E.connect() as c:
        return any(_has(c, BAK + t) for t in KEEP_TABLES)


def backup(E) -> None:
    from sqlalchemy import text
    from itemmaster import upload_reports, ingest
    with E.begin() as c:
        ingest.ensure_upc_seen(c)
        upload_reports.ensure_tables(c)  # (so every table to keep exists)
        from itemmaster.dept_mapping import NOTIF_DISMISS_DDL, USER_NOTES_DDL
        c.execute(text(NOTIF_DISMISS_DDL))
        c.execute(text(USER_NOTES_DDL))
        for t in KEEP_TABLES:
            c.execute(text(f"SELECT * INTO dbo.{BAK}{t} FROM dbo.{t}"))


def put_back(E, keep_copies: bool = False) -> None:
    """Everything back from the copies, in one transaction: children emptied
    first, parents refilled first. Does nothing if there are no copies.
    keep_copies: between suites (so each starts from the same data)."""
    from sqlalchemy import text
    with E.begin() as c:
        have = [t for t in KEEP_TABLES if _has(c, BAK + t)]
        for t in reversed(have):
            c.execute(text(f"DELETE FROM dbo.{t}"))
        for t in have:
            cols = c.execute(text("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(:t) AND is_computed = 0"),
                             {"t": f"dbo.{t}"}).scalars().all()
            col_list = ", ".join(f"[{x}]" for x in cols)
            ident = c.execute(text("SELECT COUNT(*) FROM sys.columns WHERE object_id = OBJECT_ID(:t) AND is_identity = 1"),
                              {"t": f"dbo.{t}"}).scalar()
            if ident:
                c.execute(text(f"SET IDENTITY_INSERT dbo.{t} ON"))
            c.execute(text(f"INSERT INTO dbo.{t} ({col_list}) SELECT {col_list} FROM dbo.{BAK}{t}"))
            if ident:
                c.execute(text(f"SET IDENTITY_INSERT dbo.{t} OFF"))
        for t in ([] if keep_copies else have):
            c.execute(text(f"DROP TABLE dbo.{BAK}{t}"))


def finish(E, before_id, keep_ids: set) -> None:
    """Back to how things were before the run."""
    from itemmaster import dept_mapping as dm
    if before_id is not None:
        print(f"Putting everything back as it was (snapshot #{before_id})…", flush=True)
        dm.restore_snapshot(E, before_id, "Tests")
    for sid in dm.list_snapshots(E)["snapshot_id"]:
        if int(sid) not in keep_ids:
            dm.delete_snapshot(E, int(sid))
    put_back(E)
    imp = os.path.join(TESTS, "import_snap.txt")
    if os.path.exists(imp):
        os.remove(imp)
    print("Done — snapshots:", sorted(int(x) for x in dm.list_snapshots(E)["snapshot_id"]), flush=True)


def main(argv) -> None:
    from itemmaster import dept_mapping as dm
    if "--list" in argv:
        print("\n".join(SUITES))
        return
    E = db()
    if "--restore" in argv:
        snaps = dm.list_snapshots(E)
        before = snaps[snaps["label"].fillna("") == BEFORE_LABEL]
        before_id = int(before["snapshot_id"].min()) if not before.empty else None
        keep = {int(s) for s in snaps["snapshot_id"] if before_id is None or int(s) < before_id}
        finish(E, before_id, keep)
        return
    snaps = dm.list_snapshots(E)
    if 0 not in set(snaps["snapshot_id"]):
        sys.exit("Snapshot #0 (the Item Master Baseline) is missing — the tests start from it.")
    if leftover_backup(E) or (snaps["label"].fillna("") == BEFORE_LABEL).any():
        sys.exit("A previous test run didn't finish putting things back. Run `python tests/run_tests.py --restore` first.")
    unknown = [a for a in argv if not a.startswith("-") and a not in SUITES]
    if unknown:
        sys.exit(f"Unknown suite(s): {', '.join(unknown)}. See --list.")
    chosen = [a for a in argv if not a.startswith("-")] or SUITES
    os.makedirs(os.path.join(TESTS, "logs"), exist_ok=True)
    keep_ids = {int(x) for x in snaps["snapshot_id"]}
    backup(E)
    before_id = dm.take_snapshot(E, "Tests", label=BEFORE_LABEL, kind="manual")
    print(f"Saved everything as it is now (snapshot #{before_id} + copies of the raw data and logs).", flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": TESTS + os.pathsep + APP,
           # no suite may delete these while cleaning up after itself
           "TEST_KEEP_SNAPSHOTS": ",".join(str(x) for x in sorted(keep_ids | {before_id}))}
    try:
        r = subprocess.run([sys.executable, os.path.join(TESTS, "build_import_snap.py")], cwd=APP, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        print((r.stdout.strip().splitlines() or [r.stderr[-400:]])[-1], flush=True)
        out = open(os.path.join(TESTS, "logs", "summary.txt"), "w", encoding="utf-8")
        totals = [0, 0]
        # the first suite starts from #0 too, like every one after it (not from
        # whatever happens to be staged right now, e.g. a workbook import)
        dm.restore_snapshot(E, 0, "Tests")
        put_back(E, keep_copies=True)
        for name in chosen:
            t = time.time()
            try:
                r = subprocess.run([sys.executable, os.path.join(TESTS, name + ".py")], cwd=APP, env=env, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=3600)
                log = r.stdout + r.stderr
                died = r.returncode != 0
            except subprocess.TimeoutExpired as e:
                log = (e.stdout or "") + "\nTIMEOUT"
                died = True
            open(os.path.join(TESTS, "logs", f"{name}.txt"), "w", encoding="utf-8").write(log)
            p = len(re.findall(r"^\s*PASS\b", log, re.M))
            f = len(re.findall(r"^\s*FAIL\b", log, re.M))
            # (a suite's deliberate errors log tracebacks too — only a non-zero exit is a crash)
            crash = "  CRASHED" if died else ""
            totals[0] += p
            totals[1] += f
            line = f"{name:24} {time.time() - t:6.0f}s  PASS {p:3}  FAIL {f:3}{crash}"
            print(line, flush=True)
            out.write(line + "\n")
            out.flush()
            dm.restore_snapshot(E, 0, "Tests")
            put_back(E, keep_copies=True)  # (uploads, seen-history, reports: each suite starts from the same data)
        line = f"\nTOTAL  PASS {totals[0]}  FAIL {totals[1]}"
        print(line, flush=True)
        out.write(line + "\n")
        out.close()
    finally:
        finish(E, before_id, keep_ids)


if __name__ == "__main__":
    main(sys.argv[1:])
