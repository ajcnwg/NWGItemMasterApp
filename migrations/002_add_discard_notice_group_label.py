"""
Adds group_label to dbo.change_discard_notices: which combo/Broken Out
group an individual discarded item came from (e.g. "KEHE — HEALTH BODY
CARE / HBC / AROMATHERAPY BODY OILS"), separate from entity_label (the
item's own identity, e.g. "85962900336 — OIL BREATHE EASY").

Without this, a single admin override across a whole Broken Out group
produces one discard notice per UPC with no way to tell which UPCs came
from which combo once several groups' notices are showing at once — the
UI grouped them by who-staged/who-triggered only, mixing unrelated
combos into one flat item list. group_label lets the UI sub-group by
combo within that.
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_engine


def main():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                IF NOT EXISTS (
                    SELECT * FROM sys.columns
                    WHERE object_id = OBJECT_ID('dbo.change_discard_notices') AND name = 'group_label'
                )
                ALTER TABLE dbo.change_discard_notices ADD group_label NVARCHAR(400) NULL;
                """
            )
        )
    print("dbo.change_discard_notices.group_label is ready.")


if __name__ == "__main__":
    main()
