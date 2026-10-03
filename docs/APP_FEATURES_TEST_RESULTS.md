# Click-through test results

Tested in the browser on the 8502 test copy, signed in as AJ (admin), on
2026-10-01, against `docs/APP_FEATURES.md`. Editor and viewer views were checked
with the saved tests' preset users (no passwords typed). The database was saved
first and put back afterwards (`python tests/run_tests.py --restore`).

Legend: **Pass** · **Fixed** (found a problem, fixed it, re-checked) · **Note**
(works, but worth improving) · **Not clicked** (with why)

## 3. Item Master

| ID | Result | Notes |
|---|---|---|
| IM-01 | Pass | DAIRY → 10,873 items; Brand list narrows to dairy brands; filter kept in the address. |
| IM-02 | Pass | "cheddar" with DAIRY → 293, not case-sensitive. |
| IM-03 | Pass | Manually-edited filter + "Edited by anyone" picker appear. |
| IM-04 | Pass | "N matching items (T total) — page X of Y". |
| IM-06 | Pass | Cell edit → "1 edited row(s) — not staged yet". |
| IM-07 | Pass | Stage Changes → "Staged 1 changed row(s)". |
| IM-08 | Pass | Discard puts the cell back. |
| SH-04 | Pass | A link with `?tab=Item+Master&im_q=…` opens with that search. |
| IM-05 | Note | (Checked with the editor/viewer preset later.) |
| IM-09 | Pass | Banner + Go to Merge seen earlier this session when a source had changed. |

Found on Item Master: blank cells show the word "None" (also in other tables); an empty result shows a bare "empty" table; no Clear filters button; "1 matching items"; a staged item disappears from the admin grid with no explanation.

## 9. Pending Changes tab

| ID | Result | Notes |
|---|---|---|
| PC-01 | Pass | Search (no match message / match), Leave out all (Push disabled), Include all, Undo on one ("Took back the staged add…"). |
| PC-02 | Fixed | Tick was a ~2 s server run; now a form — tick is instant (no run), Push without tick says "Tick … first". Pushes: 1 edit 1.8 s, 2 adds ~3 s. |

## 5. Add Item

| ID | Result | Notes |
|---|---|---|
| AI-01 | Pass | Required check, "12AB34 isn't a valid UPC", "already in the item master", valid add staged with Department + Brand; failed add keeps the typing. |
| AI-02 | Pass | 2 ready · 3 need fixing with a clear reason each; Show only rows that need fixing; staged 2. Template download not clicked (downloads need your OK) — its file builds correctly. |
| AI-03 | Pass | Lists items added by hand; search narrows it. |
| AI-04 | Note | Removes at once with no confirmation (a stale search picked the wrong item for me) — adding a confirm. |

Found: the "only rows that need fixing" table keeps its old height (empty rows).

## 6. Delete Item

| ID | Result | Notes |
|---|---|---|
| DI-01 | Pass | "Tick at least one item first."; staged delete; staged item drops out of the search. |
| DI-02 | Pass | Earlier today: popup lists whose work; Stage anyway / Cancel. |
| DI-03 | Note | 2 ready, 1 invalid — but "000000000 … UPCs are digits only" is the wrong reason (all zeros); and spreadsheet deletes skip the "someone has work on these" check. |
| DI-04 | Pass | Restore brings the item back with all its fields. The tabs jump back to "Find and delete" afterwards (the Deleted (N) label changed). |
| DI-05 | Not clicked | Needs an item a newer file brought back changed; covered by the saved tests (ui_r2_items_sources). |

## 8. UPC Overrides

| ID | Result | Notes |
|---|---|---|
| UO-01 | Pass | "No changes made."; Department change staged; search clears after. |
| UO-02 | Pass | 1 UPC override + 1 Broken Out item decision + 1 no change, classified right. Status column cuts its text off. |

## 4. Department Review

