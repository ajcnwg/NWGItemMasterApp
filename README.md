# NWG Item Master App

A from-scratch, database-backed replacement for the Excel-based ingestion
pipeline in `..\BRdata PowerBI Data to Import\script.py` — configure
distributor sources, upload their files, merge by priority, and browse/edit
the resulting item master. No Excel involved anywhere in this app.

This is a fresh build (not a continuation of the old `Review App` folder,
which was moved aside to `Review App (old)` rather than deleted). It's live
with all six real distributor sources — **330,839 items** in `dbo.items`,
verified as an **exact 100% match** (every single UPC, both directions)
against script.py's real last output
(`NWGGrocersCloud_PowerBI_Item_Updates_09_10_2026.csv`).

## What this replaces from script.py

- **Data Source Definitions.xlsx** → the **Sources** tab. Add a new
  distributor (key, label, priority, column mapping, cleaning rules)
  directly in the app — no Excel workbook, no code.
- **Reading + cleaning a distributor file** → the **Upload & Ingest** tab.
  Upload the file, the app maps its columns per that source's config,
  applies that source's cleaning rules, and cleans the UPC — then stages it
  in `dbo.raw_items`.
- **The priority merge** (`merge_all_sources` in script.py) → the **Merge**
  tab. For every UPC across enabled sources, walks priority order and takes
  Department/Category/Subcategory/Brand/Description together from the first
  source with every field filled in, falling back to the highest-priority
  source with any data otherwise.
- **The final output file** → the **Item Master** tab (browse/inline-edit)
  plus **Add Item** / **Delete Item**.
- **The manual Department decision sheets** → the **Department Review**
  tab (Crosswalk/Unmatched/Broken Out/Pending Changes/Decided) — see
  below. **UPC Overrides** covers the single-item case: search one item
  and edit its fields directly (Department is a constrained dropdown
  there, unlike Item Master's free-text grid), staged through the same
  Pending Changes review as everything else.

## UPC cleaning — the one deliberate difference from script.py

Every source's UPC is cleaned the same way script.py does (strip
non-digits, drop a trailing check digit only for sources that actually
have one, keep at most the last 12 digits) — **except leading zeros are
stripped from the final result instead of zero-padding to a fixed 12
digits.** This is applied universally across all six sources. Per-source
check-digit handling (confirmed against the 100%-match ground truth —
**do not** apply a trailing-digit strip to a source that doesn't actually
have one, it silently collapses unrelated UPCs together):

| Source | Check digit dropped? | Notes |
|---|---|---|
| NWG (Scan Advantage) | No | Raw `UPC` is already the complete identifier |
| SPINS | Yes (1 digit) | 13-digit EAN → drop check digit |
| URM | Yes (1 digit) | `GTIN Product Code` → drop check digit |
| KEHE | Yes (1 digit) | `UPC12` → drop check digit |
| C&S PNW / CA | No | Raw `UPC` is already the complete identifier |

## Per-source cleaning rules (all editable on the Sources tab)

