# NWG Item Master App — everything a user can do

The full list of what the app lets each kind of user do, tab by tab. Each line
has an ID so it can be tested and ticked off (results: `docs/APP_FEATURES_TEST_RESULTS.md`).

**Roles** (set per user in `config.yaml`)

| Role | Tabs |
|---|---|
| Viewer | Item Master only, read-only |
| Editor | Item Master (read-only grid), Department Review, Add Item, Delete Item, Upload Reports, UPC Overrides, Pending Changes, Activity |
| Admin | Everything an editor has, plus Sources, Upload & Ingest, Merge, Snapshots — and editing in the Item Master grid, Department Review Settings, overrides, force-release, pushing alone |

**How changes work.** Almost every change is *staged* first (it waits on a Pending
Changes list) and only goes live when someone *pushes* it. A few things act straight
away; those are marked **(live at once)**. Department Review pushes by editors need
2 approvals; admins can push alone. A safety snapshot is taken before big changes.

---

## 1. Signing in and the page itself (SH)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| SH-01 | Anyone | Sign in with username and password | The app opens on Item Master (or the tab in the link). Wrong password: "Username or password is incorrect." |
| SH-02 | Anyone | Log out (sidebar, under your name) | Back to the sign-in screen; your Undo/Redo history and unsaved grid drafts are cleared. |
| SH-03 | Anyone | See your name and role at the top of the sidebar | Shows "name · role". |
| SH-04 | Anyone | Open a shared link / reload | The same tab, section, filters, page and open Broken Out group come back (`?tab=`, `?sub=`, `?q=`, `?page=`, `?group=`, `?im_…`). |
| SH-05 | Anyone | Switch tabs | Old tab's content disappears at once, placeholder cards show, the new tab draws. Only the tabs your role allows are listed. |
| SH-06 | Anyone | Hit an unexpected error | A friendly card ("Something went wrong", reference #) with **Try again**; admins also see **Details (admin)**. Database asleep: "Still connecting…" and it retries by itself. Firewall: "Can't reach the database from this network". |
| SH-07 | Editor+ | Leave and come back | Unsaved grid drafts, Undo/Redo steps, uploaded group Excel files and "Saved for later" ticks are remembered. |

## 2. Top bar, notifications, app errors (TB / NT)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| TB-01 | Editor+ | Click the bell (shows a count when there's something new) | Opens the sidebar with the notifications. Never hides or clears them. |
| TB-02 | Editor+ | **Undo** (top bar) | Popup: undoes your last grid change (not staged yet) or your last Department Review action (vote, staged decision, override, Break Out / Send Back). Says what it will undo; **Confirm undo** / **Cancel**. One click that changed many groups (e.g. Approve all) is one step: "N groups, all from one click". Refuses (and says why) if someone changed that group since — for a many-group step, only those groups are left as they are. Nothing to undo: says so. |
| TB-03 | Editor+ | **Redo** (top bar) | Same, the other way. |
| NT-01 | Editor+ | Read **Waiting on you** | Only your own: suggestions on items you staged, undo requests only you can act on, disputes you voted in. Stays until handled. 3+ of one kind fold into one card ("49 × Asked to undo · Kristi 40, Eric 9"); an opened card shows the newest 15. |
| NT-01b | Editor+ | Read **What happened to your work** | Each written for you when it happened, only about work your account touched — never your own actions, never others' work you had no part in: it went live in someone else's push ("1,461 of your group decision(s): MEAT 103 · FROZEN 86 …"), an admin overrode or replaced it, a Merge discarded it, it was moved or undone by someone else, a new vote / agreement on a group you voted on, your claim lapsed or an admin released it, your suggestion was accepted or declined, your settings request was decided, your staged item edits were pushed. "While you were away" line when 10+ are new. |
| NT-01c | Admin | Read **Waiting on you** + **Pushes** | Settings requests to decide, one "Staged, waiting on a push" reminder (how much, how old), app errors, and one note per push someone else made (opens its report). Your own pushes: no note (they're in the report). |
| NT-02 | Editor+ | **Open** on a notification | Straight to it: the group's card wherever it is now (Crosswalk / Unmatched / Broken Out / the right page of Pending Changes / Decided), scrolled to and outlined; a push's report (Upload Reports → Pushes); Settings. The old tab clears at once ("Opening…"). |
| NT-03 | Editor+ | **Dismiss** on one notification | Gone for you, for good. |
| NT-04 | Editor+ | **Dismiss these N** on a folded card | All of them at once. |
| NT-05 | Editor+ | **Mark all as read** | New marks go; a refresh or second tab never clears them on its own. |
| NT-06 | Editor+ | Search (shown with 8+ notifications) | By group, person or what happened. |
| NT-07 | Admin | **Mine / Team** switch | Team: each editor's notifications (Open works; no Dismiss) — to see what's waiting on them. |
| UR-11 | Editor+ | Upload Reports → **Pushes** | Every recent push (Department Review and Item Master): who, when, groups / items, by department, by who staged them, the groups (pick one → Open it). |
| NT-08 | Admin | App errors (top of sidebar): **Full details**, **Mark fixed**, **Mark all N fixed** | Shows errors people hit (who, where, details); marking fixed removes them. |

## 3. Item Master tab (IM)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| IM-01 | Anyone | Filter by Department, Brand, Source | Each filter only offers values that exist under the others; "(no Department)" option when some are blank. |
| IM-02 | Anyone | Search description, brand or UPC | Narrows the grid (plain text, not case-sensitive). |
| IM-03 | Anyone | **Only manually-edited items** (+ **Edited by**) | Shows only items someone edited by hand, optionally by one person. |
| IM-04 | Anyone | Rows per page, Page | "N matching items (T total) — page X of Y". |
| IM-05 | Viewer/Editor | Look at the grid | Read-only ("Read only for your role."). |
| IM-06 | Admin | Edit cells (Description, Department, Category, Subcategory, Brand, Pack, Size, UOM, Source) | "N edited row(s) — not staged yet"; edits survive filter/page changes. An item with no department shows "(no department)"; picking it on an item that has one is refused ("Every item needs a department"). |
| IM-07 | Admin | **Stage Changes** | Staged as item edits on Pending Changes. An item already corrected by hand asks first (**Stage this over the existing correction** / **Discard this edit, keep the correction**). An item with someone's change already waiting is refused with who/what. |
| IM-08 | Admin | **Discard** | Drops the unstaged edits. |
| IM-09 | Anyone | "Raw data has changed … since the last Merge" banner (+ **Go to Merge** for admins) | Shown when a source's file changed and hasn't been merged. |

## 4. Department Review (DR)

Sections are tabs (switch instantly, in the browser): **Crosswalk, Unmatched, Broken
Out, Pending Changes, Decided, Settings**. Every section has the same filter bar.

### 4.1 Filter bar and paging (every section)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-F01 | Editor+ | Search (source, department, category, subcategory) | Narrows the list; shared by every unlocked section. |
| DR-F02 | Editor+ | Facets (Source, Old Department, Suggested / Being worked on by / Decided as / Status / Decided by) | Multi-pick filters, per section. |
| DR-F03 | Editor+ | Sort by + Descending | Sorts the whole list before paging. |
| DR-F04 | Editor+ | **Lock filters** | This section keeps its own filters; others don't change it. |
| DR-F05 | Editor+ | **Clear filters** | Back to no search/facets, default sort, 25 per page. |
| DR-F06 | Editor+ | Groups per page (10/25/50/100), Page — top and bottom | "N groups · M items · page X of Y". |
| DR-F07 | Editor+ | **Show affected items (N)** on any card | Opens the group's items (UPC, Description, Brand, Pack, Size, UOM) without reloading the page. |

### 4.2 Crosswalk and Unmatched (DR-Q)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-Q01 | Editor+ | Pick a Department and **Approve** | Card leaves the list; "Staged: … → DEPT (N item(s))". Goes to Pending Changes. Someone else suggested a different one: goes to Needs agreement. No Department picked: "Pick a Department first." |
| DR-Q02 | Editor+ | **Approve all N on this page as suggested…** | Popover with counts; **Approve these N** stages each with its suggestion, all at once (a second or two). The top-bar Undo takes all of them back; each card's **↩ Undo…** takes back one. |
| DR-Q03 | Editor+ | **Break Out** | Popup: how many items can be auto-matched now; **Apply auto-decisions** / **Start blank** / **Cancel**. Moves the group to Broken Out at once (undoable). |
| DR-Q04 | Editor+ | **↩ Undo…** on a card (when it has something to undo) | Undo picker (see 4.8). |
| DR-Q05 | Editor+ | Read the evidence line | e.g. "76% of 17 matched item(s) say GROCERY", "No evidence — pick a Department", "saved default: X". |

### 4.3 Broken Out (DR-BO)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-BO01 | Editor+ | Read a group's progress | Bar and counts: done · staged · to decide · auto · items; who's working on it. |
| DR-BO02 | Editor+ | **Work on this group** | Claims it (others see "Being worked on"); opens its items. Someone just beat you: says so. |
| DR-BO03 | Editor+ | **Done — release it** | Releases the claim. |
| DR-BO04 | Admin | **Force release** on someone else's group | Releases their claim. |
| DR-BO05 | Editor+ | Items to decide grid: filter rows, Show (All / Still blank / Filled in), tick rows, pick a Department + **Set rows** / **Set all N shown**, **Clear rows**, **Clear all**, **Fill N blank(s) with suggested**, type/paste/drag in the Department column | Fills the grid; nothing saved until staged; each step can be undone with the top-bar Undo. Not a real Department: shown in red, not staged. |
| DR-BO06 | Editor+ | Auto-matched items to confirm: **Fill N blank(s) with auto** or pick others | Only rows filled in are staged. |
| DR-BO07 | Editor+ | **Stage all N decision(s) in this group** | Staged to Pending Changes; items someone else already decided go as suggestions. |
| DR-BO08 | Editor+ | **Prefer Excel?** → **Download as Excel**, fill it in, **Upload it back**, then **Stage these N decision(s)** or **Put them into the grids**; **Remove file** | Excel has a Department dropdown; bad values reported; UPCs not in the group ignored. |
| DR-BO09 | Editor+ | **Back to Crosswalk / Unmatched** | Popup lists what's discarded; **Send it back** / **Cancel**. Undoable. |
| DR-BO10 | Editor+ | Open a group from a link or shortcut | Lands on the page the group is on, scrolled to it and outlined. |

### 4.4 Pending Changes (Department Review) (DR-PC)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-PC01 | Editor+ | Search pending changes | Narrows every list below. |
| DR-PC02 | Editor+ | **Include** tick on a group / Broken Out group | Unticked: moves to "Saved for later" (kept, not pushed). Every list on Pending Changes shows 25 groups a page (‹ Previous · "26–50 of 1,810" · Next ›); ticks on other pages are kept. |
| DR-PC03 | Editor+ | **Include all N** / **Leave all out** | Ticks/unticks every regular (non-import) change. |
| DR-PC04 | Editor+ | **I also agree — DEPT** | Records your agreement (counts toward approvals). |
| DR-PC05 | Editor+ (who staged it) | **Change Department** + **Update** | Restages with the new Department; approvals reset. |
| DR-PC06 | Editor+ (someone else's) | **Suggest Department** + **Suggest** | Your vote; sends it to Needs agreement. Max 5 different suggestions. |
| DR-PC07 | Editor+ | Needs agreement: **I also think this** / **Change vote** / **Withdraw my vote** / **Add suggestion** / **Save for later** | Settled when everyone agrees (or an admin overrides). |
| DR-PC08 | Editor+ | Broken Out group: **Change items (N)** grid + **Apply changes** | Your own items change; others' become suggestions to their owner. An admin's change replaces the owner's (owner gets a notice; top-bar Undo puts theirs back). |
| DR-PC09 | Owner / admin | Item suggestions: **Accept / Deny** (all, per item, or picked); suggester: **Withdraw** | Applies or drops the suggested Department. |
| DR-PC10 | Admin | **🛡️ Override** on a group | Popup: force any Department and lock it (**Override**, **Update override**, **Remove override (unlock)**). For a Broken Out group: per-item **Override to** / **Unlock**, or whole group at once. |
| DR-PC11 | Editor | **Approve this batch** | Adds your approval ("Approved by … (k of 2 needed)"). |
| DR-PC12 | Editor+ (2 approvals) / Admin | Tick **I've reviewed these changes…** then **Push N Included Item(s)** | Tick is instant. Push (with a progress bar; ~20 s for 1,500 groups + 3,000 items) makes the included decisions live and updates those items' Department in the item master (only those items). "Pushed N item(s). Item Master updated — M item department(s) changed." Nothing included: says so. Push blocked while a "Needs your choice" question is open. Push without the tick: "Tick … first". |
| DR-PC13 | Editor+ | Recent moves: **↩ Undo…** | Undo a Break Out / Send Back, back to any earlier step. |
| DR-PC14 | Stager / admin | UPC overrides staged for items in these groups: **↩ Undo…** → **Remove them** | Takes back those staged overrides. |
| DR-PC15 | Stager / admin | Undo requested: **↩ Undo…** | Undo something others asked to be undone. |
| DR-PC16 | Editor+ | Old-workbook import → **Needs your choice**: **Use this** on an option; **Choices already made** → **Ask again** | Picking stages that option (App option: nothing); Ask again takes it back and reopens the question. |
| DR-PC17 | Admin | **Undo the whole import…** → **Undo the import** | Takes back everything the import still has (not pushed, not changed by others). |
| DR-PC18 | Editor+ | **Recently pushed** → **Open in Decided** | Jumps to that group in Decided. |
| DR-PC19 | Editor+ | "N staged change(s) didn't survive" → **Got it — dismiss** | Explains changes discarded by an override or a Merge. |

### 4.5 Decided (DR-D)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-D01 | Editor+ | Read how a group was decided | Decided as X / item by item, how, by whom, when, pushed by, from which queue. |
| DR-D02 | Editor+ | **Change Department** + **Stage** (whole group) | Staged on Pending Changes; current one stays until pushed. Locked by an override: says so. |
| DR-D03 | Editor+ | **Mark as reviewed** (auto-decided group) | Recorded as reviewed (undoable). |
| DR-D04 | Editor+ | **Back to Crosswalk/Unmatched** | Popup (what's lost); sends it back for a fresh decision (undoable). |
| DR-D05 | Editor+ | Item-by-item group: **Back to Broken Out**, **Back to Crosswalk/Unmatched** | Reopen popups (see 4.8). |
| DR-D06 | Editor+ | **Show item decisions** → **Confirm all N auto-decided item(s) as reviewed**; New Department grid + **Stage changes** | Confirms autos; stages changed items. Same as current: "nothing to stage". |

### 4.6 Settings — admin (DR-S)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-S01 | Admin | Requests from editors: note + **Approve** / **Deny**; Decided requests → **Undo** | Approving makes the change (Strict / Unmatched Default re-runs the engine). Undo puts it back to waiting. |
| DR-S02 | Admin | Departments: type a name + **Add** | Added to every Department list. Checks: empty, already there, >60 chars, bad characters. |
| DR-S03 | Admin | Remove a Department you added + **Remove** | Only when it's not in use (says where it's used). |
| DR-S04 | Admin | Strict Departments grid (add/remove rows, Trust direct evidence) + **Save Strict Departments** | Saved; Department engine re-runs ("N group(s) changed…"). |
| DR-S05 | Admin | Unmatched Defaults: Find, Show (All/Waiting/No default), edit Default + **Save N change(s)**; Per-source exceptions grid + **Save exceptions** | Saved; engine re-runs; groups waiting get the default as their suggestion. |
| DR-S06 | Admin | **Download the app as a workbook** | Excel of every group and decision (~15 s). |
| DR-S07 | Admin | Upload a department workbook → review the plan (tabs, Show filter, **Download this report**) → **Stage N change(s), make N move(s), take back N** | Snapshot first; stages decisions, makes moves; conflicts skipped and listed. After: **Download the full report**, **Import another workbook**, Group + **Open in Pending Changes**. |

### 4.7 Settings — editor requests (DR-R)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-R01 | Editor | Request: Add a Department / Add a Strict Department / Set an Unmatched Default, with a reason → **Send request** | "Sent request #N to the admins". Admins see it; you're notified when decided. |
| DR-R02 | Editor | Your requests → **Withdraw** (waiting ones) | Withdrawn. |
| DR-R03 | Editor | See current Departments, Strict Departments, Unmatched Defaults | Read-only. |

### 4.8 Department Review popups (DR-DLG)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DR-DLG01 | Editor+ | Undo picker: choose a point to go back to (**Confirm undo**); for Broken Out: keep the rest / only auto-matched / re-run auto-matching / all blank | Goes back; refuses if the group changed meanwhile. Not your staged work: **Request undo** (asks its owner). |
| DR-DLG02 | Editor+ | Send back popup: **Send it back** / **Cancel** | Shows Now → After and what's discarded. |
| DR-DLG03 | Editor+ | Break out / reopen popup: **Apply auto-decisions** / **Start blank** / **Break it out** / **Send it back** / **Cancel** | Shows how many items can be auto-matched. |
| DR-DLG04 | Admin | Override popup (see DR-PC10) | Forces and locks. |

## 5. Add Item (AI)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| AI-01 | Editor+ | Fill the form (UPC*, Description*, Department, Category, Subcategory, Brand, Pack, Size, UOM) + **Add Item** | Staged add on Pending Changes. Errors: required fields; "isn't a valid UPC — UPCs are digits only"; "already in the item master"; someone's change already waiting. A failed add keeps what you typed. |
| AI-02 | Editor+ | **Adding many items — upload a spreadsheet**: **Download the blank template**, upload it, see the check (Ready / No change / needs fixing, **Show only rows that need fixing**), **Stage adding N item(s)** | Ready rows staged; bad rows listed and skipped. |
| AI-03 | Editor+ | Manually added items: search, rows per page, page | Lists items added by hand. |
| AI-04 | Editor+ | Select one + **Remove Manually-Added Item** **(live at once)** | Removed from the item master (kept under Deleted, restorable). |

## 6. Delete Item (DI)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| DI-01 | Editor+ | Find and delete: search, tick rows, **Stage deleting** | Staged deletes on Pending Changes. Nothing ticked: "Tick at least one item first." Staged items drop out of the search. |
| DI-02 | Editor+ | Delete an item someone else has work on | Popup "Someone has work on these items" (UPC / What / Whose): **Stage deleting anyway** / **Cancel**; items with someone's change waiting can't be staged (**Open Pending Changes**). |
| DI-03 | Editor+ | **Deleting many items — upload a spreadsheet** | As AI-02, for deletes. |
| DI-04 | Editor+ | Deleted (N): search, tick, **Restore** **(live at once)** | Back exactly as it was, into its Department Review group. |
| DI-05 | Editor+ | Deleted items a newer file brought back different: tick **Put back** → **Use the deleted info for N item(s)**, or **Keep them all as they came back** | Stages the old info (overrides / Broken Out decisions), or keeps the file's version. |

## 7. Upload Reports (UR)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| UR-01 | Editor+ | Latest uploads: summary table; a tab per source; **New items (N)** / **Not in the file (N)** | What each source's latest upload added and dropped (baseline for sources not uploaded since). |
| UR-02 | Editor+ | New items: **Open group →** shortcuts (not on the baseline) | Lists new items still needing a Department, by group; jumps to that exact group (outlined). Staged ones say "staged on Pending Changes". Items that joined a decided group or were auto-matched aren't listed. |
| UR-03 | Editor+ | New items: filter, tick, **Stage deleting** | Staged deletes (only items already in the item master). |
| UR-04 | Editor+ | Not in the file: tick, **Stage removing** | Staged removals; "Still in another source's file" listed separately, can't be removed. |
| UR-05 | Editor+ | Possible duplicate UPCs: filter, pages (‹ › / Previous / Next), open a pair, **What the files say**, **Keep this one** | Staged combine (the other is removed and stays out). Each Upload Reports tab starts with a live count ("N possible duplicate pairs to review", "N items with a made-up UPC · M staged to delete", "N items no current file has") — tab names stay plain so the open tab never resets. |
| UR-06 | Editor+ | **Not the same item** **(live at once)** | Pair taken off the list for good; listed under "Marked as two different items" → **Put back on the list**. |
| UR-07 | Editor+ | **Combined · N** → tick + **Undo combine** **(live at once)** | The removed item comes back as it was. |
| UR-08 | Editor+ | Placeholder UPCs: tick + **Stage deleting** | Made-up codes (99999…), staged deletes. |
| UR-09 | Editor+ | No file has these: tick + **Stage removing** | Items no current file lists. |
| UR-10 | Editor+ | History: select a report | Read-only view of what it added/dropped (kept one year). |

## 8. UPC Overrides (UO)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| UO-01 | Editor+ | Search, pick an item, change fields (Description, Department, Category, Subcategory, Brand, Source Key, Pack, Size, UOM) + **Stage Change** | Staged edit on Pending Changes. No change: "No changes made." Odd current Department: warned. |
| UO-02 | Editor+ | **Changing many items — upload a spreadsheet** | As AI-02; Departments for Broken Out items go as Broken Out decisions. |

## 9. Pending Changes tab (PC)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| PC-01 | Editor+ | Pending Item Master Changes: search, sort, Include ticks, **Include all / Leave out all**, page, **Full details**, **Undo** on one | Lists staged adds/deletes/edits/combines; Undo takes one back. |
| PC-02 | Editor+ | Tick **I've reviewed…** + **Push N Included Change(s)** | Tick instant; included changes go live. Delete: item leaves its group; anyone's staged Department on it is dropped (they're told). |
| PC-03 | Admin | Pending Source Changes: **Apply immediately** tick, **Full details**, **Undo**, tick + **Push N Source Change(s)** | Source settings go live; with Apply immediately the source's last file is re-read with the new settings (held back if columns are missing). |

## 10. Sources (SR) — admin

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| SR-01 | Admin | Edit the sources grid (label, columns, cleaning rules, priority, enabled, Size Format, Apply Now) + **Stage Source Changes** | Staged on Pending Changes; warned if the last file lacks a named column. |
| SR-02 | Admin | Add a source: upload sample file(s), pick sheets, **Analyze Selected Sheets**, or **+ Add a blank source manually** | A pre-filled form per sheet. |
| SR-03 | Admin | Source form + **Stage New Source** / **Remove This Form**; **Clear All** | Staged add. Checks: required fields, key already exists / pending. |

## 11. Upload & Ingest (UP) — admin

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| UP-01 | Admin | Drop one file or several (up to one per source) | Each is matched to its source by name and checked: Ready / Rejected / Which source? / Same as current / Two files for this source. |
| UP-02 | Admin | **Which source?** picker | For a file whose name doesn't say, or that couldn't be read as the guessed source. |
| UP-03 | Admin | Read the preview per file | New items, Rows read, Will be kept (vs last upload), Invalid UPC, Duplicate UPC; the new items list (+ CSV); "not in this file"; deleted items it lists; letter-UPC warning; name-looks-like-another-source warning. |
| UP-04 | Admin | Upload the same file again | "same as their source's current data — nothing new, nothing dropped"; nothing to save. |
| UP-05 | Admin | **Save N file(s) and add M new item(s)** | Saves the files and adds the new items straight away, each into its Department Review group (a decided group's Department applies at once; item matches decide Broken Out items). Existing items, groups and decisions don't change. Safety snapshot first. Result box: summary + "N new item(s) need a Department decision" with **Open group →**; **Dismiss**. |
| UP-06 | Admin | Upload with a UPC someone staged to add by hand | New items wait on the Merge tab with a warning saying whose staged add. |
| UP-07 | Admin | Upload history: Show (source), Rows dropped by an upload (pick one) | Every upload logged, with why rows were dropped. |
| UP-08 | Admin | **Go to Merge** (when a source changed since the last Merge) | Opens Merge. |

## 12. Merge (MG) — admin

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| MG-01 | Admin | **Compute Merge** | Draft of the new items (existing items never change). |
| MG-02 | Admin | Review the draft: metrics, "What differs in the files (ignored)", new items with the decision each will get (Show filter, search, CSV, group + **Open in …**) | Read-only. |
| MG-03 | Admin | **Discard draft** | Draft gone; nothing changed. |
| MG-04 | Admin | **Approve** | Recorded (editors' approvals; admins exempt). |
| MG-05 | Admin | Tick **I've reviewed this draft…** (+ **Take their staged adds back** if shown) + **Push Items to Database** | New items added, into their groups. Out-of-date draft: blocked and discarded. |
| MG-06 | Admin | Check against the rules: **Check now**, **Download the list**, **Apply these N change(s)** **(live at once)**, **Undo that fix**, **Discard** | Finds items out of line with the rules/decisions and fixes them. |
| MG-07 | Admin | Last Merge / Added by past Merges (pick a Merge) / Source order | Read-only reports. |
| MG-08 | Admin | Spot-check a UPC list (upload a file with a UPC column) | "X of Y reference UPCs matched (p%)". |

## 13. Snapshots (SN) — admin

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| SN-01 | Admin | Name + **Take Snapshot Now** | "Snapshot #N taken." |
| SN-02 | Admin | Show (All/Manual/Monthly/Automatic), search | Narrows the list. |
| SN-03 | Admin | **Details** → **Show details**, **Compare with what's live now** | What's in it; what restoring would change. |
| SN-04 | Admin | **Restore** → **Confirm restore #N** / **Cancel** | Replaces live data with the snapshot; today's saved first. |
| SN-05 | Admin | **Undo this restore** → **Confirm** / **Cancel** | Puts back the data from before the restore. |
| SN-06 | Admin | **Delete** → **Delete #N** / **Cancel** | Gone for good. |

## 14. Activity (AC)

| ID | Who | What you can do | What should happen |
|---|---|---|---|
| AC-01 | Editor+ | Filters: Period, People, Areas, Search, How | Narrows everything below. |
| AC-02 | Editor+ | Metrics + **Download these N change(s) as CSV** | Counts ("Groups touched" = the rows in By group); CSV of the filtered changes. |
| AC-03 | Editor+ | By person (+ one person day by day), By group (find, Now in), By day (per person), Every line (pages), Staged now | Read-only views of who did what. |

## 15. Rules that cut across tabs (XR)

| ID | Rule |
|---|---|
| XR-01 | An upload only adds: new items go into their groups; existing groups keep their evidence, suggestions and decisions. |
| XR-02 | A new item in a decided group takes its Department; in an undecided group it waits; in a Broken Out group an item match decides it, otherwise it waits (and a finished Broken Out group reopens for it). |
| XR-03 | Deleting or combining an item someone has work on warns first; their staged Department decision is dropped when the delete is pushed, and they're told. |
| XR-04 | An item holds one staged item change at a time (first one wins; others are told who has it). |
| XR-05 | A push only changes the items it decides (nothing else's Department moves). |
| XR-06 | Pushed decisions can't be undone with Undo — they're sent back or re-decided instead. |
| XR-07 | Same file uploaded again is recognised and changes nothing. |