| ID | Result | Notes |
|---|---|---|
| DR-F01 | Pass | Search, shared by unlocked sections (Unmatched picked up "frozen" instantly). |
| DR-F02 | Pass | Source facet 170 → 32. Chip too narrow to show "CS_CA". |
| DR-F03 | Pass | Sort by Old Department, descending. |
| DR-F04 | Pass | Locked Unmatched kept its own filters. |
| DR-F05 | Pass | Clear filters on a locked section only clears it. |
| DR-F06 | Note | 10 per page → 4 pages; bottom Page control works, but the view stays at the bottom of the new page. |
| DR-F07 | Pass | Items open without a page reload. |
| DR-Q01 | Pass | Approve stages (earlier today). |
| DR-Q02 | Note | Approve all 10 on this page works but took ~16 s. |
| DR-Q03 | Pass | Popup says what happens; Cancel; Break it out 3.9 s. |
| DR-BO01–03 | Pass | Progress line; Work on this group 0.7 s; Done — release it 0.7 s. |
| DR-BO04 | Pass | Force release on Jason's claim. Claim time is shown in UTC without saying so. |
| DR-BO05 | Pass | Fill with suggested, Clear all, pick + Set all shown (each ~0.6 s). |
| DR-BO06 | Pass | Fill 1 blank(s) with auto. |
| DR-BO07 | Pass | Stage all → "Staged 1 item decision(s)"; group leaves Broken Out. |
| DR-BO08 | Pass | Uploaded a group file: 1 differs, 1 UPC not in group ignored; Put them into the grids. (Download not clicked.) |
| DR-BO09 | Pass | Back to Unmatched popup lists the 1 auto decision discarded; ~5 s. |
| DR-BO10 | Note | `?group=` link opens the right page but doesn't scroll to the group (the shortcut does). |
| TB-02 | Pass | Undo of a grid change: popup says what; grid back. |
| TB-03 | Pass | Redo of it. |
| DR-PC03 | Note | Include all / Leave all out work for the 13 regular changes; the import's 23 stay included (by design) — label doesn't say so. |
| DR-PC04 | Pass | "I also agree" → "(agreed by AJ)". |
| DR-PC05 | Pass | Change Department + Update on my own. |
| DR-PC06 | Pass | Suggest BAKERY on Jason's GROCERY → Needs agreement. |
| DR-PC07 | Pass | Save for later moves it; Change vote → "Resolved … → GROCERY". |
| DR-PC08 | Note | Change items grid: as admin, changes to Jason's items went as suggestions to him (matches the card text; admins force with Override). |
| DR-PC09 | Pass | Kristi's suggestion on Jason's items: Accept one, Deny one. |
| DR-PC10 | Pass | Override → DAIRY (locked), Remove override (unlock). Dialog says "override at DAIRY". |
| DR-PC12 | Fixed | Push 129-item group 2.1 s (was 8–20 s); only that group's items' Department changed (46). |
| DR-PC13 | Pass | Recent moves → Undo… → "Everything as it was" brought the group back to Broken Out (5.5 s). Move time in UTC without saying so. |
| DR-PC17 | Pass | Undo the whole import: 22 groups, 59 items, 2 moves taken back; left alone the group I had worked on since. |
| DR-PC18 | Fixed | "Open in Decided" did nothing (button in a section that re-runs on its own) — now opens Decided on the group (2.1 s), and the address follows. Same fix covers the workbook import's "Open in Pending Changes". |
| DR-D01 | Pass | How/by/when/from shown. Dates are UTC days. |
| DR-D02 | Pass | Change Department + Stage. |
| DR-D03 | Note | Mark as reviewed works, but the card still says "by Editor Demo" (not who reviewed it). |
| DR-D04 | Pass | Back to Crosswalk popup explains the decision is removed; Cancel. |
| DR-D05 | Pass | Back to Broken Out popup (fully auto → goes back blank). |
| DR-D06 | Note | Confirm auto-decided items works but shows no message; Stage changes works. |