Ported 1:1 from script.py's hardcoded loaders and its
`DEFAULT_BRAND_CLEANING_RULES` / `DEFAULT_DEPARTMENT_CLEANING_RULES`
constants (confirmed there are no more rules than these — those constants
are the actual complete list, the docstring wasn't summarizing more):

| Source | Rule |
|---|---|
| NWG | Brand `_` → blanked |
| NWG | Department `PENDING FOR ASSIGNMENT` → Department/Category/Subcategory blanked |
| NWG | A blank Department defaults to `GENERAL MERCHANDISE` |
| SPINS | Department `OTHER` → Department/Category/Subcategory blanked |
| SPINS | Brand ending in `PL` → becomes exactly `PL` |
| SPINS | Among duplicate UPCs, prefers dropping the one branded `PL` |
| URM | `Group`/`Subgroup` have a leading numeric code + space stripped (`strip_leading_code_fields`) |
| KEHE | Rows where `UOM` is `DS` or `PL` excluded entirely (`exclude_column`/`exclude_values`) |

Each of these is a real column on `dbo.sources` (not hardcoded per-source
Python) — see `migrations/001_create_schema.py` for the schema, and the
Sources tab / Add Source form's "Advanced cleaning rules" section for the UI.

## The monthly refresh workflow

This app is meant to be re-run every month (e.g. the 1st) as each
distributor's new file comes in:

1. **Upload & Ingest** the new file for a source — it REPLACES that
   source's previously staged rows in `raw_items`.
2. **Every upload is permanently logged** (`ingestion_log` +
   `ingestion_rejected_rows`), not just the first time. The Upload & Ingest
   tab shows the full history for whichever source you have selected, and
   lets you pick any past upload to see exactly which rows were dropped and
   why:
   - **`invalid_upc`** — no usable digits in the UPC column (blank, text,
     or a placeholder like C&S's `"0"` for items with no real UPC).
   - **`duplicate_upc`** — the same UPC appeared more than once in that file.
   - **`excluded_value`** — dropped by that source's own Exclude
     Column/Values rule (e.g. KEHE's `UOM` = `DS`/`PL`).
3. **Run Merge.** This rebuilds `dbo.items` from scratch out of
   `raw_items` — but manual work survives it:
   - **Manual edits** (Item Master inline edits) and **manually-added
     items** (Add Item) are saved to `dbo.manual_overrides` and re-applied
     on top of every merge, field by field.
   - **Manual deletions** are remembered in `dbo.deleted_upcs` (with a full
     snapshot of the item's data) so a source that still lists a deleted UPC
     doesn't silently bring it back. The Delete Item tab has a "Deleted
     items" list (searchable) with a **Restore Item** button that brings
     the item back with its original data intact.
4. **Spot-check.** The Merge tab has a "Spot-check against a reference UPC
   list" tool — upload any known-good UPC list (a prior export, etc.) and
   it reports match %, flagging anything under 50% as a likely cleaning bug.
   This is exactly how the 100%-match validation above was done and caught
   a real mistake along the way (see below).

## Department Review — the crosswalk/decision workflow

This is the largest part of the app (`dept_mapping.py`) and the piece
script.py's original "Workbooks To Edit" Department decision sheets are
replaced by. When a merge pulls in a source/department/category/
subcategory combination that isn't NWG's own trusted data, it doesn't get
pushed into `dbo.items` as raw text — it lands here for a human (or the
engine's own auto-matching) to decide first. Sub-tabs, left to right:

- **Crosswalk** — combos with enough evidence (overlap with NWG's own
  department for the same UPCs) to suggest a department with confidence.
  Approve the suggestion, pick something else, or send the whole combo to
  **Broken Out** if it's genuinely a mix of departments.
- **Unmatched** — combos with no evidence to suggest from at all; pick a
  department by hand or break it out.
- **Broken Out** — per-UPC decisions inside a combo that's a real mix
  (e.g. a KEHE category spanning both Health/Body Care and Grocery).
  Editable **Excel-like grid** (`st.data_editor` with a department
  dropdown column) so you can select, paste, or drag-fill department
  values across many rows at once, the way you would in a spreadsheet.
  Whoever decides a UPC first **owns** it; anyone else who disagrees
  submits a suggestion the owner (or admin) explicitly accepts or denies —
  never a silent majority vote.
- **Pending Changes** — the staging area before anything actually writes
  to `dbo.items`. Laid out top to bottom: the push confirmation (how many
  items are about to change, a confirm checkbox, the **Push** button),
  **Ready to push**, **Saved for later** (anything you've unchecked to
  hold back from the next push), **Needs agreement** (combos/groups with
  an unresolved dispute — a Broken Out group with even one open suggestion
  sits here in full, never half-pushed), and **Undo requested**.
  Undo authority: the original stager of a combo, or the first person to
  ever decide a UPC in a Broken Out group, can undo it alone; anyone else's
  click just files a request that shows up under Undo requested for the
  authorized person (or an admin) to action.
- **Decided** — everything already pushed to `dbo.items` through this
  workflow, with a plain-language note on how it was decided, and one
  click to send it back to Crosswalk/Unmatched/Broken Out if a decision
  turns out to be wrong.
- **Settings** — the reference tables the engine reads: canonical
  Department list, Unmatched Defaults, Strict Departments, and matching
  thresholds (minimum purity/sample size, chaining rounds, etc.).

**Admin override**: an admin can force any combo or Broken Out group to a
specific department at any time. Once set, it's locked — no one else's
Approve/Suggest/Agree does anything until an admin either changes the
override or explicitly removes it (which reopens it to normal review
rather than silently keeping the last value).

## What was deliberately left out of this version

- **Brand** crosswalk/decision — only **Department** is covered by the
  review workflow above; Brand still passes straight through from the
  merge as-is.
- Derived/combined columns beyond the two implemented (`upc_suffix_column`
  for a UPC split across two raw columns; `strip_leading_code_fields` for a
  leading numeric code) — script.py's more general "Combine Two Columns"
  rule (e.g. UNFI Natural's Segment/Sub-Segment) isn't ported.

## Setup

1. **Install dependencies:**
   ```bash
   python -m pip install -r requirements.txt
   ```

2. **Copy the example config files and fill in real values** (both are
   gitignored so your credentials never end up in source control):
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
   `.env` holds the Azure SQL connection details. `config.yaml` holds login
   accounts — use `scripts/hash_password.py` to generate a bcrypt hash for each
   user's password, and generate a real random `cookie.key` (see the
   comment in `config.example.yaml`) rather than leaving the placeholder.
   Roles: `admin` (full access — Sources, Upload & Ingest, Merge, Snapshots,
   editing Item Master directly), `editor` (Item Master read-only,
   Department Review, Add/Delete Item, UPC Overrides; everything else
   hidden), `viewer` (Item Master only, read-only).

3. **Create the database schema** (safe to re-run; every statement is
   guarded so it only creates what's missing):
   ```bash
   python migrations/001_create_schema.py
   ```
   This builds every table the app needs in one pass. If a future change
   needs a schema update, it'll ship as a new `migrations/002_*.py` and so
   on — run any new ones the same way, in order.

4. **Run the app:**
   ```bash
   python -m streamlit run app.py
   ```

   With 330K+ items, the Item Master and Deleted Items tabs use real paging
   (Rows Per Page + Page controls, like a normal data grid) rather than
   loading everything into the browser at once. Department/Brand filters on
   Item Master are cascading — picking a Department narrows the Brand list
   to only brands that actually appear in it, and vice versa.

   `dbo.items(department)` and `dbo.items(brand)` have indexes
   (see `migrations/001_create_schema.py`) — these speed up `load_items()`'s initial
   `ORDER BY Department, Category, Description` (avoids a full sort) and
   would matter more if filtering is ever pushed down into SQL; today's
   Department/Brand/Search filtering happens client-side in pandas against
   the already-cached DataFrame, so the indexes aren't the reason clicking
   a filter feels instant — the 5-second cache on `load_items()` is.

## The six real sources currently configured

| source_key | Label | Priority | File (in `Inputs\`) |
|---|---|---|---|
| `nwg` | Scan Advantage | 1 | `NWG (26we 07.09.26).xlsx` |
| `spins` | SPINS | 2 | `SPINS_Total-UPC-wHeirarchy_Ran7-7-26.xlsx` |
| `urm` | URM | 3 | `Weekly URM Item List w Linking.2026-07-20-05-11-51.xlsx` (sheets: `Grocery`, `General Merchandise`, combined) |
| `kehe` | KEHE | 4 | `KEHE Link-Codes-Master 7.7.26.xlsx` |
| `cs_pnw` | C&S PNW | 5 | `CS PNW Daily Order Guide 7.20.26.xlsx` (sheet: `ALL ITEMS`) |
| `cs_ca` | C&S CA | 6 | `CS CA Daily Order Guide 7.20.26.xlsx` (sheet: `ALL ITEMS`) |

Lower priority number wins when the same UPC appears in multiple sources —
matches script.py's original `PRIORITY_ORDER`.

**Note on URM**: there's also a newer-format `URM Order Guide *.xlsx` file
that was tried first — it produced a real but different item set (not what
generated the historical ground truth), so it's not currently used. Use the
`Weekly URM Item List` file above until told otherwise.

`scripts/seed_real_sources.py` + `scripts/bulk_stage_and_merge.py` did the
initial one-time setup and load of all six from `Inputs\` (bypassing the
Sources/Upload & Ingest UI, since it's not practical to click through six
~10-25MB files one at a time). They're specific to this deployment's six
real distributor files, not generic setup scripts — every month going
forward (and for any new deployment), use the Sources and Upload & Ingest
tabs in the app itself.

## Adding a new distributor source (e.g. UNFI Natural)

On the **Sources** tab, under "Add a new source", you can optionally upload
a sample file first and click **Analyze File and Fill In Form** — it
detects the real header row (skipping title banners, matching e.g. NWG's
row-2 header) and guesses which column is UPC/Department/Category/
Subcategory/Brand/Description by keyword, in `autodetect.py`
(`detect_header_row`, `guess_column_mapping`). This is generic across any
distributor's naming, not hardcoded to any one source — tested against all
six real files, it filled in every field correctly for SPINS/KEHE/URM/NWG,
and got the unambiguous fields right for C&S PNW (missed 2 genuinely
ambiguous ones, e.g. C&S's `GL DESCRIPTION` for Department, which needs a
person's judgment). **It never combines two columns or figures out
cleaning rules on its own** — e.g. UNFI Natural's Segment/Sub-Segment
should really be combined for Subcategory, but the auto-fill will only
guess `Segment` alone, which you'd then adjust. Always review every field
it fills in before saving — it's a starting point, not a final answer.

Then fill in (or correct) the rest of the form by hand:
- **Priority Rank**: lower number = higher priority on a shared UPC
- **UPC Column** / **UPC Suffix Column** / **Strip Trailing Digits**: how
  to build and clean this source's UPC — suffix column only if the UPC is
  split across two raw columns (e.g. a base code + separate check digit).
  **Verify with the Merge tab's spot-check tool before trusting a new
  source's cleaning** — don't assume a source has (or doesn't have) a
  check digit; a wrong guess silently collapses unrelated UPCs together.
- **Department/Category/Subcategory/Brand/Description Column**: the exact
  header text in that distributor's file
- **Advanced cleaning rules** (optional, in the same form): Exclude
  Column/Values, blanking rules, brand-suffix rename, dedup priority,
  leading-code stripping — see the table above for real examples

Then go to **Upload & Ingest**, pick that source, and upload its file
(`.xlsx`, `.xls`, `.xlsb`, or `.csv` — `.xlsb` needs the `pyxlsb` package,
already in `requirements.txt`). Once every source you want included has
been uploaded, go to **Merge** and click **Run Merge**.

**UNFI Natural** was intentionally left unconfigured — the three
`UNFI Natural *.xlsb` files are meant to be added later, entirely through
this app's Sources + Upload & Ingest tabs, as the test of "can a new
source be onboarded without touching any code."
