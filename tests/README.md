# App tests

End-to-end tests that drive the real `app.py` (headless, as the demo accounts —
no password is ever typed) against the app's database: Crosswalk / Unmatched /
Broken Out / Pending Changes / Decided / Settings, votes between editors, pushes,
top-bar Undo / Redo and every card's Undo…, Items (add / delete / UPC overrides),
Sources, uploads and the monthly refresh, Merge, the department workbook
download / upload, notifications, the Activity report, and the error screens.

## Run them

```
python tests/run_tests.py                       # everything (1–2 hours)
python tests/run_tests.py ui_r2_journey ui_system   # just some
python tests/run_tests.py --list
python tests/run_tests.py --restore             # finish a run that was stopped part-way
```

Results: `tests/logs/summary.txt` (one line per suite), full logs in `tests/logs/`.

## Is it safe to run on the real database?

Yes — but nobody should work in the app while it runs. The runner:

1. saves everything as it is now in a "Before tests" snapshot;
2. runs each suite from snapshot **#0 (Item Master Baseline)** — so #0 must exist;
3. at the end restores the "Before tests" snapshot, deletes every snapshot the
   tests made (numbers carry on from the last real one), and puts the Activity
   log, settings requests, app errors, people's unsaved grid work and every
   source's raw distributor data back exactly as they were.

The old-workbook import suites need the old department workbook at
`tests/data/old department workbook V2.xlsx` (or set `OLD_WORKBOOK` to its path).
`tests/data/` and `tests/output/` are git-ignored — they hold company data.

## Adding a test

Copy one of the `ui_r2_*.py` files: `session("jason")` signs in as a demo
account, `goto(at, "Department Review", "Crosswalk")` opens a tab, widgets are
found by their `key=`, and `check(ok, "what it means")` prints PASS / FAIL.
Always restore `BASE` (#0) in a `finally:`.