Found in Department Review: the Approve / Suggest / Update buttons are cut off ("Appr…", "Sugg…", "Upd…"); "Lock filters" wraps; "1 items".
| DR-PC14 | Pass | UPC overrides card → Undo… → Remove them. ("Remove these 1 staged …" wording.) |
| DR-PC15 | Note | Jason's undo request reaches AJ as a notification ("Jason asked you to undo your decision" + Open), and AJ's Undo… takes it back — but AJ's own card doesn't say Jason asked, and the picker calls the request "1 dispute vote(s)". |
| DR-PC16 | Pass | Use this (stages the workbook's option, 7 s); Ask again puts the question back and blocks Push. |
| DR-S01 | Pass | Jason's 2 requests: Approve #1, Deny #2, Undo #1 (back to waiting). Bell showed 2. |
| DR-S02 | Pass | Empty / bad characters refused; "click  test dept" added as CLICK TEST DEPT. Box isn't cleared after adding. |
| DR-S03 | Pass | Removed it. |
| DR-S04 | Note | Save Strict Departments re-runs the engine (35 s) even with no change. |
| DR-S05 | Pass | BAKERY - DRY default → GROCERY; engine re-run 38.6 s; its 5 waiting groups now suggest GROCERY. |
| DR-S06 | Not clicked | Download (needs your OK). |
| DR-S07 | Pass | Re-imported the V2 workbook: plan (81 to stage, 3 moves, 1 skipped), applied with a snapshot first (~49 s); Open in Pending Changes now works (was broken, see DR-PC18). |
| DR-R01–03 | Pass | (Editor preset) Sent a request, withdrew it; settings shown read-only. |
| NT-01 | Pass | Waiting on you / Since your last visit; New badges. |
| NT-02 | Pass | Open → Pending Changes filtered to that group (5.2 s). |
| NT-03 | Pass | ✕ dismissed one at once. |
| NT-04 | Not clicked | Needs 3+ of one kind; covered by the saved test ui_upload_reports §11. |
| NT-05 | Pass | (Nothing new to mark at the time; button only shows when there is.) |
| NT-07 | Pass | Team: Eric, Jason (1 waiting · 3 new), Kristi. |
| NT-08 | Pass | App errors: Mark fixed, Mark all fixed; bell back to normal. |
| SH-01–03 | Pass | Signed in as AJ · admin. Logout not clicked (signing back in needs your password). |
| SH-05 | Pass | Tab switches hide the old tab at once. |
| SH-06 | Pass | A real error showed the friendly card with reference #3 and admin details (see MG-06). |

## 7. Upload Reports

| ID | Result | Notes |
|---|---|---|
| UR-01 | Note | Latest uploads per source; a source with no file name shows "nan · Baseline …". 15 s first load. |
| UR-02 | Pass | Shortcut tested earlier today (Open group → lands on the group). |
| UR-05 | Pass | Keep this one → staged combine (0.7 s); pages (Next → page 2 in 1.3 s, scrolls to top). |
| UR-06 | Pass | Not the same item; Put back on the list. Tab count (186) lags until the next full page run. |
| UR-07 | Pass | After pushing the combine: Undo combine brought the item back. |
| UR-08 | Pass | Placeholder tick + Stage deleting. |
| UR-09 | Pass | 0 items — message shown. |
| UR-10 | Pass | Select a report → view-only detail. |

## 10. Sources

| ID | Result | Notes |
|---|---|---|
| SR-01 | Pass | Edited KEHE's File Keyword → staged; grid shows "None" for blanks. |
| SR-02 | Pass | Sample file → sheets → Analyze → pre-filled form (UPC, Description, Category, Brand found). Remove This Form, Clear All. |
| SR-03 | Pass | Required-field check; staged a new source. |
| PC-03 | Pass | Source Undo; pushed the KEHE change (1.9 s). |

## 11. Upload & Ingest

| ID | Result | Notes |
|---|---|---|
| UP-01/02 | Pass | A file named "october order guide" asks Which source?; picking C&S PNW reads it. |
| UP-03 | Pass | Preview metrics; "Deleted items this file lists · 1". |
| UP-04 | Pass | July files → "same as their source's current data"; Nothing to save. |
| UP-05/06 | Pass | One-step Save tested earlier today (two uploads). |
| UP-07 | Pass | History filtered to KEHE (10 uploads). |
| UP-08 | Pass | Seen earlier today. |

## 12. Merge

| ID | Result | Notes |
|---|---|---|
| MG-01 | Pass | Compute Merge 15.7 s → draft "0 new item(s)". |
| MG-02 | Note | Draft shown; Push is still offered for a draft with nothing to add. |
| MG-03 | Pass | Discard draft. |
| MG-04 | Pass | Approve → "Approved by AJ (1 of 2 needed, admins exempt)". |
| MG-05 | Pass | Push tested earlier today via the upload's one-step Save. |
| MG-06 | Fixed | Apply these 846 change(s) crashed ("not a valid instance of data type float" — a blank Department sent as NaN). Fixed; Apply then works (~2 min for 846, no progress shown) and Undo that fix put all 846 back (1.5 s). The caption says it "stages" fixes — it applies them at once. |
| MG-07 | Pass | Last Merge, Added by past Merges, Source order. |
| MG-08 | Pass | Spot-check: "Only 0 of 3 reference UPCs matched" for a file of deleted/invalid UPCs. |

## 13. Snapshots

| ID | Result | Notes |
|---|---|---|
| SN-01 | Pass | "Snapshot #3 taken." (9.6 s); name box clears. |
| SN-02 | Pass | Automatic filter. |
| SN-03 | Pass | Details → Compare: "Restoring this would: … bring back 4 removed item(s) · change 2 item(s) …". |
| SN-04 | Pass | Restore #3 → safety #4 saved first. |
| SN-05 | Pass | Undo this restore (40 s). |
| SN-06 | Pass | Delete #3 (confirm); Cancel on #4 keeps it. |

## 14. Activity

| ID | Result | Notes |
|---|---|---|
| AC-01 | Pass | People = Jason → 2 changes. |
| AC-02 | Note | Metrics + CSV button (download not clicked). "Groups touched 70" vs "By group: 49 group(s)" don't agree. |
| AC-03 | Pass | By person / By group / By day / Staged now all draw. |

## Roles (preset users, no passwords)

| Check | Result |
|---|---|
| Viewer: only Item Master, read-only, no top bar | Pass |
| Editor: Item Master read-only + the 7 editor tabs, no admin tabs | Pass |
| Editor: Settings shows the request form, not admin settings | Pass |
| Editor: Approve this batch; Push disabled without approvals; no Override; no Undo the whole import | Pass |

## Fixed after the click-through

| What | Fix | Checked |
|---|---|---|
| Department push slow (8–20 s) | Only the pushed groups' items are re-synced (not all 330k); a big push uses one full read instead of many small ones | Browser: 1-group push 2.1 s; 1 item changed, not 800 |
| Upload/merge changed unrelated Departments | A Merge only syncs the items it added | Code + targeted vs full targets match (3,000 items, 0 differences) |
| "I've reviewed…" tick took ~2 s | Tick + Push are a form on all four push screens: the tick runs nothing on the server | Browser: tick instant; Push without tick says so |
| Open in Decided / Open in Pending Changes did nothing | Buttons inside a section now switch the whole page; the address follows | Browser |
| Apply rules fixes crashed on a blank Department | Blank sent as empty, not NaN; and applied in one batch | Browser: 846 changes 8.5 s (was ~2 min), Undo 1.8 s |
| "None" printed in blank cells | Read-only tables show blanks; Item Master grid text cells too | Browser (the Department dropdown column still shows Streamlit's faded "None" for an empty pick) |
| Item Master | Clear filters button; "No items match" message; "1 matching item"; a note when staged items are left out | Browser |
| Remove Manually-Added Item had no confirmation | Pop-up naming the item | Browser |
| Delete Item jumped back to the first tab after a restore | Tab labels no longer change ("Deleted items") | Browser |
| "000000000 — UPCs are digits only" | Says the real reason (all zeros / letters / no digits / blank) | Unit check |
| Spreadsheet deletes skipped the "someone has work on these" warning | They go through the same check as Find and delete | Code |
| Spreadsheet preview kept empty rows / cut-off Status | Sized to its rows; Status column wider | Code |
| Approve / Suggest / Update buttons cut off; Lock filters wrapped | Wider button columns | Browser |
| Paging from the bottom stayed at the bottom | Jumps to the new page's top | Browser |
| `?group=` link didn't scroll to the group | Scrolls to and outlines it | Browser |
| UTC times without saying so | Claim time, undo picker and Decided dates in local time | Code |
| "1 items", "All suggesteds", "override at DAIRY", "Remove these 1…" | Wording fixed | Browser / code |
| "Leave all out" left the import's changes in | Labels say "Include / Leave these N" (import has its own ticks) | Code |
| Mark as reviewed kept the old "by" | Records who reviewed it and when | Code |
| Confirm auto-decided items showed no message | Message added | Code |
| Add Department box kept the text | Clears after adding | Code |
| Save Strict Departments / Save exceptions re-ran the engine with nothing changed | Enabled only after an edit | Code |
| Stager didn't see "X asked you to undo this" on the card | Shown on their card | Code |
| Undo picker called votes "dispute votes" | "vote(s) on it" | Code |
| "nan · Baseline" in Upload Reports | Missing file name shows "(no file name)" | Code |
| Merge offered Push for an empty draft | Push disabled; "Nothing new to add" | Code |
| Rules caption said fixes are "staged" | Says they go live at once, with an Undo | Code |

## Decided 2026-10-02 (the open questions)

| Question | Your answer | What changed | Checked |
|---|---|---|---|
| Approve all N on this page was ~1.5 s per group | Make it faster; each card's Undo… takes back one group, the top-bar Undo takes back all of them | One transaction for the whole page (25 groups: ~37 s -> ~1.3 s). The top-bar Undo / Redo now takes back everything one click did (25 groups: ~1 s). A group already taken back by its own Undo… is skipped quietly | Browser: approve 25, card Undo… on one, top-bar Undo (put that one back), top-bar Undo took all 25 back; disputes / agreements match one-by-one |
| Admin changing someone else's Broken Out item on Pending Changes | Admin replaces it | An admin's pick replaces the owner's staged one; the owner gets a "changed by" notice; the top-bar Undo puts theirs back. Editors still send suggestions | Browser: Kristi's DELI -> AJ's DAIRY, notice recorded, Undo restored Kristi's |
| Activity "Groups touched" (70) vs By group (49) | Match the list | "Groups touched" (and the per-person Groups column) count exactly the By group rows | Browser: 77 = 77 |
| Upload Reports tab counts lagged | Count inside the tab | Plain tab names; each tab's first line is a live count (pairs to review; made-up UPCs and how many are staged; items no file has) | Browser: Not a duplicate -> 186 to 185 at once, same tab |
| Item Master Department showed a faded "None" | Items should always have one; an option is fine | Blank departments show "(no department)"; picking it on an item that has one is refused with a message | Browser: label shown; FROZEN kept with the message; picking FROZEN on a blank item stages normally |

Also fixed while checking: the grid's hover toolbar covered Item Master's Discard button (more room under the bar), and the small ‹ › page buttons on Possible duplicate UPCs showed empty (no room for the arrow).

Still open:
- **Push stays blocked while the import has an open "Needs your choice"** — your rule from 2026-09-29.
- 26,231 items have no department today (mostly items in groups still waiting for a decision) — they show "(no department)" in the grid.
- The Broken Out "Change items" grid's **New Department** column shows Streamlit's faded "None" until you pick one (means "nothing picked").

## Mass-change audit 2026-10-02 (notifications at scale)

Setup (all four reviewer accounts, the app's own functions and Undo records): Approve all on 320 groups (Jason / Kristi / Eric),
~110 disputes, 66 undo requests, 45 admin overrides of others' decisions, 60 Break Outs with ~485 item decisions and 126 item
suggestions on others' items, 15 of Jason's groups moved by AJ, 9 settings requests; then **1,500 group changes by Jason, ~3,000
item decisions by Kristi**, and one push by AJ of 1,527 group decisions + the item decisions (77,621 items). Eric was the third
account, only watching.

| Found | Fixed | Checked |
|---|---|---|
| A refresh, a reconnect or a second tab counted as a new visit — "Since your last visit" emptied before it was read | "New" now lasts until **Mark all as read**; updates older than two weeks drop off | Two sessions both show 1,560 new; mark read clears it for good |
| Every reviewer got every dispute in the app (130 per person) | Disputes you're in stay yours; everyone else's are one Team activity line | Eric: 3 lines, not 130 cards |
| No sign of other people's big activity | **Team activity**: one compact card, a line per person and area, Open Activity / Needs agreement / Dismiss all | "Jason · 1,500 change(s) in Department Review" |
| Folds were per kind only; a folded list drew every row (484 sidebar buttons) | Fold by kind with who did it ("from Eric 3, Kristi 3, Jason 3"); opened fold shows newest 15 + "…and N more" + Open <tab> | Jason's 1,560: 2 cards, 70 buttons, 0.5 s per click |
| Overridden group decisions said "1 of your staged item(s) replaced…" | "AJ (admin) overrode your GROCERY decision with DAIRY"; item overrides fold to "overrode N of your item decision(s)" | Notice text |
| Undo requests stayed on a group after an admin override, landing on the admin | An override clears them | — |
| Dismiss many was one round trip each | One statement | 1,460 dismissed in 0.8 s |
| Row Open buttons squeezed to an empty box; stale Mine cards under Team while loading | Buttons on their own line | Screenshots |
| **Pending Changes drew every staged group** (1,867 buttons, 29k page elements): 15 s to open, 4.4 s a click, and the browser stopped sending clicks (Push / Use this did nothing) | 25 groups a page in every list; ticks on other pages kept | 1,810 staged: 564 buttons, 2.5 s a click; untick on page 1 survives page 2 and back |
| Push: one transaction per group | One transaction for all groups + batched activity lines, progress bar | 77,621 items pushed in ~20 s |
| Team view lagged a minute after a push | Push clears the notification cache | — |

Still to look at: opening Pending Changes the first time with thousands staged is ~10 s; a single vote on a card is ~1.2 s.

## Personal notifications (2026-10-02, after the mass audit)

Your call: nobody hears about work they never touched; admins hear about pushes (plus settings requests, app errors and a
"waiting on a push" reminder); every note opens exactly where it's about. Built as notes written for the people involved at the
moment each thing happens (dbo.user_notifications), plus the live "waiting on you" list. Click-through with the mass data:
AJ's list = 9 settings requests + 1 reminder (+ one note per editor push); Jason's 1,500 pushed groups = one "Went live" card with a
department breakdown; Eric got nothing about work he never touched; Open landed on the right card in Pending Changes (right page) and
Decided, and on the push's report. Pending Changes keeps its cards: 25 a page per section, a one-line summary bar, a staged-by
filter only on long multi-person lists, push summaries. New suite `ui_notifications` (20 checks). Full run: **38 suites,
835 pass / 0 fail**; database back to #0 + the staged import.
