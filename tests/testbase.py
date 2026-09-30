"""Shared test constants: #0 is the Item Master Baseline; the old-workbook
import state is rebuilt by build_import_snap.py into a temporary snapshot."""
import os

SP = os.path.dirname(os.path.abspath(__file__))
BASE = 0
_f = os.path.join(SP, "import_snap.txt")
IMPORT_SNAP = int(open(_f).read().strip()) if os.path.exists(_f) else None
# Snapshots a suite must never delete while cleaning up: #0, the import
# state, and whatever the runner says existed before the run (its own
# "Before tests" snapshot included).
KEEP = tuple(x for x in (BASE, IMPORT_SNAP) if x is not None) + tuple(
    int(x) for x in os.environ.get("TEST_KEEP_SNAPSHOTS", "").split(",") if x.strip())
V2 = os.environ.get("OLD_WORKBOOK") or os.path.join(SP, "data", "old department workbook V2.xlsx")
