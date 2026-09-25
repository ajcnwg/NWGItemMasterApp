"""
Monthly refresh from an inbox folder — for running unattended once the app
is hosted (Windows Task Scheduler, cron, an Azure job…).

Drop each distributor's new file into the inbox. Every file is matched to
its source by the source's File Keyword (Sources tab), read and cleaned
with that source's settings, sanity-checked against its last upload, and
ingested. A Merge draft is then computed, which runs every Department
Review decision over the new data. Processed files move to
inbox/processed/<date>/, held-back ones to inbox/held/<date>/.

    python scripts/monthly_refresh.py --inbox "D:/ItemMaster/inbox"
    python scripts/monthly_refresh.py --inbox "D:/ItemMaster/inbox" --push

Without --push the draft waits on the Merge tab for an admin to review and
push (recommended). With --push it goes live straight away (safety
snapshot first, then this month's snapshot) — but never if a file was held
back as unreadable or suspiciously small.
"""

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from itemmaster import monthly_refresh  # noqa: E402
from itemmaster.db import get_engine  # noqa: E402

EXTS = {".xlsx", ".xls", ".xlsb", ".csv"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inbox", required=True, help="folder holding this month's distributor files")
    ap.add_argument("--push", action="store_true", help="push the Merge live after computing it")
    ap.add_argument("--allow-suspicious", action="store_true",
                    help="also ingest files much smaller than the source's last upload")
    ap.add_argument("--actor", default="Monthly refresh script", help="name recorded as who did it")
    ap.add_argument("--keep-files", action="store_true", help="leave files in the inbox afterwards")
    ap.add_argument("--check-only", action="store_true",
                    help="just match and read every file and report — change nothing")
    args = ap.parse_args()

    inbox = Path(args.inbox)
    files = sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith("~$"))
    if not files:
        print(f"No distributor files in {inbox}.")
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    log_path = inbox / f"refresh_{stamp}.log"
    lines = []

    def log(msg):
        print(msg)
        lines.append(msg)

    log(f"Monthly refresh {stamp} — {len(files)} file(s) from {inbox}")
    if args.check_only:
        engine = get_engine()
        sources = monthly_refresh.load_sources(engine)
        for f in files:
            r = monthly_refresh.check_file(engine, f, sources)
            log(f"{r['file']}: {r['status']} — {r.get('source_key') or '?'} {r['note']}"
                + (f" · {r['rows']:,} rows (last time {r['previous_rows'] or 0:,})" if r.get("rows") is not None else ""))
        return 0
    out = monthly_refresh.run(get_engine(), files, args.actor, push=args.push,
                              allow_suspicious=args.allow_suspicious, log=log)
    if not args.keep_files:
        for f in out["files"]:
            dest = inbox / ("processed" if f["status"] == "ingested" else "held") / stamp
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(inbox / f["file"]), dest / f["file"])
    log_path.write_text("\n".join(lines), encoding="utf-8")
    held = [f for f in out["files"] if f["status"] != "ingested"]
    return 1 if held else 0


if __name__ == "__main__":
    sys.exit(main())
