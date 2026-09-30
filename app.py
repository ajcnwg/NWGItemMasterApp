"""
NWG Item Master App
--------------------
Self-service replacement for the Excel-based ingestion pipeline in
script.py: configure distributor sources, upload their files, merge by
source priority, review Departments, and browse/edit the resulting item
master — all backed by Azure SQL.

Streamlit runs this file top to bottom on every click. It's laid out as:

   1. Page setup: styles, friendly error screens
   2. Sign-in and the database connection
   3. Top-bar Undo tracking and database helpers
   4. Small value helpers (comparing rows, formatting)
   5. Loaders: items, sources, merge status, snapshots
   6. Settings changes, notices and push results
   7. Department Review: loaders, shared filters, search and paging
   8. Department Review: Show affected items
   9. Department Review: staged changes, votes and suggestions
  10. Department Review: recent moves, where a change came from
  11. Item Master changes: push, bulk uploads, monthly refresh
  12. Excel department workbook (download / upload) and Settings requests
  13. Pending Changes tab sections (items, sources)
  14. Broken Out grids and the Excel file for a group
  15. Popups: admin override, Undo…, send back, break out
  16. Notifications and the app error log
  17. Top bar (bell, Undo, Redo) and the account box
  18. Tabs — one function each (render_item_master_tab, render_department_review_tab
      and its sub-tabs render_dr_*, render_add_item_tab, … render_snapshots_tab)
  19. Router — runs only the chosen tab's function, then keeps the address bar in step

The data work lives in the itemmaster package (db, dept_mapping, ingest,
item_bulk, monthly_refresh, old_workbook_import); styles are in assets/.
"""

import hashlib
import html
import os
from collections import Counter
from datetime import datetime, timezone
import json
import io
import re
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from functools import partial

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitAPIException
import streamlit_authenticator as stauth
import yaml
from sqlalchemy import text

from itemmaster.autodetect import (
    analyze_source,
    guess_source_identities_batch,
    list_sheet_names,
    sheet_has_minimum_info,
)
from itemmaster.db import get_engine, robust_begin, robust_connect
from itemmaster.ingest import (
    INVALID_UPC, REASON_EXPLANATIONS, SIZE_FORMATS, clean_upc, load_raw_upload, map_and_clean,
    read_raw_file, save_raw_upload, stage_source,
)
from itemmaster import dept_mapping
from itemmaster import item_bulk
from itemmaster import monthly_refresh
from itemmaster import old_workbook_import

st.set_page_config(page_title="NWG Item Master App", layout="wide")


# ===========================================================================
# Page setup: styles, friendly error screens
# ===========================================================================

@st.cache_resource(show_spinner=False)
def _css(name: str, mtime: float = 0) -> str:  # mtime: a changed file is read again
    """The stylesheet without comments or blank lines — st.markdown would
    otherwise read those as page text."""
    import re as _re
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name), encoding="utf-8") as f:
        css = _re.sub(r"/\*.*?\*/", "", f.read(), flags=_re.S)
    return "\n".join(line.strip() for line in css.splitlines() if line.strip())


def inject_css(name: str, html: str = "") -> None:
    """The app's styles live in assets/*.css; this puts one on the page
    (plus any small bit of HTML it styles)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)
    st.markdown(f"{html}<style>{_css(name, os.path.getmtime(path))}</style>", unsafe_allow_html=True)




def _error_card(icon: str, title: str, lines: list, ex: Exception, retry_label: str = "Try again") -> None:
    """One short, calm card instead of a wall of red text: what happened,
    what to do, a retry button. Admins can unfold the raw error."""
    with st.container(border=True, key="friendly_error_card"):
        st.markdown(f"### {icon} {title}")
        for line in lines:
            st.markdown(line)
        st.button(retry_label, key="_friendly_error_retry", type="primary")
        if globals().get("is_admin"):
            with st.expander("Details (admin)"):
                st.code(f"{type(ex).__name__}: {ex}"[:3000], language=None, wrap_lines=True)


def _friendly_script_error(ex: Exception) -> bool:
    """Replaces Streamlit's red error box + traceback for everything that
    escapes the page. Database connection problems get a plain explanation
    (and fix themselves where waiting can fix them); anything else is a
    short "something went wrong" card. The full error still goes to the
    server log (Streamlit logs it before calling this)."""
    import re as _re
    from sqlalchemy.exc import DBAPIError
    from itemmaster.db import FIREWALL_MARKER, is_transient_connection_error
    msg = str(ex)
    if isinstance(ex, DBAPIError) and FIREWALL_MARKER in msg:
        ip = _re.search(r"IP address '([^']+)'", msg)
        where = f" (**{ip.group(1)}**)" if ip else ""
        _error_card("", "Can't reach the database from this network", [
            f"The database only lets in approved internet addresses, and this one{where} isn't approved yet — "
            "this usually happens after switching networks (home, office, hotspot).",
            "**To fix it:** an admin adds this address in the Azure portal → the SQL server → **Networking**. "
            "It can take up to 5 minutes to start working.",
        ], ex)
        return True
    if isinstance(ex, DBAPIError) and is_transient_connection_error(ex):
        _error_card("", "Still connecting to the database…", [
            "It's taking longer than usual to wake up. This page tries again by itself in a few seconds — "
            "nothing you were working on is lost.",
        ], ex, retry_label="Try again now")
        import streamlit.components.v1 as _components
        _components.html("<script>setTimeout(() => window.parent.location.reload(), 10000);</script>", height=0)
        return True
    # Anything else is a real bug: record it for the admins (→ App errors).
    where = " / ".join(str(v) for v in (st.session_state.get("active_tab"), st.session_state.get("dept_review_subtab")
                                        if st.session_state.get("active_tab") == "Department Review" else None) if v)
    ref = None
    if globals().get("ENGINE") is not None:
        ref = dept_mapping.log_app_error(ENGINE, st.session_state.get("name"), where, ex)
    if ref is None:
        import logging
        logging.getLogger("item_master_app").error("Unexpected error (%s)", where, exc_info=ex)
    _error_card("", "Something went wrong", [
        "That didn't work — nothing was saved halfway, so it's safe to try again.",
        f"The details were sent to the app's admin{f' (reference **#{ref}**)' if ref else ''}.",
    ], ex)
    return True


try:
    from streamlit.runtime.scriptrunner_utils.script_run_context import get_script_run_ctx as _get_ctx
    if (_ctx := _get_ctx()) is not None:
        _ctx.on_script_error = _friendly_script_error
except Exception:
    pass

# The app's one loading screen, replacing Streamlit's own "Running
# load_items()." text and faded page. Streamlit marks the app element
# data-test-script-state="running" for exactly as long as the page is being
# built, so this is pure CSS — no polling.
inject_css("loading.css", html="""
    <div class="app-busy-lock"></div>
    <div class="app-loading-overlay">
    <div class="app-loading-spinner"></div>
    <div class="app-loading-text">Loading…</div>
    </div>
""")
# Always drawn (only its class changes) so nothing below it shifts: after a
# popup closes, that one rerun changes nothing on the page — no skeletons.
st.html(f'<div class="{"app-quiet-run" if st.session_state.pop("_quiet_run", False) else "app-run"}"></div>')
# Bordered boxes (group cards, move cards, questions) carry no name CSS can
# find, so they're tagged data-app-card as they appear — the skeleton (one
# per card, in loading.css) keys off it.
st.html("""<script>
if (!window._appCardTagger) {
  const tag = () => document.querySelectorAll('[data-testid="stMain"] [data-testid="stVerticalBlock"]:not([data-app-card])')
    .forEach(b => b.setAttribute("data-app-card", parseFloat(getComputedStyle(b).borderTopWidth) > 0 ? "1" : "0"));
  window._appCardTagger = new MutationObserver(tag);
  window._appCardTagger.observe(document.body, {childList: true, subtree: true});
  tag();
}
</script>""", unsafe_allow_javascript=True)

with open("config.yaml") as f:
    auth_config = yaml.safe_load(f)

authenticator = stauth.Authenticate(
    auth_config["credentials"],
    auth_config["cookie"]["name"],
    auth_config["cookie"]["key"],
    auth_config["cookie"]["expiry_days"],
    auto_hash=False,
)

# ===========================================================================
# Sign-in and the database connection
# ===========================================================================

@st.cache_resource(show_spinner=False)
def _shared_engine():
    # One engine (and connection pool) for the whole app, not a new one per
    # click — and its connections wait out a paused database waking up.
    return get_engine()


ENGINE = _shared_engine()

authenticator.login(location="main")

auth_status = st.session_state.get("authentication_status")
if not auth_status:
    # The item master downloads while they type their password.
    dept_mapping.prefetch_items_in_background(ENGINE)
if auth_status is False:
    st.error("Username or password is incorrect.")
    st.stop()
elif auth_status is None:
    st.warning("Please enter your username and password.")
    st.stop()

username = st.session_state["username"]
user_role = auth_config["credentials"]["usernames"][username].get("role", "viewer")
# Three tiers: "admin" (everything, incl. Sources/Upload/Merge/Snapshots and
# editing Item Master directly), "editor" (a restricted reviewer — Item
# Master read-only, Department Review, Add/Delete Item, UPC Overrides —
# everything else is judged too complex/risky for this role to touch),
# "viewer" (Item Master only, read-only). is_reviewer covers both roles
# that can actually make department/item decisions; is_admin gates the
# tabs and Item Master editing that stay admin-only.
is_admin = user_role == "admin"
is_reviewer = user_role in ("admin", "editor")


# ---------------------------------------------------------------------------
# Each person's unsaved work (grid values not staged yet, their Undo / Redo
# steps, an uploaded Excel file waiting to be used) is kept in
# dbo.user_workspace, so a page refresh or signing in again loses nothing.
# ---------------------------------------------------------------------------
_WS_KEYS = ("_draft_undo", "_draft_redo", "_excel_staged_actions")


def _ws_encode(o):
    if isinstance(o, datetime):
        return {"__dt": o.isoformat()}
    if isinstance(o, (bytes, bytearray)):
        import base64
        return {"__b64": base64.b64encode(bytes(o)).decode()}
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _ws_decode(d):
    if "__dt" in d and len(d) == 1:
        return datetime.fromisoformat(d["__dt"])
    if "__b64" in d and len(d) == 1:
        import base64
        return base64.b64decode(d["__b64"])
    return d


def _workspace_items() -> dict:
    out = {}
    for k in list(st.session_state.keys()):
        v = st.session_state[k]
        if (k in _WS_KEYS or k.startswith("_grp_file_")
                or (k.startswith("wb_") and isinstance(v, dict) and "draft" in v)
                or (k.startswith(("dept_pending_include_combo_", "dept_pending_include_upc_group_")) and v is False)  # Saved for later
                or (k.startswith("dept_pending_snooze_") and v is True)):
            out[k] = json.dumps(v, default=_ws_encode, sort_keys=True)
    return out


def save_workspace() -> None:
    if not is_reviewer:
        return
    now = _workspace_items()
    saved = st.session_state.get("_ws_saved", {})
    changed = {k: v for k, v in now.items() if saved.get(k) != v}
    gone = [k for k in saved if k not in now]
    if not changed and not gone:
        return
    name = st.session_state["name"]
    with ENGINE.begin() as conn:
        for k, v in changed.items():
            conn.execute(text(
                "MERGE dbo.user_workspace AS t USING (SELECT :u AS username, :k AS item_key) AS s "
                "ON t.username = s.username AND t.item_key = s.item_key "
                "WHEN MATCHED THEN UPDATE SET payload = :p, updated_at = SYSUTCDATETIME() "
                "WHEN NOT MATCHED THEN INSERT (username, item_key, payload) VALUES (:u, :k, :p);"),
                {"u": name, "k": k, "p": v})
        for k in gone:
            conn.execute(text("DELETE FROM dbo.user_workspace WHERE username = :u AND item_key = :k"), {"u": name, "k": k})
    st.session_state["_ws_saved"] = now


def load_workspace() -> None:
    """Once per browser session: bring back this person's unsaved work
    (anything idle longer than UNDO_KEEP_HOURS is dropped first)."""
    if not is_reviewer or st.session_state.get("_ws_loaded") == st.session_state["name"]:
        return
    dept_mapping.cleanup_old_undo(ENGINE, st.session_state["name"])
    with ENGINE.connect() as conn:
        rows = conn.execute(text("SELECT item_key, payload FROM dbo.user_workspace WHERE username = :u"),
                            {"u": st.session_state["name"]}).all()
    for k, p in rows:
        st.session_state[k] = json.loads(p, object_hook=_ws_decode)
    if "_excel_staged_actions" in st.session_state:  # JSON turned its action ids into text
        st.session_state["_excel_staged_actions"] = {int(a): f for a, f in st.session_state["_excel_staged_actions"].items()}
    st.session_state["_ws_saved"] = {k: json.dumps(st.session_state[k], default=_ws_encode, sort_keys=True) for k, _ in rows}
    st.session_state["_ws_loaded"] = st.session_state["name"]
    trim_grid_undo()
    if any(k.startswith(("dept_pending_include_", "dept_pending_snooze_")) for k, _ in rows):
        st.rerun()  # Saved-for-later ticks are checkbox keys: set them before the checkboxes exist


# One id per script run = one per click. Every Department Review write made
# during this run is recorded under it, so a click that writes several
# times ("Accept all" over 12 items) is ONE thing for the top-bar Undo.
CLICK_ID = uuid.uuid4().hex


# ===========================================================================
# Top-bar Undo tracking and database helpers
# ===========================================================================

@contextmanager
def track(combo_id: int, label: str, description: str):
    """Wrap any Department Review action on a group: records the group's
    exact state before and after, so the top-bar Undo / Redo can take back
    (or put back) exactly this action. Records even when the block ends in
    st.rerun(), which works by raising."""
    before = dept_mapping.capture_combo_state(ENGINE, combo_id)
    try:
        yield
    finally:
        dept_mapping.log_action(ENGINE, st.session_state["name"], combo_id, label, description, before, click_id=CLICK_ID)
        load_undo_redo.clear()


def activity(area: str, action: str, target: str = None, n_items: int = None, combo_id: int = None, details=None) -> None:
    """One line in the admin Activity report (who changed what)."""
    dept_mapping.log_activity(ENGINE, st.session_state.get("name"), area, action, target, n_items, combo_id, details)


def _warn_retrying(attempt, delay, exc):
    # A toast (not st.warning) because this is purely transient status — it
    # almost always resolves itself within a few seconds as an idle Azure
    # SQL database wakes up, and should fade on its own rather than sitting
    # inline on the page forever like an actual, actionable error would.
    st.toast(
        f"Database connection timed out — it's likely an idle Azure SQL database waking back "
        f"up. Retrying in {delay:.0f}s (attempt {attempt + 1})...",
    )


def db_connect():
    """Drop-in replacement for ENGINE.connect() that retries a transient
    connection timeout (e.g. Azure SQL serverless waking up from idle)
    instead of crashing the page."""
    return robust_connect(ENGINE, on_retry=_warn_retrying)


def db_connect_cached():
    """Same retry behavior as db_connect(), but with NO on_retry callback —
    for use inside an @st.cache_data-decorated loader only. Streamlit
    caches every UI element a cached function renders so it can replay
    them on a future cache hit; a retry's st.toast() call can't be safely
    replayed (CacheReplayClosureError: "a streamlit element is called on
    some layout block created outside the function"), since the toast
    isn't tied to any layout that still exists at replay time. A silent
    retry still recovers from an idle Azure SQL database waking up —
    it just doesn't narrate it while doing so inside a cached call."""
    return robust_connect(ENGINE, on_retry=None)


def db_begin():
    """Drop-in replacement for ENGINE.begin() — see db_connect()."""
    return robust_begin(ENGINE, on_retry=_warn_retrying)


# ===========================================================================
# Small value helpers (comparing rows, formatting)
# ===========================================================================

def file_sheet_info(uploaded_file) -> tuple:
    """Sheet names + which ones look like they have enough info to default
    to "selected" (see sheet_has_minimum_info), computed ONCE per file and
    cached in session_state by a cheap (filename, size) identity.

    Deliberately NOT @st.cache_data: that decorator hashes its arguments to
    check the cache, and Streamlit's default hash for an UploadedFile reads
    through the file's actual bytes — for a multi-MB workbook, that's real,
    repeated work on EVERY rerun (including ones from an unrelated widget
    elsewhere on the page), which is what was causing the page to visibly
    grey out on every click. A (name, size) tuple is essentially free to
    check instead."""
    file_id = (uploaded_file.name, uploaded_file.size)
    cache = st.session_state.setdefault("file_sheet_info_cache", {})
    if file_id not in cache:
        sheet_names = list_sheet_names(uploaded_file)
        eligible = {
            sheet for sheet in (sheet_names or [None])
            if sheet_has_minimum_info(uploaded_file, sheet)
        }
        cache[file_id] = (sheet_names, eligible)
    return cache[file_id]


ITEM_EDITABLE_COLUMNS =["Description", "Department", "Category", "Subcategory", "Brand", "Pack", "Size", "UOM", "SourceKey"]
# session_state key holding Item Master edits staged against a UPC that
# already has a manual_overrides row — held here across reruns until
# explicitly confirmed or discarded, one at a time, instead of silently
# staging over someone's existing manual correction.
ITEM_MASTER_CONFLICT_KEY = "item_master_pending_conflicts"


def row_changed(edited_row, original_row, columns) -> bool:
    """True if any of `columns` actually differs between the two rows.
    Deliberately NOT edited_row[cols].equals(original_row[cols]): Streamlit's
    data_editor round-trips every value through Arrow, which can turn an
    all-null object column's None into float NaN (or otherwise change
    dtype) even when nothing was edited — pandas' Series.equals() then
    reports a difference for a column that didn't actually change,
    flagging huge numbers of untouched rows as edited (confirmed: a fresh
    unedited page of 50 rows flagged every row with a null Department,
    Category, Subcategory, or Brand). Comparing scalar-by-scalar and
    treating any two "missing" values as equal, regardless of whether
    one is None and the other NaN, avoids this false positive."""
    for col in columns:
        a, b = edited_row[col], original_row[col]
        if pd.isna(a) and pd.isna(b):
            continue
        if a != b:
            return True
    return False


def item_row_changed(edited_row, original_row) -> bool:
    return row_changed(edited_row, original_row, ITEM_EDITABLE_COLUMNS)


ITEM_MASTER_FIELD_LABELS = {
    "description": "Description", "department": "Department", "category": "Category",
    "subcategory": "Subcategory", "brand": "Brand", "pack": "Pack", "size": "Size",
    "uom": "UOM", "source_key": "Source Key",
}
# Maps the same lowercase field names above to load_items()'s column names,
# so an edit's staged (lowercase) values can be diffed against the live
# (Titlecase) item master row for the same UPC.
ITEM_MASTER_FIELD_TO_DF_COLUMN = {
    "description": "Description", "department": "Department", "category": "Category",
    "subcategory": "Subcategory", "brand": "Brand", "pack": "Pack", "size": "Size",
    "uom": "UOM", "source_key": "SourceKey",
}

SOURCE_FIELD_LABELS = {
    "source_label": "Source Label", "enabled": "Enabled", "priority_rank": "Priority Rank",
    "file_keyword": "File Keyword", "sheet_name": "Sheet Name", "header_row": "Header Row",
    "upc_column": "UPC Column", "upc_suffix_column": "UPC Suffix Column",
    "strip_trailing_digits": "Strip Trailing Digits", "department_column": "Department Column",
    "category_column": "Category Column", "subcategory_column": "Subcategory Column",
    "brand_column": "Brand Column", "description_column": "Description Column",
    "pack_column": "Pack Column", "size_column": "Size Column", "size_format": "Size Format",
    "uom_column": "UOM Column", "uom_aliases": "UOM Aliases", "exclude_column": "Exclude Column",
    "exclude_values": "Exclude Values", "blank_brand_when_equals": "Blank Brand When Equals",
    "blank_department_when_equals": "Blank Department When Equals",
    "blank_department_default": "Blank Department Default", "brand_suffix_match": "Brand Suffix Match",
    "brand_suffix_result": "Brand Suffix Result",
    "dedup_deprioritize_brand_value": "Dedup: Deprioritize Brand Value",
    "strip_leading_code_fields": "Strip Leading Code Fields", "notes": "Notes",
}


def _is_blank(v) -> bool:
    return v is None or v == "" or (isinstance(v, float) and pd.isna(v))


def _format_pending_value(v) -> str:
    if _is_blank(v):
        return "(blank)"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def diff_pending_fields(old: dict, new: dict, fields: list, labels: dict) -> list:
    """Field-by-field diff for a pending-change detail view — returns
    [(label, current, staged), ...] for fields that actually differ, treating
    any two "blank" spellings (None, NaN, "") as equal so an edit that leaves
    a field untouched doesn't show up as a spurious change."""
    diffs = []
    for f in fields:
        old_v, new_v = old.get(f), new.get(f)
        if _is_blank(old_v) and _is_blank(new_v):
            continue
        if old_v == new_v:
            continue
        diffs.append((labels.get(f, f), _format_pending_value(old_v), _format_pending_value(new_v)))
    return diffs


def render_pending_field_table(rows: list, columns: list) -> None:
    if rows:
        st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True, width='stretch')
    else:
        st.caption("Nothing to show.")


def mark_own_progress() -> None:
    """Suppresses the global full-page loading overlay for the rest of this
    script run — call once, right before a block that renders its own real
    st.progress() bar (Compute/Push Merge), so that overlay doesn't blur it
    out from under the user after ~1s. See the overlay's own CSS comment
    for why this needs a DOM marker rather than a Python-side flag."""
    st.markdown('<div class="own-progress-marker"></div>', unsafe_allow_html=True)


# ===========================================================================
# Loaders: items, sources, merge status, snapshots
# ===========================================================================

@st.cache_data(show_spinner=False)
def load_items() -> pd.DataFrame:
    # Re-downloaded only when the table actually changed — see
    # dept_mapping.load_items_df.
    return dept_mapping.load_items_df(ENGINE)


@st.cache_data(show_spinner=False)
def load_sources() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(text("SELECT * FROM dbo.sources ORDER BY priority_rank"), conn)


@st.cache_data(show_spinner=False)
def load_raw_item_counts() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, COUNT(*) AS row_count, MAX(loaded_at) AS last_loaded "
                "FROM dbo.raw_items GROUP BY source_key"
            ),
            conn,
        )


@st.cache_data(show_spinner=False)
def load_ingestion_log(source_key: str) -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT id, uploaded_at, uploaded_by, original_filename, rows_parsed, rows_staged, "
                "dropped_invalid_upc, dropped_duplicate_upc FROM dbo.ingestion_log "
                "WHERE source_key = :sk ORDER BY uploaded_at DESC"
            ),
            conn,
            params={"sk": source_key},
        )


@st.cache_data(show_spinner=False)
def load_stale_sources() -> list[str]:
    return dept_mapping.get_stale_sources_since_last_merge(ENGINE)


def render_merge_staleness_banner() -> None:
    """Item Master and Department Review both read only from dbo.items —
    never from raw_items directly — so a source whose raw data just got
    refreshed (an Upload, or a Sources config edit's Apply Now) is
    invisible here until Merge actually runs again. That gap is exactly
    what caused real confusion: Apply Now correctly updated raw_items,
    but Item Master kept showing the old data with no indication why.
    Surfacing it here, by name, on every page that reads dbo.items."""
    stale = load_stale_sources()
    if not stale:
        return
    label = "source" if len(stale) == 1 else "sources"
    message = (
        f"Raw data has changed for {len(stale)} {label} ({', '.join(stale)}) since the last "
        "Merge — this page won't reflect it until Merge runs again."
    )
    # The button only makes sense for whoever can actually reach Merge —
    # for anyone else (Editor/Viewer, who never see that tab at all) it'd
    # be a dead click that silently does nothing, since Merge isn't in
    # their tab_names for the nav-request check below to match against.
    if not is_admin:
        st.warning(message)
        return
    c1, c2 = st.columns([5, 1])
    c1.warning(message)
    with c2:
        st.markdown("<div style='padding-top:8px;'></div>", unsafe_allow_html=True)
        if st.button("Go to Merge", key=f"goto_merge_{'_'.join(stale)}"):
            st.session_state["_nav_to_tab"] = "Merge"
            st.rerun()


@st.cache_data(show_spinner=False)
def _load_snapshots(fingerprint: tuple) -> pd.DataFrame:
    return dept_mapping.list_snapshots(ENGINE)


def load_snapshots() -> pd.DataFrame:
    """Re-read whenever the snapshot list changed in the database — also
    when another admin (or a script) took or deleted one."""
    with db_connect() as conn:
        fp = tuple(conn.execute(text("SELECT COUNT(*), MAX(snapshot_id), MIN(snapshot_id), MAX(taken_at) "
                                     "FROM dbo.dept_mapping_snapshots")).one())
    return _load_snapshots(fp)


load_snapshots.clear = _load_snapshots.clear


@st.cache_data(show_spinner=False)
def load_has_snapshot_this_month() -> bool:
    return dept_mapping.has_snapshot_this_month(ENGINE)


def clear_snapshot_caches() -> None:
    load_snapshots.clear()
    load_has_snapshot_this_month.clear()


@st.cache_data(show_spinner=False)
def load_merge_compute_meta() -> dict | None:
    return dept_mapping.get_merge_compute_meta(ENGINE)


@st.cache_data(show_spinner=False)
def load_stale_sources_since_compute() -> list[str]:
    return dept_mapping.get_stale_sources_since_compute(ENGINE)


@st.cache_data(show_spinner=False)
def load_last_merge_summary() -> dict | None:
    return dept_mapping.get_last_merge_summary(ENGINE)


def render_merge_change_breakdown(changed_by_field: dict, changed_by_source: dict) -> None:
    """What actually changed, not how many rows a source happens to have
    overall — a static per-source item count barely moves month to month
    and doesn't explain a spike in Changed (e.g. a source's Size/UOM
    cleaning rule getting fixed shows up here as a concentrated size/uom
    count on that one source, not as an unrelated total row count)."""
    if not changed_by_field and not changed_by_source:
        return
    bcol1, bcol2 = st.columns(2)
    if changed_by_field:
        rows = sorted(changed_by_field.items(), key=lambda kv: -kv[1])
        rows = [(ITEM_MASTER_FIELD_LABELS.get(f, f), n) for f, n in rows]
        bcol1.caption("Differs in files, by field (ignored — existing items aren't changed)")
        bcol1.dataframe(pd.DataFrame(rows, columns=["Field", "# Items"]), hide_index=True, width='stretch')
    if changed_by_source:
        rows = sorted(changed_by_source.items(), key=lambda kv: -kv[1])
        bcol2.caption("Differs in files, by source")
        bcol2.dataframe(pd.DataFrame(rows, columns=["Source", "# Items"]), hide_index=True, width='stretch')


def clear_merge_compute_caches() -> None:
    load_merge_compute_meta.clear()
    load_stale_sources_since_compute.clear()


def auto_recompute_and_push_merge() -> dict | None:
    """Runs Compute automatically — called right after anything that
    changes raw_items (a Sources edit with Apply Now, a fresh Upload &
    Ingest) or a Department Review decision, so a fresh, accurate draft is
    always sitting on the Merge tab without a separate, easy-to-forget
    trip there. Computing is safe to do unconditionally: it only ever
    writes to items_staged/merge_compute_meta, never dbo.items, so there's
    nothing to protect against here regardless of who else has pending
    work in progress.

    Pushing that draft live is a different matter — a Merge push replaces
    the ENTIRE live item master and can discard someone else's in-progress
    Department Review/Item Master work (see push_merge_compute's own
    notes), so it's deliberately NEVER done automatically here, admin or
    not. An admin gets a lighter bar on the Merge tab itself (their own
    click is enough, no second approver needed) plus a specific warning
    there if pushing right now would affect someone else's in-progress
    work — but that warning only means anything if it happens BEFORE the
    push, which a trigger buried inside an unrelated action (a Sources
    edit, an Upload) can't show. Everyone, including admins, reviews and
    pushes from the Merge tab; only how many approvals that requires
    differs by role.

    Returns {"item_count": ...} plus the usual save_merge_compute diff
    keys, or None if there was nothing to merge yet (no enabled source
    has any raw data and no manual overrides exist)."""
    sources_df = load_sources()
    enabled_sources = sources_df[sources_df["enabled"] == True].sort_values("priority_rank")
    final_df, overrides_applied, deleted_count = dept_mapping.compute_merge_final_df(
        ENGINE, enabled_sources["source_key"].tolist(),
    )
    if final_df is None:
        return None
    compute_meta = dept_mapping.save_merge_compute(
        ENGINE, final_df, st.session_state["name"], overrides_applied, deleted_count,
    )
    clear_merge_compute_caches()
    return {"item_count": len(final_df), **compute_meta}


# ===========================================================================
# Settings changes, notices and push results
# ===========================================================================

def apply_settings_change(what: str) -> None:
    """A Settings change only matters once the Department engine re-runs
    with it — so it re-runs right away (and the item master's Department
    follows), instead of waiting for the next Merge."""
    with st.spinner(f"Saved {what}. Re-running the Department engine with the new settings (about a minute)..."):
        before = dept_mapping.combo_decision_map(ENGINE)
        try:
            summary, discarded = dept_mapping.run_engine_guarded(ENGINE, st.session_state["name"])
        except Exception as e:
            st.session_state["_toast"] = (
                f"Saved {what}, but re-running the Department engine failed, so nothing was re-decided yet ({e}). "
                "It will apply on the next Merge push, or save again to retry.", "",
            )
            st.rerun()
        after = dept_mapping.combo_decision_map(ENGINE)
        synced = dept_mapping.sync_item_departments(ENGINE)
    moved = sum(1 for cid, v in after.items() if before.get(cid) != v)
    clear_dept_review_caches()
    clear_dept_suggestion_caches()
    load_decided_combos.clear()
    load_broken_out_combos.clear()
    load_items.clear()
    clear_settings_caches()
    msg = (f"Saved {what} and re-ran the Department engine — {moved:,} group(s) changed tab or department, "
           f"{synced['items']:,} item department(s) updated in Item Master.")
    if discarded:
        msg += f" {len(discarded)} staged decision(s) were discarded because their evidence changed."
    st.session_state["_toast"] = (msg, "")
    st.rerun()


@st.cache_data(show_spinner=False)
def load_discard_notices() -> pd.DataFrame:
    return dept_mapping.get_discard_notices(ENGINE)


def _discard_kind(reason: str) -> str:
    r = (reason or "").lower()
    if "overrode" in r:
        return "Replaced by an admin override"
    if "merge" in r:
        return "Discarded by a Merge (the data under it changed)"
    return (reason or "Discarded")[:80]


def render_discard_notices(entity_type: str) -> None:
    """Tells a person their staged-but-not-pushed work was discarded — an
    admin override replaced it, or a Merge changed the data under it (see
    push_merge_compute / admin_override_upc) — so they can re-stage it.

    Scoped to the viewer: editors only see notices about their OWN work
    (and dismissing only clears theirs); admins see everyone's. One
    collapsed line no matter how many there are, opening to a one-row-per-
    group summary plus a scrollable item list — a single override across a
    big Broken Out group fires one notice per UPC, and a wall of per-item
    cards with their own buttons helped nobody."""
    notices = load_discard_notices()
    notices = notices[notices["entity_type"] == entity_type]
    actor = st.session_state["name"]
    mine = notices[notices["originally_staged_by"] == actor]
    shown = notices if is_admin else mine
    if shown.empty:
        return
    shown = shown.copy()
    shown["what"] = shown["reason"].map(_discard_kind)
    shown["group"] = shown["group_label"].fillna(shown["entity_label"])
    shown["when"] = pd.to_datetime(shown["triggered_at"], errors="coerce")

    if is_admin:
        title = f"{len(shown):,} staged change(s) didn't survive" + (f" — {len(mine):,} of them yours" if len(mine) else "")
    else:
        title = f"{len(shown):,} of your staged change(s) didn't survive"
    with st.expander(title, key=f"discard_notices_{entity_type}"):
        st.caption(
            "Something changed after these were staged, so they were discarded instead of pushed. "
            "Re-stage any that are still right."
        )
        summary = (
            shown.groupby(["group", "what", "originally_staged_by", "triggered_by"], dropna=False)
            .agg(items=("id", "count"), latest=("when", "max"))
            .reset_index()
            .sort_values("latest", ascending=False)
        )
        st.dataframe(
            pd.DataFrame({
                "Group": summary["group"],
                "Items": summary["items"],
                "What happened": summary["what"],
                **({"Staged by": summary["originally_staged_by"].fillna("")} if is_admin else {}),
                "By": summary["triggered_by"].fillna("a Merge"),
                "When": summary["latest"].dt.strftime("%m/%d %H:%M"),
            }),
            hide_index=True, width='stretch', height=min(250, 40 + 35 * len(summary)),
        )
        with st.expander(f"Every item ({len(shown):,})", key=f"discard_notice_items_{entity_type}"):
            st.dataframe(
                pd.DataFrame({
                    "Item": shown["entity_label"], "Group": shown["group"], "Details": shown["reason"],
                    **({"Staged by": shown["originally_staged_by"].fillna("")} if is_admin else {}),
                    "When": shown["when"].dt.strftime("%m/%d %H:%M"),
                }),
                hide_index=True, width='stretch', height=min(300, 40 + 35 * len(shown)),
            )
        label = f"Got it — dismiss {'these' if len(shown) > 1 else 'this'}"
        if st.button(label, key=f"dismiss_discard_shown_{entity_type}"):
            dept_mapping.dismiss_discard_notices(ENGINE, shown["id"].tolist())
            load_discard_notices.clear()
            st.rerun()


def render_blocked_item_master_edits(blocked: dict) -> None:
    """First-write-wins feedback for Item Master/Add Item/Delete Item/UPC
    Overrides — shown immediately to whoever's edit just got skipped
    because someone else already had a pending edit staged for that same
    UPC. `blocked` is {upc: {change_type, description, department,
    staged_by, staged_at}} as returned by save_item_master_pending(_bulk)."""
    if not blocked:
        return
    label = "item" if len(blocked) == 1 else "items"
    with st.container(border=True):
        st.error(f"{len(blocked)} {label} already had a pending edit staged by someone else — yours wasn't saved for those.")
        for upc, row in blocked.items():
            when_str = pd.to_datetime(row.get("staged_at"), errors="coerce")
            when_str = when_str.strftime("%Y-%m-%d %H:%M") if pd.notna(when_str) else ""
            st.caption(
                f"**{upc}** — {row.get('description') or '(no description)'}: already staged as a "
                f"**{row['change_type']}** to **{row.get('department') or '(no department)'}** by "
                f"**{row.get('staged_by') or 'unknown'}**" + (f" on {when_str}" if when_str else "") +
                " — undo it on Pending Changes first if you want to replace it with yours."
            )


def render_auto_merge_result(result: dict | None) -> None:
    """Compact status line for wherever auto_recompute_and_push_merge()
    gets triggered — the full detail (Added/Changed/Removed, per-field/
    per-source breakdown) is always still on the Merge tab's own ready-
    to-push panel; this just points there. Never reports a push here —
    auto_recompute_and_push_merge only ever computes now; the actual push
    is always a conscious action on the Merge tab (see its own docstring
    for why: a pending-work override warning only means anything shown
    BEFORE the push, which an unrelated trigger can't do)."""
    if result is None:
        return
    # Editors/viewers can't see the Merge tab at all — telling them to go
    # review it there would be a dead end. They still deserve to know a
    # fresh draft exists and that it's now an admin's move, just not a
    # link to a tab that isn't there for them.
    where = "Review and push it from the Merge tab when ready." if is_admin else "An admin will review and push it."
    st.info(
        f"\U0001f504 A fresh merge draft is ready — {result.get('added_count') or 0:,} new item(s) to add "
        f"(existing items are never changed by a Merge). {where}"
    )


@st.cache_data(show_spinner=False)
def load_rejected_rows(log_id: int) -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT reason, raw_upc, department, category, subcategory, brand, description "
                "FROM dbo.ingestion_rejected_rows WHERE log_id = :log_id ORDER BY reason"
            ),
            conn,
            params={"log_id": log_id},
        )


@st.cache_data(show_spinner=False)
def load_deleted_items() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT upc, description, department, category, subcategory, brand, pack, size, uom, "
                "deleted_by, deleted_at FROM dbo.deleted_upcs ORDER BY deleted_at DESC"
            ),
            conn,
        )


@st.cache_data(show_spinner=False)
def load_manual_items() -> pd.DataFrame:
    """Items with no distributor source of their own — i.e. added by hand
    on the Add Item tab, not pulled in by any Merge. Freshly-added ones have
    source_key NULL (the Add Item insert doesn't set it); once a Merge runs,
    the merge logic relabels any surviving no-source row's source_key as
    'manual' instead of leaving it NULL — so both need to match here, or a
    manually-added item would silently vanish from this list the moment
    someone next clicks Run Merge."""
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT upc AS UPC, description AS Description, department AS Department, "
                "category AS Category, subcategory AS Subcategory, brand AS Brand, "
                "pack AS Pack, size AS Size, uom AS UOM, "
                "created_at AS CreatedAt, updated_at AS UpdatedAt "
                "FROM dbo.items WHERE source_key IS NULL OR source_key = 'manual' "
                "ORDER BY created_at DESC"
            ),
            conn,
        )


# ===========================================================================
# Department Review: loaders, shared filters, search and paging
# ===========================================================================

@st.cache_data(show_spinner=False)
def load_dept_review_queue(tier: str) -> pd.DataFrame:
    return dept_mapping.get_review_queue(ENGINE, tier)


@st.cache_data(show_spinner=False)
def load_combo_member_items(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_combo_member_items(ENGINE, combo_id)


@st.cache_data(show_spinner=False)
def load_broken_out_combos() -> pd.DataFrame:
    return dept_mapping.get_broken_out_combos(ENGINE)


@st.cache_data(show_spinner=False)
def load_pending_upc_overrides(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_pending_upc_overrides(ENGINE, combo_id)


@st.cache_data(show_spinner=False)
def load_auto_decided_upc_overrides(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_auto_decided_upc_overrides(ENGINE, combo_id)


@st.cache_data(show_spinner=False)
def load_decided_combos() -> pd.DataFrame:
    return dept_mapping.get_decided_combos(ENGINE)


@st.cache_data(show_spinner=False)
def load_combo_upc_decisions(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_combo_upc_decisions(ENGINE, combo_id)


@st.cache_data(show_spinner=False)
def load_strict_departments() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, old_department, trust_direct_evidence "
                "FROM dbo.dept_mapping_strict_departments ORDER BY source_key, old_department"
            ),
            conn,
        )


@st.cache_data(show_spinner=False)
def load_departments() -> pd.DataFrame:
    return dept_mapping.get_departments(ENGINE)


@st.cache_data(show_spinner=False)
def load_unmatched_defaults() -> pd.DataFrame:
    return dept_mapping.get_unmatched_defaults(ENGINE)


@st.cache_data(show_spinner=False)
def load_unmatched_old_departments() -> pd.DataFrame:
    return dept_mapping.unmatched_old_departments(ENGINE)


@st.cache_data(show_spinner=False)
def load_department_usage() -> pd.DataFrame:
    return dept_mapping.department_usage_all(ENGINE)


@st.cache_data(show_spinner=False)
def load_old_department_texts() -> list:
    """Every raw Department text the distributors use (for picking a Strict Department)."""
    with db_connect_cached() as conn:
        return conn.execute(text(
            "SELECT DISTINCT UPPER(LTRIM(RTRIM(raw_department))) FROM dbo.dept_mapping_combos "
            "WHERE ISNULL(raw_department, '') <> '' ORDER BY 1")).scalars().all()


def clear_settings_caches() -> None:
    for f in (load_departments, load_strict_departments, load_unmatched_defaults, load_unmatched_old_departments,
              load_department_usage):
        f.clear()


def sql_value(v):
    """Pandas represents a SQL NULL as NaN (a float) once it's in a
    DataFrame — pyodbc can't bind that into an NVARCHAR column ('not a
    valid instance of data type float'). Convert it back to a real None
    before passing any DataFrame-sourced value as a bound query parameter."""
    return None if pd.isna(v) else v


SOURCE_NUMERIC_BOOL_COLUMNS = {"enabled", "priority_rank", "header_row", "strip_trailing_digits"}


def source_grid_value(row, col):
    """Same NaN-to-None fix as sql_value, plus: clearing a text cell in
    the Sources grid leaves "" behind, not a real blank (NULL) — unlike
    the Add Source form's own fields, which already go through `x or
    None`. Left alone, a cleared Pack/Size/etc. Column would stage as an
    empty string forever instead of actually clearing the mapping.
    Skipped for the handful of numeric/boolean columns, where an empty
    string never appears and 0/False must NOT be coerced to None."""
    v = sql_value(row[col])
    if col not in SOURCE_NUMERIC_BOOL_COLUMNS and v == "":
        return None
    return v


def evidence_sentence(row) -> str:
    """Why a Crosswalk/Unmatched group isn't decided on its own, in a few words."""
    suggested = row["suggested_department"]
    if pd.isna(suggested):
        suggested = None
    resolved_via = row["resolved_via"]
    if pd.isna(resolved_via):
        resolved_via = None
    if resolved_via == "Default from Key":
        return f"No evidence · saved default: **{suggested}**"
    n_evidence = row["n_evidence"]
    if not n_evidence or not suggested:
        return "No evidence — pick a Department"
    if resolved_via and "single sibling" in resolved_via:
        return f"One similar group suggests **{suggested}**"
    purity = row["purity"]
    pct = f"{purity:.0%}" if not pd.isna(purity) else "0%"
    sentence = f"{pct} of {int(n_evidence):,} matched item(s) say **{suggested}**"
    runner_up = row["runner_up_department"]
    if runner_up and not pd.isna(runner_up):
        sentence += f" · {row['runner_up_share']:.0%} {runner_up}"
    if resolved_via in ("Partially Chained", "Chained - Insufficient"):
        sentence += " · partly via another distributor"
    return sentence


GROUP_PAGE_SIZES = [10, 25, 50, 100]
DEFAULT_GROUP_PAGE_SIZE = 25


def get_shared_dept_filter() -> dict:
    """The ONE search/sort/page-size state every UNLOCKED Department
    Review tab (Crosswalk/Unmatched/Broken Out/Decided) mirrors — change
    it on any unlocked tab and every other unlocked tab picks it up the
    next time it renders. A tab with its own "Lock filters" checked keeps
    its private settings instead (see get_dept_tab_locks / get_dept_tab_
    filters) and neither reads from nor writes to this."""
    return st.session_state.setdefault("dept_shared_filter", {
        "search": "", "sort_label": None, "sort_desc": True, "page_size": DEFAULT_GROUP_PAGE_SIZE,
    })


def get_dept_tab_locks() -> dict:
    """Per-tab "Lock filters" state, default False (unlocked, follows the
    shared settings). Persists independently of the widget-cleanup issue
    get_dept_tab_filters works around, since it's never a widget's own key."""
    return st.session_state.setdefault("dept_tab_locks", {})


def get_dept_tab_filters() -> dict:
    """Each LOCKED tab's own private search/sort/page-size — deliberately
    a plain dict entry, not the widget's own session_state key. Streamlit
    quietly deletes a widget's session_state entry for any run in which
    that widget isn't drawn (documented cleanup behavior, to avoid
    leaking state for conditionally-shown widgets) — and since only ONE
    Department Review sub-tab's widgets are ever drawn per run (the
    elif-chain this whole section is built from), switching to a
    different tab and back was silently wiping the widget's own value.
    This dict is never a widget's key, so it survives that cleanup, and
    every render explicitly re-seeds the real widget from it (from here
    when locked, from the shared dict above otherwise)."""
    return st.session_state.setdefault("dept_tab_filters", {})


def _dept_filter_keys(tab_key: str) -> tuple:
    return (f"{tab_key}_search", f"{tab_key}_sort_by", f"{tab_key}_sort_desc", f"{tab_key}_page_size")


def _current_dept_values(tab_key: str) -> dict:
    search_key, sort_key, desc_key, size_key = _dept_filter_keys(tab_key)
    return {
        "search": st.session_state.get(search_key, ""),
        "sort_label": st.session_state.get(sort_key),
        "sort_desc": st.session_state.get(desc_key, True),
        "page_size": st.session_state.get(size_key, DEFAULT_GROUP_PAGE_SIZE),
    }


def _sync_dept_tab(tab_key: str) -> None:
    """Called on every filter-widget change: always remembers this tab's
    own current values (so they're not lost if it gets locked later), and
    — unless this tab is locked — also pushes them into the shared state
    so every OTHER unlocked tab picks them up next render. This is what
    makes "clear this tab" or "change sort here" propagate everywhere by
    default, with locking as the opt-out rather than the other way
    around."""
    values = _current_dept_values(tab_key)
    get_dept_tab_filters()[tab_key] = values
    if not get_dept_tab_locks().get(tab_key, False):
        get_shared_dept_filter().update(values)


def get_dept_facets(tab_key: str) -> dict:
    """Each tab's own Source / Department / … picks — a plain dict, not the
    widgets' keys (see get_dept_tab_filters for why)."""
    return st.session_state.setdefault("dept_facets", {}).setdefault(tab_key, {})


def apply_facets(df: pd.DataFrame, picks: dict) -> pd.DataFrame:
    for col, values in picks.items():
        if values and col in df.columns:
            df = df[df[col].fillna("(none)").astype(str).isin(values)]
    return df


def render_filter_bar(tab_key: str, sort_options: dict, search_label: str, search_placeholder: str,
                      facets: dict = None, df: pd.DataFrame = None) -> tuple:
    """Search + Sort by + Descending, shared across Crosswalk/Unmatched/
    Broken Out/Decided by default (see get_shared_dept_filter) — a
    "Lock filters" checkbox opts one tab out of that sharing, and "Clear
    filters" resets to true hard defaults (empty search, first sort
    option, descending) and broadcasts that reset too, unless locked.
    Also sorts the FULL (pre-pagination) DataFrame, not just whichever
    page renders — st.data_editor/st.dataframe's own click-a-column-
    header sort only reorders the current page, which silently hides
    matches sitting on other pages (confirmed as a real, misleading bug).
    sort_options: {display label: column name}.
    facets: {label: column} dropdowns (e.g. Source, Department), their
    choices taken from df; the picks are this tab's own. Returns
    (search_text, sort_column_name, sort_desc) — or, with facets, also the
    picks for apply_facets as a 4th value."""
    shared = get_shared_dept_filter()
    locks = get_dept_tab_locks()
    own = get_dept_tab_filters().get(tab_key)
    search_key, sort_key, desc_key, size_key = _dept_filter_keys(tab_key)
    lock_key = f"{tab_key}_lock"
    default_sort_label = next(iter(sort_options))
    is_locked = locks.get(tab_key, False)
    st.session_state[lock_key] = is_locked
    # Always re-seed the actual widgets from the right durable store —
    # every run, regardless of lock state — since a widget's own
    # session_state entry cannot be trusted to have survived if this tab
    # wasn't the one rendered last time (see get_dept_tab_filters).
    source = own if (is_locked and own) else shared
    st.session_state[search_key] = source["search"]
    st.session_state[sort_key] = source["sort_label"] if source["sort_label"] in sort_options else default_sort_label
    st.session_state[desc_key] = source["sort_desc"]
    st.session_state[size_key] = source["page_size"] if source["page_size"] in GROUP_PAGE_SIZES else DEFAULT_GROUP_PAGE_SIZE

    def _touch():
        _sync_dept_tab(tab_key)

    def _toggle_lock():
        locks[tab_key] = st.session_state[lock_key]
        # Locking freezes exactly what's showing right now as this tab's
        # own private settings, so toggling it on never loses anything.
        get_dept_tab_filters()[tab_key] = _current_dept_values(tab_key)

    fcol1, fcol2, fcol3 = st.columns([4, 1, 1.2])
    search = fcol1.text_input(
        search_label, key=search_key, label_visibility="collapsed",
        placeholder=search_placeholder, on_change=_touch,
    )
    with fcol2.container(key=f"{tab_key}_lock_wrap"):
        st.checkbox(
            "Lock filters", key=lock_key, on_change=_toggle_lock,
            help="Keep this tab's own filters — otherwise, changing filters on any tab (including Clear) updates every other unlocked tab too.",
        )
    if fcol3.button("Clear filters", key=f"{tab_key}_clear_filters", width='stretch'):
        defaults = {"search": "", "sort_label": default_sort_label, "sort_desc": True, "page_size": DEFAULT_GROUP_PAGE_SIZE}
        get_dept_tab_filters()[tab_key] = defaults
        get_dept_facets(tab_key).clear()
        if not is_locked:
            shared.update(defaults)
        st.rerun()

    facets = facets or {}
    picks = get_dept_facets(tab_key)
    cols = st.columns([1.3] * len(facets) + [1.6, 0.9])
    for (flabel, col), c in zip(facets.items(), cols):
        choices = sorted(df[col].fillna("(none)").astype(str).unique()) if df is not None and col in df.columns else []
        wkey = f"{tab_key}_facet_{col}"
        st.session_state[wkey] = [v for v in picks.get(col, []) if v in choices]

        def _pick(col=col, wkey=wkey):
            picks[col] = st.session_state[wkey]
        c.multiselect(flabel, choices, key=wkey, on_change=_pick, placeholder=f"All {flabel.lower()}" + ("es" if flabel.endswith("s") else "s"),
                      label_visibility="collapsed", format_func=lambda v, col=col: v.upper() if col == "source_key" else v)
    sort_label = cols[-2].selectbox("Sort by", list(sort_options.keys()), key=sort_key, on_change=_touch,
                                    label_visibility="collapsed", format_func=lambda x: f"Sort: {x}")
    with cols[-1].container(key="dept_sort_desc_wrap"):
        sort_desc = st.checkbox("Descending", key=desc_key, on_change=_touch)
    if facets:
        return search, sort_options[sort_label], sort_desc, {c: picks.get(c, []) for c in facets.values()}
    return search, sort_options[sort_label], sort_desc


def render_page_controls(tab_key: str, page_num_key: str, df_len: int, matching_label: str) -> tuple:
    """Groups-per-page + Page + a "N matching — page X of Y" caption, all
    on one row and vertically aligned against the two dropdowns (see the
    im_page_caption/manual_page_caption/deleted_page_caption CSS rule this
    reuses). Page SIZE is shared/lockable like the rest of the filter bar;
    page NUMBER is deliberately left out of that — jumping to "page 3" on
    one tab has no sensible meaning to carry over to a different list on
    another tab, so it always just starts back at 1 there.
    Returns (page_size, page_num, total_pages)."""
    shared = get_shared_dept_filter()
    locks = get_dept_tab_locks()
    own = get_dept_tab_filters().get(tab_key)
    size_key = _dept_filter_keys(tab_key)[3]
    is_locked = locks.get(tab_key, False)
    source = own if (is_locked and own) else shared
    st.session_state[size_key] = source["page_size"] if source["page_size"] in GROUP_PAGE_SIZES else DEFAULT_GROUP_PAGE_SIZE

    def _touch():
        _sync_dept_tab(tab_key)

    # Two-pass total_pages, same as Item Master's own pagination: the
    # selectbox itself can change page_size THIS run (its on_change fires
    # before this line, per Streamlit's callback order), so total_pages
    # has to be recomputed from its returned value, not the pre-seeded
    # guess, before the Page number_input's own max_value is set.
    total_pages = max(1, (df_len - 1) // st.session_state[size_key] + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages
    pcol3, pcol1, pcol2 = st.columns([3.4, 1.1, 1], vertical_alignment="center")
    page_size = pcol1.selectbox("Groups per page", GROUP_PAGE_SIZES, key=size_key, on_change=_touch,
                                label_visibility="collapsed", format_func=lambda n: f"{n} per page")
    total_pages = max(1, (df_len - 1) // page_size + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages
    page_num = pcol2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=page_num_key,
                                  label_visibility="collapsed", help="Page")
    with pcol3.container(key=f"{tab_key}_page_caption"):
        st.caption(f"{matching_label} · page {page_num} of {total_pages}")
    return page_size, page_num, total_pages


def render_search_bar(tab_key: str, search_label: str, search_placeholder: str) -> str:
    """Search + Lock-filters variant of render_filter_bar, for Pending
    Changes — three separate lists (recent moves, group changes, item
    changes) rather than one sortable/paginated table, but still
    participates in the same shared-search default/lock/"Clear filters"
    convention as the other Department Review tabs."""
    shared = get_shared_dept_filter()
    locks = get_dept_tab_locks()
    own = get_dept_tab_filters().get(tab_key)
    search_key = f"{tab_key}_search"
    lock_key = f"{tab_key}_lock"
    is_locked = locks.get(tab_key, False)
    st.session_state[lock_key] = is_locked
    st.session_state[search_key] = (own if (is_locked and own) else shared)["search"]

    def _touch():
        value = st.session_state.get(search_key, "")
        get_dept_tab_filters()[tab_key] = {"search": value}
        if not locks.get(tab_key, False):
            shared["search"] = value

    def _toggle_lock():
        locks[tab_key] = st.session_state[lock_key]
        get_dept_tab_filters()[tab_key] = {"search": st.session_state.get(search_key, "")}

    fcol1, fcol2, fcol3 = st.columns([4, 1, 1.2])
    search = fcol1.text_input(
        search_label, key=search_key, label_visibility="collapsed",
        placeholder=search_placeholder, on_change=_touch,
    )
    with fcol2.container(key=f"{tab_key}_lock_wrap"):
        st.checkbox("Lock filters", key=lock_key, on_change=_toggle_lock)
    if fcol3.button("Clear filters", key=f"{tab_key}_clear_filters", width='stretch'):
        get_dept_tab_filters()[tab_key] = {"search": ""}
        if not is_locked:
            shared["search"] = ""
        st.rerun()
    return search


def search_groups(df: pd.DataFrame, search: str, extra_cols: tuple = ()) -> pd.DataFrame:
    """The groups whose source, Department / Category / Subcategory text or
    full label (plus any extra_cols) contain `search` — the search box on
    Crosswalk, Unmatched, Broken Out and Decided."""
    if not search:
        return df
    s = search.lower()
    mask = (
        df["source_key"].str.lower().str.contains(s, na=False)
        | df["raw_department"].str.lower().str.contains(s, na=False)
        | df["raw_category"].str.lower().str.contains(s, na=False)
        | df["raw_subcategory"].str.lower().str.contains(s, na=False)
        | combo_label_series(df).str.lower().str.contains(s, regex=False)
    )
    for c in extra_cols:
        mask |= df[c].fillna("").astype(str).str.lower().str.contains(s, na=False)
    return df[mask]


def combo_label_series(df: pd.DataFrame) -> pd.Series:
    """Each group's full "Department / Category / Subcategory" label, so a
    search pasted from a notification or report finds it."""
    parts = df[["raw_department", "raw_category", "raw_subcategory"]].fillna("").astype(str)
    return parts.apply(lambda r: " / ".join(x for x in r if x), axis=1) if not df.empty else pd.Series([], dtype=str)


def sort_full_df(df: pd.DataFrame, column: str, sort_desc: bool) -> pd.DataFrame:
    return df.sort_values(
        column, ascending=not sort_desc, na_position="last",
        key=lambda s: s.astype(str).str.lower() if s.dtype == object else s,
    )


def render_bottom_pagination(page_size_key: str, page_num_key: str, tab_key: str, total_pages: int) -> None:
    """Groups-per-page + Page controls repeated at the bottom of a long
    list, synced to the SAME state the top controls use (two Streamlit
    widgets can't share one key directly) — so switching pages doesn't
    mean scrolling all the way back to the top every time."""
    size_widget_key, num_widget_key = f"{page_size_key}_bottom", f"{page_num_key}_bottom"
    st.session_state[size_widget_key] = st.session_state.get(page_size_key, DEFAULT_GROUP_PAGE_SIZE)
    st.session_state[num_widget_key] = min(st.session_state.get(page_num_key, 1), total_pages)

    def _sync_size():
        st.session_state[page_size_key] = st.session_state[size_widget_key]
        st.session_state[page_num_key] = 1
        _sync_dept_tab(tab_key)

    def _sync_num():
        st.session_state[page_num_key] = st.session_state[num_widget_key]

    st.divider()
    c1, c2, _ = st.columns([1, 1, 3])
    c1.selectbox("Groups per page", GROUP_PAGE_SIZES, key=size_widget_key, on_change=_sync_size)
    c2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=num_widget_key, on_change=_sync_num)


# Up to this many items, a group's list comes with the page (all the page's
# lists in one query), so Show affected items opens and closes instantly
# without reloading anything — which covers every group there is today.
AFFECTED_INSTANT_MAX = 50000
_AFFECTED = {}  # combo_id -> its items, for this page (filled by prefetch_affected_items)


# ===========================================================================
# Department Review: Show affected items
# ===========================================================================

@st.cache_data(ttl=600, show_spinner=False)
def load_member_items_bulk(combo_ids: tuple) -> pd.DataFrame:
    return dept_mapping.get_combo_member_items_bulk(ENGINE, list(combo_ids))


def prefetch_affected_items(groups) -> None:
    """groups: (combo_id, item count) pairs about to be shown on this page."""
    ids = tuple(sorted({int(c) for c, n in groups if n and int(n) <= AFFECTED_INSTANT_MAX and int(c) not in _AFFECTED}))
    if not ids:
        return
    df = load_member_items_bulk(ids)
    for cid, g in df.groupby("combo_id"):
        _AFFECTED[int(cid)] = g.drop(columns="combo_id")
    for cid in ids:
        _AFFECTED.setdefault(cid, df.iloc[0:0].drop(columns="combo_id"))


def _affected_items_table(items_df: pd.DataFrame) -> None:
    if items_df.empty:
        st.caption("No items currently in the item master for this group yet.")
        return
    manual_count = items_df["manually_edited_by"].notna().sum()
    if manual_count:
        label = "item has" if manual_count == 1 else "items have"
        st.caption(f"{manual_count} {label} a manual correction on file for this group.")
    display_df = items_df.rename(columns={
        "upc": "UPC", "description": "Description", "brand": "Brand",
        "pack": "Pack", "size": "Size", "uom": "UOM",
    }).drop(columns=["manually_edited_by"])
    st.dataframe(display_df, hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items_df)))


def render_affected_items_expander(combo_id: int, n_upcs_total: int, key_prefix: str, container=st) -> None:
    """A per-combo "see the actual rows this affects" drill-down — the
    aggregate count alone ("1,359 item(s)") doesn't let a person confirm
    they're really the items they think they are before approving a
    whole group at once. Collapsed by default. `container` lets a caller
    place this inside a column instead of a full row of its own."""
    label = f"Show affected items ({n_upcs_total:,})"
    if n_upcs_total <= AFFECTED_INSTANT_MAX:
        items_df = _AFFECTED.get(combo_id)
        if items_df is None:
            items_df = load_combo_member_items(combo_id)
        with container.expander(label, key=f"{key_prefix}_{combo_id}"):
            _affected_items_table(items_df)
    elif container is st:
        _affected_items(combo_id, label, key_prefix)
    else:
        with container:
            _affected_items(combo_id, label, key_prefix)


@st.fragment
def _affected_items(combo_id: int, label: str, key_prefix: str) -> None:
    # A very large group: its list is fetched only once it's opened, and
    # opening/closing redraws just this list, not the page.
    exp = st.expander(label, key=f"{key_prefix}_{combo_id}", on_change="rerun")
    with exp:
        if exp.open:
            _affected_items_table(load_combo_member_items(combo_id))


# ===========================================================================
# Department Review: staged changes, votes and suggestions
# ===========================================================================

@st.cache_data(show_spinner=False)
def load_dept_pending_changes() -> dict:
    """Staged whole-combo Approves — {combo_id: {...}} — read from the
    database, not st.session_state. Every click writes here immediately
    (durable — survives a closed tab or an app restart) and is visible to
    every editor right away — same st.cache_data(no ttl)+.clear()
    convention as load_items/load_broken_out_combos."""
    return dept_mapping.get_pending_changes(ENGINE)


@st.cache_data(show_spinner=False)
def load_dept_pending_upc_changes() -> dict:
    """Same idea as load_dept_pending_changes, but for individual per-UPC
    decisions staged on the Broken Out tab — {upc: {...}}."""
    return dept_mapping.get_pending_upc_changes(ENGINE)


@st.cache_data(show_spinner=False)
def load_combo_suggestions() -> dict:
    """Disputed combos only (2+ people proposing different departments) —
    {combo_id: [{...}, ...]}. A combo with a single, agreed-on suggestion
    never appears here; it's already a normal decided row in
    load_dept_pending_changes instead."""
    return dept_mapping.get_combo_suggestions(ENGINE)


@st.cache_data(show_spinner=False)
def load_upc_change_suggestions() -> dict:
    """Pending suggestions on already-decided Broken Out items, awaiting
    their owner's accept/deny — {upc: [{...}, ...]}. Unlike combo-level
    disputes, this is never peer-voting: only the owner (or admin) can act
    on these, see dept_mapping.accept_upc_suggestion/deny_upc_suggestion."""
    return dept_mapping.get_upc_change_suggestions(ENGINE)


@st.cache_data(show_spinner=False)
def load_broken_out_claims() -> dict:
    return dept_mapping.get_broken_out_claims(ENGINE)


def clear_dept_suggestion_caches() -> None:
    load_undo_redo.clear()
    load_combo_suggestions.clear()
    load_upc_change_suggestions.clear()
    load_dept_pending_changes.clear()
    load_dept_pending_upc_changes.clear()
    load_broken_out_claims.clear()


def render_combo_suggestion_disputes(disputed: dict) -> None:
    """One card per disputed Crosswalk/Unmatched combo, listing every
    currently-voted department and who's behind it. Resolution requires
    genuine unanimity among whoever currently has a vote on file — the
    people who actually hold the differing opinions need to change their
    OWN vote to match one another. Clicking a department here casts YOUR
    OWN vote for it (visible to everyone, added to that option's
    supporter list) — but it never forces a resolution just by adding a
    name to the side that already looks more popular; the dissenting
    vote is still sitting there until whoever holds it changes their own
    mind. If you're happy with what's already suggested, you don't need
    to click anything at all. Admins get a separate override below,
    entirely outside this voting pool."""
    if not disputed:
        return
    actor = st.session_state["name"]
    for combo_id, suggestions in disputed.items():
        first = suggestions[0]
        with st.container(border=True, key=f"card_disp_{combo_id}"):
            hc0, hc1, hc_act = st.columns(CARD_COLS)
            hc0.checkbox(
                "Save for later", value=False, key=f"dept_pending_snooze_combo_{combo_id}",
                label_visibility="collapsed",
                help="Move this to Saved for later — it's still unresolved, just out of the main Needs "
                     "agreement list until you check it again.",
            )
            hc1.markdown(f"**{(first['source_key'] or '').upper()}** — {first['label']}  \nPeople disagree — vote below")
            hc1.caption(f"{first['n_upcs_total']:,} items")
            card_actions(hc_act, f"undo_dispute_{combo_id}",
                         partial(open_undo_picker, combo_id, f"{(first['source_key'] or '').upper()} — {first['label']}"),
                         {"kind": "combo", "combo_id": combo_id, "tier": first.get("tier"),
                          "source_key": first["source_key"], "raw_label": first["label"], "n_upcs_total": first["n_upcs_total"],
                          "label": f"{(first['source_key'] or '').upper()} — {first['label']}"},
                         f"admin_override_btn_dispute_{combo_id}")
            by_dept = {}
            for s in suggestions:
                by_dept.setdefault(s["department"], []).append(s["staged_by"])
            actor_current_vote = next((d for d, sugg in by_dept.items() if actor in sugg), None)
            # One column per department instead of one row per department —
            # the same info (name, who's behind it, vote button) fits
            # side by side, which matters once there are hundreds of these
            # disputes on screen at once.
            dept_cols = st.columns(len(by_dept))
            for col, (department, suggesters) in zip(dept_cols, by_dept.items()):
                col.markdown(f"**{department}**")
                col.caption(', '.join(suggesters))
                already_voted = actor in suggesters
                if already_voted:
                    # Withdrawing your own vote is the one thing you
                    # unilaterally control here — pulling out entirely is
                    # different from changing to a different department
                    # (which the buttons on the other columns already do).
                    if col.button("Withdraw my vote", key=f"withdraw_combo_{combo_id}_{department}", width='stretch'):
                        with track(combo_id, f"{(first['source_key'] or '').upper()} — {first['label']}", "Withdrew your vote"):
                            result = dept_mapping.withdraw_combo_suggestion(ENGINE, combo_id, actor)
                        clear_dept_suggestion_caches()
                        if result["resolved"]:
                            st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {result['departments'][0]}.", "")
                        elif not result["departments"]:
                            st.session_state["_toast"] = (f"Withdrew your vote on **{first['label']}** — no suggestions left.", "")
                        else:
                            st.session_state["_toast"] = (f"Withdrew your vote on **{first['label']}**.", "")
                        st.rerun()
                else:
                    label = "Change vote" if actor_current_vote else "I also think this"
                    if col.button(label, key=f"agree_combo_{combo_id}_{department}", width='stretch'):
                        with track(combo_id, f"{(first['source_key'] or '').upper()} — {first['label']}", f"Voted {department}"):
                            result = dept_mapping.upsert_combo_suggestion(
                                ENGINE, combo_id, first.get("tier"), department, first["source_key"],
                                first["label"], first["n_upcs_total"], actor,
                            )
                        clear_dept_suggestion_caches()
                        if not result["disputed"]:
                            st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {department}.", "")
                        st.rerun()
            if not actor_current_vote:
                nc1, nc2 = st.columns([3, 1.4])
                new_dept_options = sorted(d for d in load_departments()["department"].tolist() if d not in by_dept)
                new_dept = nc1.selectbox(
                    "Suggest a different department", new_dept_options, index=None,
                    placeholder="Suggest Department", key=f"combo_dispute_new_{combo_id}", label_visibility="collapsed",
                )
                if nc2.button("Add suggestion", key=f"combo_dispute_new_btn_{combo_id}", width='stretch', disabled=not new_dept):
                    with track(combo_id, f"{(first['source_key'] or '').upper()} — {first['label']}", f"Suggested {new_dept}"):
                        result = dept_mapping.upsert_combo_suggestion(
                            ENGINE, combo_id, first.get("tier"), new_dept, first["source_key"],
                            first["label"], first["n_upcs_total"], actor,
                        )
                    clear_dept_suggestion_caches()
                    if result.get("blocked"):
                        st.session_state["_toast"] = (
                            f"**{first['label']}** already has {dept_mapping.MAX_DISTINCT_SUGGESTIONS} different "
                            f"suggestions ({', '.join(result['departments'])}) — pick one of those instead, or "
                            "ask an admin to override.",
                            "",
                        )
                    elif not result["disputed"]:
                        st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {new_dept}.", "")
                    st.rerun()
            render_affected_items_expander(combo_id, first["n_upcs_total"], "dispute_items")


def render_upc_change_suggestions(suggestions_by_upc: dict, pending_upc_changes: dict, combo_id) -> None:
    """Ownership model for Broken Out: unlike Crosswalk/Unmatched, a UPC
    has one owner (whoever decided it), not a voting pool — someone else's
    differing pick sits here as a suggestion the owner explicitly accepts
    or denies, never something a third party's vote can force through.
    Grouped by (owner, suggested_by, department) for the header/bulk
    action, since a Broken Out group can have hundreds of individually-
    suggested items and bulk-accepting an identical proposal across all
    of them at once matters at that scale — but buttons are strictly
    gated by what the actual viewer is allowed to do (never a shared
    control everyone sees, which misleadingly let the suggester "select"
    Accept even though the backend would reject it), and the item(s)
    itself is always shown, not hidden behind a click: inline for a
    small group (1 item shows its own identity right in the header; 2-5
    get per-item Accept/Deny/Withdraw rows so a small group never forces
    an all-or-nothing call), collapsed into an expander once a group is
    too large for that to stay readable (>5) — there, bulk Accept
    all/Deny all is the primary action for the common case of agreeing
    (or not) with the whole batch, and a checkbox column inside the
    expander lets whoever's actually allowed to decide pick out
    exceptions without leaving this card."""
    if not suggestions_by_upc:
        return
    actor = st.session_state["name"]
    groups = {}
    for upc, suggs in suggestions_by_upc.items():
        owner = pending_upc_changes.get(upc, {}).get("staged_by")
        for s in suggs:
            key = (owner, s["suggested_by"], s["department"])
            groups.setdefault(key, []).append({**s, "upc": upc})
    st.caption(f"{len(suggestions_by_upc)} item(s) have a suggested change awaiting their owner's decision.")

    INLINE_LIMIT = 5  # groups this size or smaller show every item + per-item buttons instead of a bulk-only card

    first_s = next(iter(suggestions_by_upc.values()))[0]
    grp_label = f"{(first_s.get('source_key') or '').upper()} — {first_s['label']}"

    def _accept(upc, suggested_by, department):
        with track(combo_id, grp_label, f"Accepted {suggested_by}'s suggestion ({department})"):
            dept_mapping.accept_upc_suggestion(ENGINE, upc, suggested_by, department, actor)

    def _deny(upc, suggested_by):
        verb = "Withdrew your suggestion" if suggested_by == actor else f"Denied {suggested_by}'s suggestion"
        with track(combo_id, grp_label, verb):
            dept_mapping.deny_upc_suggestion(ENGINE, upc, suggested_by)

    for (owner, suggested_by, department), items in groups.items():
        can_act = is_admin or actor == owner
        can_withdraw = actor == suggested_by
        key_base = f"{combo_id}_{hash((owner, suggested_by, department))}"
        with st.container(border=True):
            gc1, gc2, gc3 = st.columns([2.6, 1.1, 1.1])
            if len(items) == 1:
                it = items[0]
                currently = pending_upc_changes.get(it["upc"], {}).get("department")
                gc1.markdown(
                    f"**{it['upc']} — {it.get('description') or 'no description'}** "
                    f"(currently {currently or 'unknown'}) owned by **{owner or 'unknown'}** → "
                    f"suggested **{department}** by **{suggested_by}**"
                )
            else:
                gc1.markdown(f"**{len(items)} item(s)** owned by **{owner or 'unknown'}** → suggested **{department}** by **{suggested_by}**")
            accept_label = "Accept" if len(items) == 1 else "Accept all"
            deny_label = "Deny" if len(items) == 1 else "Deny all"
            withdraw_label = "Withdraw" if len(items) == 1 else "Withdraw all"
            if can_act:
                if gc2.button(accept_label, key=f"accept_upc_group_{key_base}", width='stretch'):
                    for it in items:
                        _accept(it["upc"], suggested_by, department)
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Accepted: {len(items)} item(s) → {department}.", "")
                    st.rerun()
                if gc3.button(deny_label, key=f"deny_upc_group_{key_base}", width='stretch'):
                    for it in items:
                        _deny(it["upc"], suggested_by)
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Denied: {len(items)} suggestion(s).", "")
                    st.rerun()
            elif can_withdraw:
                if gc2.button(withdraw_label, key=f"withdraw_upc_group_{key_base}", width='stretch'):
                    for it in items:
                        _deny(it["upc"], suggested_by)
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Withdrew {len(items)} suggestion(s).", "")
                    st.rerun()
            else:
                gc2.caption(f"Only {owner or 'the owner'} or an admin can accept/deny this.")

            if len(items) > 1 and len(items) <= INLINE_LIMIT:
                for it in items:
                    ic1, ic2, ic3 = st.columns([2.6, 1.1, 1.1])
                    currently = pending_upc_changes.get(it["upc"], {}).get("department")
                    ic1.caption(f"{it['upc']} — {it.get('description') or 'no description'} (currently {currently or 'unknown'})")
                    if can_act:
                        if ic2.button("Accept", key=f"accept_upc_item_{key_base}_{it['upc']}", width='stretch'):
                            _accept(it["upc"], suggested_by, department)
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Accepted: {it['upc']} → {department}.", "")
                            st.rerun()
                        if ic3.button("Deny", key=f"deny_upc_item_{key_base}_{it['upc']}", width='stretch'):
                            _deny(it["upc"], suggested_by)
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Denied suggestion for {it['upc']}.", "")
                            st.rerun()
                    elif can_withdraw:
                        if ic2.button("Withdraw", key=f"withdraw_upc_item_{key_base}_{it['upc']}", width='stretch'):
                            _deny(it["upc"], suggested_by)
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Withdrew suggestion for {it['upc']}.", "")
                            st.rerun()
            elif len(items) > INLINE_LIMIT:
                with st.expander(f"Show item(s) ({len(items)})", key=f"upc_sugg_items_{key_base}"):
                    if can_act or can_withdraw:
                        st.caption(
                            "Bulk buttons above act on all of these. To keep most and handle a few "
                            "differently, check just the exception(s) below and use the button under the table."
                        )
                        edited = st.data_editor(
                            pd.DataFrame([
                                {
                                    "Pick": False, "UPC": it["upc"], "Description": it.get("description"),
                                    "Currently": pending_upc_changes.get(it["upc"], {}).get("department"),
                                }
                                for it in items
                            ]),
                            hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items)),
                            key=f"upc_sugg_picker_{key_base}",
                            disabled=["UPC", "Description", "Currently"],
                            column_config={"Pick": st.column_config.CheckboxColumn()},
                        )
                        picked = [it for it, keep in zip(items, edited["Pick"]) if keep]
                        pc1, pc2 = st.columns(2)
                        if can_act:
                            if pc1.button(f"Accept picked ({len(picked)})", key=f"accept_upc_picked_{key_base}", width='stretch', disabled=not picked):
                                for it in picked:
                                    _accept(it["upc"], suggested_by, department)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Accepted: {len(picked)} item(s) → {department}.", "")
                                st.rerun()
                            if pc2.button(f"Deny picked ({len(picked)})", key=f"deny_upc_picked_{key_base}", width='stretch', disabled=not picked):
                                for it in picked:
                                    _deny(it["upc"], suggested_by)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Denied: {len(picked)} suggestion(s).", "")
                                st.rerun()
                        elif can_withdraw:
                            if pc1.button(f"Withdraw picked ({len(picked)})", key=f"withdraw_upc_picked_{key_base}", width='stretch', disabled=not picked):
                                for it in picked:
                                    _deny(it["upc"], suggested_by)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Withdrew {len(picked)} suggestion(s).", "")
                                st.rerun()
                    else:
                        st.dataframe(
                            pd.DataFrame([
                                {"UPC": it["upc"], "Description": it.get("description"), "Currently": pending_upc_changes.get(it["upc"], {}).get("department")}
                                for it in items
                            ]),
                            hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items)),
                        )


# ===========================================================================
# Department Review: recent moves, where a change came from
# ===========================================================================

@st.cache_data(show_spinner=False)
def load_dept_recent_moves() -> list:
    """The shared undo trail for immediate actions (Break Out / Send
    Back) — a real per-combo STACK (see dept_mapping.get_recent_moves),
    not one dedup'd slot per combo. Confirmed as a real, previously-
    shipped bug: deduping meant Send Back (snapshot: partly-decided
    Broken Out) -> Break Out again (snapshot: not_reviewed) overwrote the
    first entry the instant the second recorded, so the only undo point
    left restored to not_reviewed — the partly-decided state was gone
    with no trace. Now every immediate action pushes its own row;
    undoing the most recent leaves any earlier entry for that same combo
    still there, so repeatedly clicking Undo walks all the way back to
    wherever the combo started, one real step at a time. Persisted in
    the database (not st.session_state) so this survives a closed
    browser tab and is visible to every editor, not just whoever clicked."""
    return dept_mapping.get_recent_moves(ENGINE)


def record_recent_move(combo_id: int, source_key: str, label: str, n_upcs_total: int, description: str, snapshot: dict) -> None:
    dept_mapping.record_recent_move(
        ENGINE, combo_id, source_key, label, n_upcs_total, description, snapshot, st.session_state["name"],
    )
    load_dept_recent_moves.clear()
    clear_dept_suggestion_caches()  # a round trip can bring staged work back


def clear_pending_for_combo(combo_id: int) -> None:
    """A department decision staged for a combo (Approve, or per-item
    Department choices from Broken Out) is only ever valid given the
    state that produced it — an immediate action that changes that state
    (Break Out, Send Back, or undoing one of them) must discard anything
    staged on top of it, or a later push could silently apply a decision
    to a combo that's no longer in the state it was staged for. Confirmed
    as a real, reproducible bug: Send Back to Crosswalk -> Approve ->
    Undo the Send Back left the stale Approve sitting in Pending Changes,
    still pointing at a combo that was, again, sitting in Broken Out —
    and separately, staging every item decision on a Broken Out combo
    then Sending it back left all those item decisions still staged too.
    Clears any staged decision for this combo regardless of whose it is
    or whether it was saved yet — a not-yet-saved Approve is just as
    stale as a saved one the moment the combo's state moves out from
    under it."""
    dept_mapping.clear_pending_for_combo(ENGINE, combo_id)
    load_dept_pending_changes.clear()
    load_dept_pending_upc_changes.clear()


@st.cache_data(ttl=30, show_spinner=False)
def load_group_facts() -> dict:
    return dept_mapping.group_facts(ENGINE)


@st.cache_data(ttl=30, show_spinner=False)
def load_pending_overrides_by_group() -> pd.DataFrame:
    return dept_mapping.pending_overrides_by_group(ENGINE)


@st.cache_data(ttl=60, show_spinner=False)
def load_noted_item_counts() -> dict:
    return dept_mapping.noted_item_counts(ENGINE)


MOVE_WORDS = {"Broken Out to UPC-Level": "Moved to Broken Out", "Sent back for review": "Sent back for review",
              "Send Back to Broken Out": "Moved back to Broken Out", "Send Back to Crosswalk": "Sent back to Crosswalk",
              "Send Back to Unmatched": "Sent back to Unmatched"}

NOTE_WORDS = {
    "approved group": "approved the whole group",
    "manually overridden group": "Department changed by hand",
    "manual override": "Department changed by hand",
    "Manually Reviewed": "item decided by hand",
    "Manually Approved": "item decided by hand",
    "Break Out to UPC-Level": "Break Out to UPC-Level",
    "Send to Broken Out": "send to Broken Out",
    "Return to Crosswalk/Unmatched": "return for review",
}


def short_note(note: str | None) -> str:
    """ "📥 One-time old-workbook import (file.xlsx) — Old Crosswalk: approved group"
    -> "Old workbook (Crosswalk sheet): approved the whole group"."""
    if not note:
        return ""
    if note.startswith("📥 One-time old-workbook import"):
        why = note.split(" — ", 1)[-1].split(" · group ")[0]
        if why.startswith("Old ") and ": " in why:
            sheet, what = why[4:].split(": ", 1)
            return f"Old workbook ({sheet} sheet): {NOTE_WORDS.get(what, what)}"
        return "Old workbook: " + why
    return note


def group_line(combo_id: int, change: dict | None = None, note: str | None = None) -> tuple:
    """One short gray line for a staged group card, plus the longer
    listed-twice detail (shown as the line's tooltip)."""
    f = load_group_facts().get(combo_id) or {}
    bits = [f"{int(f.get('n_upcs_total') or 0):,} items"] if f else []
    if change:
        when = local_time(change.get("staged_at"))
        who = f"Staged by {change.get('staged_by') or 'unknown'}" + (f", {when}" if when else "")
        if change.get("overridden_by"):
            who += f" (admin override by {change['overridden_by']})"
        elif change.get("agreed_by"):
            who += f" (agreed by {change['agreed_by']})"
        bits.append(who)
    state, dept = f.get("decision_state"), f.get("decided_department")
    if state == "decided":
        bits.append(f"now decided as {dept}")
    elif state == "decided_broken_out":
        bits.append("finished item by item" + (" — this replaces its item decisions" if change else ""))
    elif state == "broken_out":
        bits.append("in Broken Out")
    if note:
        bits.append(short_note(note))
    if combo_id in open_choice_groups():
        bits.append("see Needs your choice")
    twice = load_import_notes().get(combo_id)
    if twice:
        bits.append("listed twice in the old workbook")
    return " · ".join(bits), twice


def group_origin_lines(combo_id: int, note: str | None = None) -> list:
    """Two short lines for a staged change: where its group is in the app
    right now, and — when recorded — what the old workbook said about it."""
    f = load_group_facts().get(combo_id)
    lines = []
    if f:
        state, dept = f["decision_state"], f["decided_department"]
        if state == "decided":
            now = f"decided as {dept}"
        elif state == "decided_broken_out":
            now = "finished item by item" + (" · this group decision replaces its item decisions" if note else "")
        elif state == "broken_out":
            now = "being decided item by item"
        elif dept:
            now = f"auto-decided as {dept}"
        else:
            now = "not decided yet"
        lines.append(f"In the app now: **{f['where']}** · {now} · {int(f['n_upcs_total'] or 0):,} items")
    if note:
        lines.append(short_note(note))
    if combo_id in open_choice_groups():
        lines.append("The old workbook and the app disagree about this group — choose under **Needs your choice** below")
    twice = load_import_notes().get(combo_id)
    if twice:
        lines.append(f"Old workbook note: {twice}")
    return lines


def local_time(v, fmt: str = "%b %d %I:%M %p") -> str:
    t = pd.to_datetime(v, errors="coerce")
    return "" if pd.isna(t) else t.tz_localize("UTC").tz_convert("America/Los_Angeles").strftime(fmt)


@st.cache_data(show_spinner=False)
def load_import_choices(status: str | None = "open") -> list:
    return dept_mapping.list_import_choices(ENGINE, status)


def reopen_undone_choices() -> None:
    """A picked option that's no longer staged (someone undid it) puts its
    question back, so nothing gets pushed around it."""
    changed = False
    for c in load_import_choices(None):
        if c["status"] != "switched":
            continue
        o = next((o for o in json.loads(c.get("options_json") or "[]") if o["text"] == (c.get("alternative") or "")), None)
        if o and not option_staged(int(c["combo_id"]), o):
            dept_mapping.reopen_import_choice(ENGINE, int(c["choice_id"]))
            changed = True
    if changed:
        load_import_choices.clear()


def option_staged(combo_id: int, o: dict) -> bool:
    is_ = o.get("is") or {}
    if o.get("items"):
        upcs = {i[0] for i in o["items"]}
        return any(u in upcs for u in load_dept_pending_upc_changes())
    pend = (load_dept_pending_changes().get(combo_id) or {}).get("department")
    state = dept_mapping.get_combo_snapshot(ENGINE, combo_id).get("combo", {}).get("decision_state")
    if "pending" in is_:
        return bool(pend) and pend.upper() == is_["pending"].upper()
    if "state" in is_:
        return state == is_["state"]
    if "not_state" in is_:
        return state != is_["not_state"]
    return True


@st.cache_data(show_spinner=False)
def load_import_notes() -> dict:
    return dept_mapping.import_notes(ENGINE)


def open_choice_groups() -> set:
    return {c["combo_id"] for c in load_import_choices()}


def render_import_choices() -> None:
    """The old workbook and the app disagree about a group the import
    touched: the import staged nothing on it — here a person picks which
    is right, and that option is staged. Push waits for these."""
    reopen_undone_choices()
    open_ = load_import_choices()
    closed = load_import_choices(None)
    closed = [c for c in closed if c["status"] in ("kept", "switched")]
    if not open_ and not closed:
        return
    if open_:
        st.markdown(f"##### Needs your choice ({len(open_)})")
        st.caption("The old workbook and the app don't agree about these groups. Nothing is staged for them until "
                   "you pick one, and Push waits until they're all answered. Ask again takes a choice back.")
    actor = st.session_state["name"]
    for c in open_:
        cid_key = int(c["choice_id"])
        options = json.loads(c.get("options_json") or "[]")
        if not options:
            continue
        n = int((load_group_facts().get(int(c["combo_id"])) or {}).get("n_upcs_total") or 0)
        with st.container(border=True):
            st.markdown(f"**{c['label']}** · {n:,} items")
            st.caption(f"{c['workbook_says']} {c['app_has']}")
            cols = st.columns(len(options))
            for i, (col, o) in enumerate(zip(cols, options)):
                with col.container(border=True):
                    st.markdown("**Old workbook**" + (f"  \n:gray[{o['from']}]" if o.get("from") else "")
                                if o["who"] == "workbook" else "**App**  \n:gray[as it is now]")
                    st.write(o["text"])
                    if o.get("stages"):
                        st.caption(o["stages"])
                    if st.button("Use this", key=f"choice_use_{cid_key}_{i}", width="stretch"):
                        if o["who"] == "app" and not o.get("goto"):
                            dept_mapping.close_import_choice(ENGINE, cid_key, "kept", actor, picked=o["text"])
                            load_import_choices.clear()
                            st.session_state["_toast"] = (f"Kept as it is: **{c['label']}**", "")
                            st.rerun()
                        apply_import_choice({**c, "alt_action": o["goto"], "alternative": o["text"], "items": o.get("items")})
    if closed:
        with st.expander(f"Choices already made ({len(closed)})", key="import_choices_closed"):
            for c in closed:
                x1, x2 = st.columns([4, 1], vertical_alignment="center")
                picked = c.get("alternative") or c["applied"]
                x1.markdown(f"**{c['label']}**  \n:gray[Chose: {picked} · {c['decided_by']} {local_time(c['decided_at'])}]")
                if x2.button("Ask again", key=f"choice_reopen_{c['choice_id']}", width="stretch",
                             help="Takes back what this choice staged and puts the question back."):
                    take_back_import_choice(c)
                    st.rerun()


def option_in_effect(combo_id: int, options: list) -> int:
    """Which option is really in effect right now (after an undo, or a
    change made by hand, too): the first whose "is" matches; the app's own
    option when none of the workbook's do."""
    pend = (load_dept_pending_changes().get(combo_id) or {}).get("department")
    state = dept_mapping.get_combo_snapshot(ENGINE, combo_id).get("combo", {}).get("decision_state")
    for i, o in enumerate(options):
        is_ = o.get("is") or {}
        if (("pending" in is_ and pend and pend.upper() == is_["pending"].upper())
                or ("no_pending" in is_ and not pend)
                or ("state" in is_ and state == is_["state"] and not ("pending" in is_))
                or ("not_state" in is_ and state != is_["not_state"])):
            return i
    return next((i for i, o in enumerate(options) if o["who"] == "app"), len(options) - 1)


def take_back_import_choice(c: dict) -> None:
    """Ask again: undoes what the picked option staged (a move it made, its
    group decision, its item decisions), so the question is open with
    nothing staged again."""
    cid, actor = int(c["combo_id"]), st.session_state["name"]
    picked = c.get("alternative") or ""
    o = next((o for o in json.loads(c.get("options_json") or "[]") if o["text"] == picked), None)
    if c["status"] == "switched" and o:
        steps = (o.get("goto") or "").split("+")
        with track(cid, c["label"], "Took back a workbook-vs-app choice"):
            if any(s.startswith(("send_back", "break_out")) for s in steps) and dept_mapping.get_combo_undo_path(ENGINE, cid).get("moves"):
                dept_mapping.undo_combo_to_stage(ENGINE, cid, 1, actor)
            if any(s.startswith("stage_group:") or s.startswith("send_back_approve:") for s in steps):
                clear_pending_for_combo(cid)
            if o.get("items"):
                dept_mapping.delete_pending_upc_changes(ENGINE, [i[0] for i in o["items"]])
    dept_mapping.reopen_import_choice(ENGINE, int(c["choice_id"]))
    load_import_choices.clear()
    load_dept_recent_moves.clear()
    clear_dept_suggestion_caches()
    clear_dept_review_caches()


def apply_import_choice(c: dict) -> None:
    """Stages the picked option for one choice, through the same steps (and
    the same Undo) as doing it by hand."""
    cid, actor = int(c["combo_id"]), st.session_state["name"]
    f = load_group_facts().get(cid) or {}
    label = c["label"]
    action = c["alt_action"] or ""
    note = f"{old_workbook_import.NOTE_PREFIX} ({c['file_name'] or 'workbook'}) — chosen over the app: {c['alternative']}"[:400]
    with track(cid, label, f"Workbook vs app: {c['alternative']}"):
        for step in [x for x in action.split("+") if x and x != "keep"]:
            if step.startswith("send_back_approve:") or step == "send_back":
                snapshot = dept_mapping.get_combo_snapshot(ENGINE, cid)
                dept_mapping.revert_broken_out_combo(ENGINE, cid, actor)
                clear_pending_for_combo(cid)
                queue = "Crosswalk" if (f.get("queue") or "Crosswalk") == "Crosswalk" else "Unmatched"
                dept_mapping.record_recent_move(ENGINE, cid, f.get("source_key") or "", label.split(" — ", 1)[-1],
                                                int(f.get("n_upcs_total") or 0), f"Send Back to {queue}", snapshot, actor,
                                                origin_note=note)
                if step.startswith("send_back_approve:"):
                    step = "stage_group:" + step.split(":", 1)[1]
            if step.startswith("stage_group:"):
                dept = step.split(":", 1)[1]
                tier = "unmatched" if (f.get("queue") or "") == "Unmatched" else "review"
                clear_pending_for_combo(cid)
                dept_mapping.upsert_combo_suggestion(ENGINE, cid, tier, dept, f.get("source_key") or "",
                                                     label.split(" — ", 1)[-1], int(f.get("n_upcs_total") or 0), actor)
                dept_mapping.set_pending_note(ENGINE, cid, note)
            elif step == "stage_items":
                its = c.get("items") or []
                src = (f.get("source_key") or "").lower()
                changes = {u: {"department": d, "combo_id": cid, "label": label.split(" — ", 1)[-1], "description": desc,
                               "source_key": src, "origin_note": f"{old_workbook_import.NOTE_PREFIX} ({c['file_name'] or 'workbook'}) — {frm}"[:400]}
                           for u, d, desc, _g, frm in its}
                if changes:
                    dept_mapping.stage_broken_out_decisions(ENGINE, changes, actor, is_admin=is_admin)
            elif step == "undo_move":
                if dept_mapping.get_combo_undo_path(ENGINE, cid).get("moves"):
                    dept_mapping.undo_combo_to_stage(ENGINE, cid, 1, actor)
            elif step == "drop_group":
                clear_pending_for_combo(cid)
            elif step == "break_out":
                snapshot = dept_mapping.get_combo_snapshot(ENGINE, cid)
                dept_mapping.break_out_combo(ENGINE, cid, actor)
                dept_mapping.record_recent_move(ENGINE, cid, f.get("source_key") or "", label.split(" — ", 1)[-1],
                                                int(f.get("n_upcs_total") or 0), "Broken Out to UPC-Level", snapshot, actor,
                                                origin_note=note)
    dept_mapping.close_import_choice(ENGINE, int(c["choice_id"]), "switched", actor, picked=c["alternative"])
    load_import_choices.clear()
    load_dept_recent_moves.clear()
    clear_dept_suggestion_caches()
    clear_dept_review_caches()
    st.session_state["_toast"] = (f"Staged: **{label}** — {c['alternative']}", "")
    st.rerun()


def clear_dept_review_caches() -> None:
    load_group_facts.clear()
    load_pending_overrides_by_group.clear()
    load_noted_item_counts.clear()
    load_dept_review_queue.clear()
    load_broken_out_combos.clear()
    load_pending_upc_overrides.clear()
    load_combo_upc_decisions.clear()
    load_decided_combos.clear()


# ===========================================================================
# Item Master changes: push, bulk uploads, monthly refresh
# ===========================================================================

@st.cache_data(show_spinner=False)
def load_item_master_pending() -> dict:
    """Staged Add/Delete/Edit Item actions — {upc: {...}} — visible to
    every editor the moment they're staged, same as load_dept_pending_changes."""
    return dept_mapping.get_item_master_pending(ENGINE)


def push_item_master_add(upc: str, change: dict, actor: str) -> None:
    with db_begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dbo.items (upc, description, department, category, subcategory, brand, pack, size, uom) "
                "VALUES (:upc, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom)"
            ),
            {
                "upc": upc, "description": change["description"], "department": change["department"],
                "category": change["category"], "subcategory": change["subcategory"], "brand": change["brand"],
                "pack": change.get("pack"), "size": change.get("size"), "uom": change.get("uom"),
            },
        )
        # This item has no source data of its own, so it must live in
        # manual_overrides too, or the next Merge (which rebuilds
        # dbo.items from raw_items) would drop it entirely.
        conn.execute(
            text(
                """
                MERGE dbo.manual_overrides AS target
                USING (SELECT :upc AS upc) AS src ON target.upc = src.upc
                WHEN MATCHED THEN UPDATE SET
                    description = :description, department = :department,
                    category = :category, subcategory = :subcategory,
                    brand = :brand, pack = :pack, size = :size, uom = :uom,
                    updated_by = :updated_by, updated_at = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN INSERT
                    (upc, description, department, category, subcategory, brand, pack, size, uom, updated_by)
                VALUES
                    (:upc, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom, :updated_by);
                """
            ),
            {
                "upc": upc, "description": change["description"], "department": change["department"],
                "category": change["category"], "subcategory": change["subcategory"], "brand": change["brand"],
                "pack": change.get("pack"), "size": change.get("size"), "uom": change.get("uom"), "updated_by": actor,
            },
        )
        # Un-delete: adding a UPC back should cancel out a previous manual
        # deletion of the same UPC.
        conn.execute(text("DELETE FROM dbo.deleted_upcs WHERE upc = :upc"), {"upc": upc})


def push_item_master_delete(upc: str, change: dict, actor: str) -> None:
    with db_begin() as conn:
        conn.execute(text("DELETE FROM dbo.items WHERE upc = :upc"), {"upc": upc})
        conn.execute(text("DELETE FROM dbo.manual_overrides WHERE upc = :upc"), {"upc": upc})
        conn.execute(
            text(
                """
                MERGE dbo.deleted_upcs AS target
                USING (SELECT :upc AS upc) AS src ON target.upc = src.upc
                WHEN MATCHED THEN UPDATE SET
                    description = :description, department = :department,
                    category = :category, subcategory = :subcategory, brand = :brand,
                    pack = :pack, size = :size, uom = :uom,
                    deleted_by = :deleted_by, deleted_at = SYSUTCDATETIME()
                WHEN NOT MATCHED THEN INSERT
                    (upc, description, department, category, subcategory, brand, pack, size, uom, deleted_by)
                VALUES
                    (:upc, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom, :deleted_by);
                """
            ),
            {
                "upc": upc, "description": change["description"], "department": change["department"],
                "category": change["category"], "subcategory": change["subcategory"], "brand": change["brand"],
                "pack": change.get("pack"), "size": change.get("size"), "uom": change.get("uom"), "deleted_by": actor,
            },
        )


def render_monthly_refresh() -> None:
    """All of this month's files at once: each is matched to its source by
    File Keyword, cleaned with that source's settings, checked against its
    last upload, and ingested together, then one Merge draft is computed.
    The same code runs unattended from scripts/monthly_refresh.py."""
    with st.expander("Monthly refresh — upload all of this month's files at once",
                     expanded=bool(st.session_state.get("_mr_reports"))):
        st.caption(
            "Drop all of this month's files — each is matched to its source, checked, then a Merge draft is computed.",
            help=("Drop every distributor file for the month. Each is matched to its source by the source's File Keyword "
            "(Sources tab), read with that source's cleaning rules, and compared with its last upload — a file much "
            "smaller than last time is held back until you tick it. Then one Merge draft is computed with every "
            "Department Review decision applied; review and push it on the Merge tab. For hosting, "
            "`scripts/monthly_refresh.py` does the same from an inbox folder on a schedule."),
        )
        ver = st.session_state.get("_mr_ver", 0)
        files = st.file_uploader("This month's files", type=["xlsx", "xls", "xlsb", "csv"], accept_multiple_files=True,
                                 key=f"mr_files_{ver}")
        if not files:
            st.session_state.pop("_mr_reports", None)
            return
        sources = monthly_refresh.load_sources(ENGINE)
        cache = st.session_state.setdefault("_mr_reports", {})
        for f in files:
            k = (f.name, f.size)
            if k not in cache:
                with st.spinner(f"Reading {f.name}..."):
                    cache[k] = monthly_refresh.check_file(ENGINE, f, sources)
        reps = [cache[(f.name, f.size)] for f in files]
        keys = [r["source_key"] for r in reps if r.get("source_key")]
        dup = {k for k in keys if keys.count(k) > 1}
        label = {"ready": "Ready", "suspicious": "Much smaller than last time", "error": "Can't read",
                 "skipped": "No matching source"}
        st.dataframe(pd.DataFrame([{
            "File": r["file"], "Source": r.get("source_key") or "—",
            "Rows": r.get("rows"), "Rows last time": r.get("previous_rows"),
            "Status": ("Two files for this source" if r.get("source_key") in dup else label.get(r["status"], r["status"])),
            "Why": r["note"],
        } for r in reps]), hide_index=True, width='stretch')
        take = [r for r in reps if r["status"] == "ready" and r.get("source_key") not in dup]
        sus = [r for r in reps if r["status"] == "suspicious" and r.get("source_key") not in dup]
        for r in sus:
            if st.checkbox(f"Ingest {r['file']} anyway ({r['note']})", key=f"mr_ok_{ver}_{r['file']}"):
                take.append(r)
        missing = sorted(set(sources[sources["enabled"] == True]["source_key"]) - {r.get("source_key") for r in take})  # noqa: E712
        if missing:
            st.caption(f"Not refreshed this time (keeps last month's data): {', '.join(missing)}.")
        if st.button(f"Ingest {len(take)} file(s) and compute the Merge draft", type="primary", disabled=not take,
                     key=f"mr_go_{ver}"):
            actor = st.session_state["name"]
            prog = st.progress(0.0, text="Ingesting...")
            for i, r in enumerate(take):
                prog.progress(i / (len(take) + 1), text=f"Ingesting {r['file']} into {r['source_key']}...")
                monthly_refresh.ingest_checked(ENGINE, r, actor)
            prog.progress(len(take) / (len(take) + 1), text="Computing the Merge draft...")
            st.session_state["_upload_auto_merge_result"] = monthly_refresh.compute_draft(ENGINE, actor)
            prog.empty()
            for fn in (load_raw_item_counts, load_ingestion_log, load_stale_sources, load_stale_sources_since_compute):
                fn.clear()
            clear_merge_compute_caches()
            st.session_state.pop("_mr_reports", None)
            st.session_state["_mr_ver"] = ver + 1
            st.session_state["_toast"] = (f"Ingested {len(take)} file(s). The Merge draft is ready on the Merge tab.", "")
            st.rerun()


@st.cache_data(show_spinner=False)
def bulk_template(kind: str, departments: tuple) -> bytes:
    return item_bulk.template_bytes(kind, list(departments))


def render_bulk_upload(kind: str, title: str) -> None:
    """Many items at once: download a blank template, fill it in (paste
    from any spreadsheet), upload it, check the preview, stage it all in
    one click. Staged changes go to Pending Changes like any single one."""
    ver_key = f"_bulk_{kind}_ver"
    with st.expander(f"{title} — upload a spreadsheet", expanded=st.session_state.get(f"_bulk_{kind}_open", False)):
        st.caption(item_bulk.INSTRUCTIONS[kind] + " Everything uploaded is staged to Pending Changes, not applied straight away.")
        departments = tuple(load_departments()["department"].tolist())
        st.download_button(
            "Download the blank template", bulk_template(kind, departments),
            file_name={"add": "add_items_template.xlsx", "delete": "delete_items_template.xlsx",
                       "edit": "upc_overrides_template.xlsx"}[kind],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"bulk_{kind}_template",
        )
        up = st.file_uploader(
            "Upload the filled-in template (.xlsx or .csv)", type=["xlsx", "csv"],
            key=f"bulk_{kind}_file_{st.session_state.get(ver_key, 0)}",
        )
        if up is None:
            return
        st.session_state[f"_bulk_{kind}_open"] = True
        try:
            df = item_bulk.read_upload(up)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            return
        if df.empty:
            st.warning("That file has no rows filled in.")
            return
        live_df = load_items()
        live = {
            r["UPC"]: {f: r[ITEM_MASTER_FIELD_TO_DF_COLUMN[f]] for f in item_bulk.FIELDS} | {"source_key": r["SourceKey"]}
            for r in live_df[live_df["UPC"].isin({clean_upc(u) for u in df.get("upc", [])})].to_dict("records")
        }
        actor = st.session_state["name"]
        broken_out = dept_mapping.broken_out_items(ENGINE, list(live)) if kind == "edit" else {}
        preview, changes, decisions = item_bulk.check_upload(kind, df, live, list(departments),
                                                             load_item_master_pending(), actor, broken_out)
        ready = preview["Status"].str.startswith("Ready")
        unchanged = preview["Status"] == "No change"
        problems = ~(ready | unchanged)
        routed = ""
        if kind == "edit" and len(preview) and "Goes to" in preview:
            n_dec = int(preview["Goes to"].str.contains("Broken Out").sum())
            n_ov = int(preview["Goes to"].str.contains("UPC override").sum())
            routed = f" ({n_ov:,} UPC override(s), {n_dec:,} Broken Out item decision(s))"
        st.markdown(
            f"**{int(ready.sum()):,} ready**{routed} · {int(unchanged.sum()):,} no change · "
            + (f":red[**{int(problems.sum()):,} need fixing**]" if problems.any() else "0 need fixing")
        )
        only_problems = problems.any() and st.toggle("Show only rows that need fixing", key=f"bulk_{kind}_only_bad")
        st.dataframe(
            preview[problems] if only_problems else preview, hide_index=True, width='stretch',
            height=min(420, 38 + 35 * len(preview)),
        )
        if problems.any():
            st.caption("Rows that need fixing are skipped — fix them in your file and upload it again, or stage the ready ones now.")
        verb = {"add": "adding", "delete": "deleting", "edit": "changing"}[kind]
        n_items = len(set(changes) | set(decisions))
        if st.button(f"Stage {verb} {n_items:,} item(s)", type="primary", disabled=not n_items, key=f"bulk_{kind}_stage"):
            n_dec = n_sugg = 0
            for cid, grp in pd.DataFrame([{"upc": u, **d} for u, d in decisions.items()]).groupby("combo_id") if decisions else []:
                cid = int(cid)
                glabel = f"{(grp['source_key'].iloc[0] or '').upper()} — {grp['label'].iloc[0]}"
                group_changes = {r["upc"]: {k: r[k] for k in ("department", "combo_id", "label", "description", "source_key")}
                                 | {"combo_id": cid} for r in grp.to_dict("records")}
                with track(cid, glabel, f"Set {len(group_changes)} item(s) from {up.name}"):
                    res = dept_mapping.stage_broken_out_decisions(ENGINE, group_changes, actor, is_admin=is_admin)
                n_dec += sum(1 for r in res.values() if r["status"] == "decided")
                n_sugg += sum(1 for r in res.values() if r["status"] == "suggested")
            with dept_mapping.activity_via("Spreadsheet upload"):
                blocked = dept_mapping.save_item_master_pending_bulk(ENGINE, changes, actor)
            if decisions:
                clear_dept_suggestion_caches()
            load_item_master_pending.clear()
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            st.session_state[f"_bulk_{kind}_open"] = False
            n = len(changes) - len(blocked)
            if kind == "edit":
                msg = f"Staged {n:,} UPC override(s) and {n_dec:,} Broken Out item decision(s)"
                if n_sugg:
                    msg += f" ({n_sugg:,} more went as suggestions, since someone else already decided those items)"
            else:
                msg = f"Staged {verb} {n:,} item(s)"
            st.session_state["_toast"] = (
                msg + " — see Pending Changes to review and push."
                + (f" {len(blocked)} skipped: someone else staged a change on them first." if blocked else ""),
                "",
            )
            st.rerun()


def render_item_decisions(df: pd.DataFrame, key: str, file_stem: str) -> None:
    """A list of items with the decision behind each one's Department (see
    dept_mapping.explain_items): counts per decision, a filter, the table,
    a CSV download, and a jump to any group still waiting."""
    if df.empty:
        st.caption("No items.")
        return
    got = df["Department"].notna() & (df["Department"].astype(str).str.strip() != "")
    counts = df["Decision"].value_counts()
    st.markdown(f"**{len(df):,} item(s)** · {int(got.sum()):,} have a Department · "
                f"{int((~got).sum()):,} don't yet")
    st.caption(" · ".join(f"{k}: {v:,}" for k, v in counts.items()))
    f1, f2 = st.columns([1.3, 2])
    choice = f1.selectbox("Show", ["All", "Have a Department", "No Department yet"] + list(counts.index),
                          key=f"{key}_show", label_visibility="collapsed")
    q = f2.text_input("Search", key=f"{key}_q", label_visibility="collapsed",
                      placeholder="Search UPC, description, brand, group…").strip().lower()
    view = df
    if choice == "Have a Department":
        view = df[got]
    elif choice == "No Department yet":
        view = df[~got]
    elif choice != "All":
        view = df[df["Decision"] == choice]
    if q:
        blob = view[["UPC", "Description", "Brand", "Group", "How"]].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        view = view[blob.str.contains(q, regex=False)]
    st.dataframe(view.drop(columns=["Tab"]), hide_index=True, width='stretch', height=min(520, 38 + 35 * max(len(view), 1)))
    c1, c2, c3 = st.columns([1.2, 2.2, 1])
    c1.download_button("Download (CSV)", view.drop(columns=["Tab"]).to_csv(index=False).encode(), file_name=f"{file_stem}.csv",
                       mime="text/csv", key=f"{key}_dl")
    waiting = view[view["Tab"].isin(["Crosswalk", "Unmatched", "Broken Out"]) & (view["Group"] != "")]
    groups = waiting.drop_duplicates("Group")
    if not groups.empty:
        g = c2.selectbox("Group waiting for a decision", groups["Group"].tolist(), key=f"{key}_grp",
                         label_visibility="collapsed",
                         format_func=lambda x: f"{x}  ({int((waiting['Group'] == x).sum())} item(s))")
        tab = groups.set_index("Group").loc[g, "Tab"]
        c3.button(f"Open in {tab}", key=f"{key}_open", width='stretch', on_click=_open_notification,
                  args=(tab, g.split(" — ", 1)[-1]))


# ===========================================================================
# Excel department workbook (download / upload) and Settings requests
# ===========================================================================

@st.cache_data(ttl=60, show_spinner=False)
def load_override_counts() -> dict:
    return dept_mapping.upc_override_counts(ENGINE)


def _owi_table(df: pd.DataFrame, key: str, height: int = 380) -> None:
    """One section of the import report: a result filter and the rows."""
    if df.empty:
        st.caption("Nothing here.")
        return
    outcomes = ["All"] + sorted(df["Result"].str.split(" — ").str[0].unique())
    pick = st.segmented_control("Show", outcomes, default="All", key=f"owi_show_{key}", label_visibility="collapsed") or "All"
    view = df if pick == "All" else df[df["Result"].str.startswith(pick)]
    hide = [c for c in ("combo_id", "move", "tier", "n_evidence", "source_key", "n_upcs_total") if c in view]
    st.dataframe(view.drop(columns=hide), hide_index=True, width="stretch",
                 height=min(height, 38 + 35 * max(len(view), 1)))


def _owi_report(p: dict, key: str) -> None:
    """The full report: counts, then every line by where it lands in the app."""
    owi = old_workbook_import
    summ = owi.summary(p)
    count = lambda outcome: int(summ.loc[summ["Outcome"] == outcome, "Count"].sum())
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("To stage", f"{count(owi.STAGE):,}")
    c2.metric("Moves to make", f"{count(owi.MOVE):,}")
    c3.metric("To take back", f"{count(owi.TAKE):,}", help="Staged changes you cleared in the workbook (your own only)")
    c4.metric("Already done", f"{count(owi.DONE):,}")
    c5.metric("Skipped", f"{count(owi.SKIP):,}")
    n_conf = sum(int(df["Result"].astype(str).str.startswith(owi.CONFLICT).sum()) for df in (p["groups"], p["items"], p["moves"]))
    if n_conf:
        st.warning(f"**{n_conf:,} change(s) in this workbook were skipped** because those groups or items have changed in the app "
                   f"since it was downloaded{' (' + p['downloaded_at'] + ')' if p.get('downloaded_at') else ''} — someone decided, "
                   "pushed, moved or staged them. Nothing of theirs is overwritten. Download a fresh copy to change those.")
    items, groups = p["items"], p["groups"]
    areas = ["Crosswalk", "Unmatched", "Broken Out", "Decided", "Item master"]
    tb = p.get("take_back", pd.DataFrame())
    tabs = st.tabs(["Summary", "Moves", "Group decisions"] + [f"{a} items" for a in areas] + (["Taken back"] if len(tb) else []))
    with tabs[0]:
        st.dataframe(summ, hide_index=True, width="stretch")
        st.caption("Items: a Broken Out group's item gets an item decision there; any other item gets a UPC override "
                   "of just its Department. “Already done” means the app already has it that way (live or staged).")
    with tabs[1]:
        _owi_table(p["moves"], f"{key}_moves")
    with tabs[2]:
        _owi_table(groups, f"{key}_groups")
    for tab, area in zip(tabs[3:3 + len(areas)], areas):
        with tab:
            _owi_table(items[items["Area"] == area], f"{key}_{area}")
    if len(tb):
        with tabs[-1]:
            st.caption("Staged changes the downloaded workbook showed that this copy no longer asks for. Only your own "
                       "are taken back; anyone else's are listed as kept.")
            _owi_table(tb, f"{key}_take_back")


REQUEST_BADGE = {"pending": "Waiting on an admin", "approved": "Approved", "denied": "Denied", "withdrawn": "Withdrawn"}


def _request_when(v) -> str:
    t = pd.to_datetime(v, errors="coerce")
    return "" if pd.isna(t) else t.tz_localize("UTC").tz_convert("America/Los_Angeles").strftime("%b %d %I:%M %p")


def _after_settings_request(kind: str, what: str, toast: str) -> None:
    """A new Department is usable straight away; a Strict Department or an
    Unmatched Default only takes effect once the Department engine re-runs."""
    clear_settings_caches()
    load_notifications.clear()
    if kind in ("add_strict", "unmatched_default"):
        apply_settings_change(what)  # re-runs the engine, then reruns the page
    st.session_state["_toast"] = (toast, "")
    st.rerun()


def render_settings_requests_admin() -> None:
    """Admins: what editors have asked for, to approve or deny."""
    reqs = dept_mapping.list_settings_requests(ENGINE)
    waiting = [r for r in reqs if r["status"] == "pending"]
    decided = [r for r in reqs if r["status"] in ("approved", "denied")]
    if not reqs:
        return
    st.markdown(f"#### Requests from editors ({len(waiting)} waiting)")
    st.caption("Approving makes the change (a Strict Department or Unmatched Default re-runs the engine, about a minute). "
               "Either decision can be undone below.")
    if not waiting:
        st.caption("Nothing waiting.")
    for r in waiting:
        rid = r["request_id"]
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**#{rid}** · {r['summary']}")
            c1.caption(f"Asked by **{r['requested_by']}** · {_request_when(r['requested_at'])}"
                       + (f" · “{r['reason']}”" if r.get("reason") else ""))
            note = c1.text_input("Note back to them (optional)", key=f"req_note_{rid}", label_visibility="collapsed",
                                 placeholder="Note back to them (optional)")
            if c2.button("Approve", key=f"req_approve_{rid}", type="primary", width="stretch"):
                res = dept_mapping.approve_settings_request(ENGINE, rid, st.session_state["name"], note)
                if not res["ok"]:
                    st.session_state["_toast"] = (f"Couldn't approve #{rid}: {res['problem']}", "")
                    st.rerun()
                _after_settings_request(res["kind"], f"request #{rid}", f"Approved #{rid}: {r['summary']}")
            if c3.button("Deny", key=f"req_deny_{rid}", width="stretch"):
                dept_mapping.deny_settings_request(ENGINE, rid, st.session_state["name"], note)
                load_notifications.clear()
                st.session_state["_toast"] = (f"Denied #{rid}: {r['summary']}", "")
                st.rerun()
    if decided:
        with st.expander(f"Decided requests ({len(decided)})"):
            for r in decided[:50]:
                rid = r["request_id"]
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"**#{rid}** · {r['summary']} — {REQUEST_BADGE[r['status']]} by {r['decided_by']} "
                            f"({_request_when(r['decided_at'])})" + (f" · “{r['admin_note']}”" if r.get("admin_note") else ""))
                c1.caption(f"Asked by {r['requested_by']}")
                if c2.button("Undo", key=f"req_undo_{rid}", width="stretch",
                             help="Back to waiting — an approval's change is taken back first."):
                    res = dept_mapping.undo_settings_request_decision(ENGINE, rid, st.session_state["name"])
                    if not res["ok"]:
                        st.session_state["_toast"] = (res["problem"], "")
                        st.rerun()
                    if res["was"] == "approved":
                        _after_settings_request(res["kind"], f"undo of request #{rid}", f"Undid #{rid} — the change is taken back and it's waiting again.")
                    load_notifications.clear()
                    st.session_state["_toast"] = (f"Undid the denial of #{rid} — it's waiting again.", "")
                    st.rerun()
    st.divider()


def render_settings_request_form() -> None:
    """Editors: Department Review settings are managed by admins — ask for a change here."""
    st.markdown("#### Request a change")
    st.caption("Admins manage these settings. Ask here — you'll be notified when it's approved or denied.")
    ver = st.session_state.get("_req_ver", 0)
    kinds = dept_mapping.SETTINGS_REQUEST_KINDS
    kind = st.radio("What would you like?", list(kinds), format_func=kinds.get, horizontal=True, key=f"req_kind_{ver}")
    sources = ["any"] + sorted(load_sources()["source_key"].tolist())
    departments = load_departments()["department"].tolist()
    payload = {}
    if kind == "add_department":
        payload["department"] = st.text_input("New Department", key=f"req_dept_{ver}", placeholder="e.g. FOOD SERVICE").strip().upper()
    else:
        c1, c2 = st.columns([1, 2])
        payload["source_key"] = c1.selectbox("Source", sources, key=f"req_src_{ver}",
                                             format_func=lambda x: "Any source" if x == "any" else x.upper())
        if kind == "unmatched_default":
            olds = load_unmatched_old_departments()
            if payload["source_key"] != "any":  # just the ones this source uses
                olds = olds[olds["sources"].fillna("").str.split(", ").map(lambda xs: payload["source_key"].upper() in xs)]
            choices = sorted(set(olds["old_department"].dropna()) - {""})
        else:
            choices = load_old_department_texts()
        payload["old_department"] = (c2.selectbox("Distributor's Department", choices, index=None, key=f"req_old_{ver}",
                                                  placeholder="Pick the distributor's Department…") or "").strip()
        if kind == "add_strict":
            payload["trust_direct_evidence"] = st.checkbox("Trust direct evidence (an exact Scan Advantage match can still auto-decide)",
                                                          key=f"req_trust_{ver}")
        else:
            new = st.selectbox("Default Department", ["(remove the default)"] + departments, key=f"req_new_{ver}")
            payload["new_department"] = None if new == "(remove the default)" else new
    reason = st.text_area("Why? (helps the admin decide)", key=f"req_reason_{ver}", height=80)
    if st.button("Send request", type="primary", key=f"req_send_{ver}"):
        res = dept_mapping.create_settings_request(ENGINE, kind, payload, reason, st.session_state["name"])
        if not res["ok"]:
            st.error(res["problem"])
        else:
            load_notifications.clear()
            st.session_state["_req_ver"] = ver + 1
            st.session_state["_toast"] = (f"Sent request #{res['request_id']} to the admins — you'll be notified when it's decided.", "")
            st.rerun()

    mine = dept_mapping.list_settings_requests(ENGINE, requested_by=st.session_state["name"])
    if mine:
        st.markdown("##### Your requests")
        for r in mine[:30]:
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"**#{r['request_id']}** · {r['summary']} — {REQUEST_BADGE.get(r['status'], r['status'])}")
                c1.caption(f"Sent {_request_when(r['requested_at'])}"
                           + (f" · decided by {r['decided_by']} {_request_when(r['decided_at'])}" if r["status"] in ("approved", "denied") else "")
                           + (f" · “{r['admin_note']}”" if r.get("admin_note") else ""))
                if r["status"] == "pending" and c2.button("Withdraw", key=f"req_withdraw_{r['request_id']}", width="stretch"):
                    dept_mapping.withdraw_settings_request(ENGINE, r["request_id"], st.session_state["name"])
                    load_notifications.clear()
                    st.session_state["_toast"] = (f"Withdrew request #{r['request_id']}.", "")
                    st.rerun()

    st.divider()
    st.markdown("#### Current settings (read-only)")
    c1, c2 = st.columns([1, 1.2])
    with c1:
        st.markdown("##### Departments")
        st.dataframe(load_departments().rename(columns={"department": "Department", "source_type": "From"}),
                     hide_index=True, width="stretch", height=300)
    with c2:
        st.markdown("##### Strict Departments")
        st.dataframe(load_strict_departments().rename(columns={"source_key": "Source", "old_department": "Distributor's Department",
                                                               "trust_direct_evidence": "Trust direct evidence"}),
                     hide_index=True, width="stretch")
    st.markdown("##### Unmatched Department Defaults")
    render_unmatched_defaults_editor(editable=False)


def render_workbook_section() -> None:
    """Admin only, at the bottom of Settings: work in the old-style department
    workbook in Excel — download the app as one, then upload it (or an old
    department workbook from before the app) to stage the changes."""
    st.divider()
    st.markdown("#### Excel department workbook")
    st.caption(
        'Work in Excel: download the app as a department workbook, edit the blue columns, upload it back.',
        help=("Prefer working in Excel? Download the app as an old-style department workbook — every Crosswalk, Unmatched, "
        "Broken Out and Decided group and every Broken Out item, with what's staged in Pending Changes already filled "
        "in. Make your changes in the blue columns (Approve, Manual Override Department, Actions like Send to Broken "
        "Out / Return to Crosswalk, Final UPC Overrides), then upload it below. You get a full report first, and only "
        "your changes are staged — anything unchanged is skipped as Already done. An old department workbook from "
        "before the app can be uploaded the same way."),
    )
    # One click: the file is built from the app's state at that moment
    # (about 15 seconds) and saved straight away.
    st.download_button(
        "Download the app as a workbook", lambda: old_workbook_import.export_workbook(ENGINE), key="owx_download",
        type="primary", on_click="ignore",
        file_name=f"Item Master department workbook {datetime.now():%Y-%m-%d %H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="Builds the workbook from everything in the app right now, then saves it — takes about 15 seconds.",
    )
    render_old_workbook_import()


def render_old_workbook_import() -> None:
    """Admin only: load the decisions people made in an old department
    workbook into Pending Changes — see old_workbook_import."""
    owi = old_workbook_import
    ver = st.session_state.get("_owi_ver", 0)
    done = st.session_state.get("_owi_done")
    with st.container():
        if done:
            r = done["result"]
            st.success(
                f"Imported **{done['name']}**: {r['moved']:,} move(s) made, {r['groups']:,} group decision(s) and "
                f"{r['item_decisions']:,} Broken Out item decision(s) staged in **Department Review → Pending Changes**, "
                f"{r['overrides']:,} UPC override(s) staged in the **Pending Changes** tab"
                + (f", {r['suggested']:,} went as suggestions (someone else owns those items)" if r["suggested"] else "")
                + (f", {r['blocked']:,} skipped (someone else staged them first)" if r["blocked"] else "")
                + (f", {r['taken_back']:,} staged change(s) you cleared taken back" if r.get("taken_back") else "")
                + f". A snapshot (#{r['snapshot']}) was taken first, so it can all be put back.")
            d1, d2 = st.columns(2)
            d1.download_button("Download the full report", done["report"], file_name=done["report_name"],
                               key="owi_done_report", width="stretch",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            if d2.button("Import another workbook", key="owi_again", width="stretch"):
                st.session_state.pop("_owi_done", None)
                st.session_state["_owi_open"] = True
                st.rerun()
            p = done["plan"]
            touched = pd.concat([
                p["groups"].loc[p["groups"]["Result"] == owi.STAGE, ["Group"]],
                p["items"].loc[p["items"]["Result"] == owi.STAGE, ["Group"]],
                p["moves"].loc[p["moves"]["Result"] == owi.MOVE, ["App group"]].rename(columns={"App group": "Group"}),
            ]).groupby("Group").size()
            touched = touched[touched.index != "(no group)"]
            if len(touched):
                st.markdown("**Groups it touched** — open one to see it:")
                j1, j2 = st.columns([3, 1])
                g = j1.selectbox("Group", list(touched.index), key="owi_jump_group", label_visibility="collapsed",
                                 format_func=lambda x: f"{x}  ({int(touched[x]):,} change(s))")
                if g:
                    search = g.split(" — ", 1)[-1].split(" / ")[-1]
                    j2.button("Open in Pending Changes", key="owi_jump", width="stretch",
                              on_click=_open_notification, args=("Pending Changes", search))
            _owi_report(p, "done")
            return
        up = st.file_uploader("Upload a department workbook (.xlsx)", type=["xlsx"], key=f"owi_file_{ver}")
        if up is None:
            return
        st.session_state["_owi_open"] = True
        data = up.getvalue()
        digest = hashlib.sha1(data).hexdigest()
        cached = st.session_state.get("_owi")
        if not cached or cached["hash"] != digest:
            try:
                with st.spinner("Reading the workbook and checking every decision against the app — about a minute…"):
                    extracted = owi.extract(owi.read_workbook(io.BytesIO(data)))
                    p = owi.plan(ENGINE, extracted, load_departments()["department"].tolist(), st.session_state["name"])
            except ValueError as e:
                st.error(str(e))
                return
            cached = {"hash": digest, "name": up.name, "plan": p}
            st.session_state["_owi"] = cached
        p = cached["plan"]
        _owi_report(p, "plan")
        report_name = f"Import report - {up.name.rsplit('.', 1)[0]}.xlsx"
        n_stage = int((p["groups"]["Result"] == owi.STAGE).sum() + (p["items"]["Result"] == owi.STAGE).sum())
        n_move = int((p["moves"]["Result"] == owi.MOVE).sum())
        n_take = int((p.get("take_back", pd.DataFrame(columns=["Result"]))["Result"] == owi.TAKE).sum())
        b1, b2 = st.columns(2)
        b1.download_button("Download this report", owi.report_excel(p, f"Import preview — {up.name}"),
                           file_name=report_name, key="owi_plan_report", width="stretch",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if b2.button(f"Stage {n_stage:,} change(s)" + (f", make {n_move:,} move(s)" if n_move else "")
                     + (f", take back {n_take:,}" if n_take else ""),
                     type="primary", key="owi_apply", width="stretch", disabled=not (n_stage or n_move or n_take)):
            with st.spinner("Importing — staging everything as you…"):
                result = owi.apply(ENGINE, p, st.session_state["name"], is_admin, up.name)
            activity("Department Review", f"Uploaded workbook {up.name}: {result['groups']:,} group decision(s), "
                     f"{result['item_decisions']:,} item decision(s), {result['overrides']:,} UPC override(s) staged, "
                     f"{result['moved']:,} move(s)", up.name, result["groups"] + result["item_decisions"] + result["overrides"])
            st.cache_data.clear()
            load_notifications.clear()
            st.session_state["_owi_done"] = {"result": result, "plan": p, "name": up.name, "report_name": report_name,
                                             "report": owi.report_excel(p, f"Imported {up.name} (snapshot #{result['snapshot']} taken first)")}
            st.session_state.pop("_owi", None)
            st.session_state["_owi_ver"] = ver + 1
            st.rerun()


# ===========================================================================
# Pending Changes tab sections (items, sources)
# ===========================================================================

def item_master_pending_cross_link() -> dict:
    """Loads the shared Pending Item Master Changes queue (for this tab's
    own filtering — e.g. hiding a row that already has a change staged),
    and points to the dedicated Pending Changes tab if anything's
    staged. The actual review/Undo/Push UI lives on that tab now instead
    of being repeated on every page that can stage a change — it seemed
    weird to have to check three separate pages for the same queue."""
    pending = load_item_master_pending()
    if pending:
        st.caption(f"\U0001f4cb {len(pending):,} item change(s) staged, not yet pushed — see Pending Changes.")
    return pending


ITEM_MASTER_PENDING_SORT_OPTIONS = {
    "Staged (newest first)": ("staged_at", True),
    "Staged (oldest first)": ("staged_at", False),
    "UPC": ("upc", False),
    "Description": ("description", False),
    "Department": ("department", False),
    "Action": ("change_type", False),
    "Staged By": ("staged_by", False),
}


def render_item_master_pending_section() -> dict:
    """Pending Item Master Changes queue — fed by the Add Item, Delete
    Item, and UPC Overrides tabs, all writing to the same
    dbo.item_master_pending_changes table. A click here is durable and
    visible to every editor immediately (no private-draft/Save step).
    Search/sort make this usable once it grows past a handful of rows;
    an "Include in this push" checkbox per row (defaulted on) lets a
    push go out for only the changes actually ready, leaving the rest —
    still a work in progress, or waiting on another person to look them
    over — sitting exactly as they were, not forced into an all-or-
    nothing batch. Returns the pending dict so callers can keep their own
    lists/checks in sync (e.g. not letting a UPC be staged twice at once)."""
    if st.session_state.pop("_reset_confirm_push_item_master", False):
        st.session_state["confirm_push_item_master"] = False
    render_discard_notices("item_master")
    pending = load_item_master_pending()
    if not pending:
        return pending

    fcol1, fcol2 = st.columns([3, 2])
    search = fcol1.text_input(
        "Search pending changes", key="im_pending_search",
        placeholder="Filter by UPC, description, department, or who staged it…",
    )
    sort_label = fcol2.selectbox("Sort by", list(ITEM_MASTER_PENDING_SORT_OPTIONS.keys()), key="im_pending_sort")
    sort_col, sort_desc = ITEM_MASTER_PENDING_SORT_OPTIONS[sort_label]

    rows_df = pd.DataFrame([{"upc": upc, **change} for upc, change in pending.items()])
    if search:
        s = search.lower()
        mask = pd.Series(False, index=rows_df.index)
        for col in ["upc", "description", "department", "staged_by"]:
            mask |= rows_df[col].fillna("").astype(str).str.lower().str.contains(s, regex=False)
        rows_df = rows_df[mask]
    rows_df = sort_full_df(rows_df, sort_col, sort_desc) if not rows_df.empty else rows_df

    if search and rows_df.empty:
        st.caption(f"No pending changes match “{search}”.")
    matching_upcs = rows_df["upc"].tolist()
    # Cards are drawn a page at a time — a bulk upload can stage thousands.
    page_size = 50
    n_pages = max(1, -(-len(matching_upcs) // page_size))
    page = min(st.session_state.get("im_pending_page", 1), n_pages)
    if st.session_state.get("im_pending_page") not in (None, page):
        st.session_state["im_pending_page"] = page
    visible_upcs = matching_upcs[(page - 1) * page_size: page * page_size]

    if visible_upcs:
        what = "all shown" if len(matching_upcs) <= page_size else f"all {len(matching_upcs):,} matching"
        scol1, scol2, scol3 = st.columns([1.3, 1.3, 2.4])
        if scol1.button(f"Include {what}", key="im_pending_select_all"):
            for upc in matching_upcs:
                st.session_state[f"im_pending_include_{upc}"] = True
            st.rerun()
        if scol2.button(f"Leave out {what}", key="im_pending_select_none"):
            for upc in matching_upcs:
                st.session_state[f"im_pending_include_{upc}"] = False
            st.rerun()
        if n_pages > 1:
            scol3.number_input(f"Page (of {n_pages:,}, {page_size} per page)", min_value=1, max_value=n_pages,
                               key="im_pending_page")

    needs_live_lookup = any(c["change_type"] in ("edit", "delete") for c in pending.values())
    if needs_live_lookup:  # just the cards on this page, not the whole item master
        all_items = load_items()
        live_by_upc = all_items[all_items["UPC"].isin(visible_upcs)].set_index("UPC").to_dict("index")
    else:
        live_by_upc = {}
    item_fields = list(ITEM_MASTER_FIELD_LABELS.keys())
    page_groups = dept_mapping.groups_for_upcs(ENGINE, visible_upcs) if visible_upcs else {}
    for upc in visible_upcs:
        change = pending[upc]
        with st.container(border=True):
            c0, c1, c2, c3 = st.columns([0.4, 3.6, 2, 1])
            include = c0.checkbox(
                "Include", value=True, key=f"im_pending_include_{upc}", label_visibility="collapsed",
                help="Included in the next push — uncheck to save this one for later without losing it.",
            )
            action_label = {"add": "Add", "delete": "Delete", "edit": "Edit"}[change["change_type"]]
            c1.markdown(f"**{action_label}** {upc} — {change['description'] or '(no description)'}")
            c1.caption(f"Staged by {change.get('staged_by') or 'unknown'} at {change.get('staged_at')}")
            item_group = page_groups.get(upc)
            if item_group is not None:
                f = load_group_facts().get(item_group, {})
                bits = [b for b in (f.get("raw_department"), f.get("raw_category"), f.get("raw_subcategory")) if b]
                c1.caption(f"{f.get('where', '?')} group: {(f.get('source_key') or '').upper()} — "
                           f"{' / '.join(bits) or '(blank)'}" + (f" · {short_note(change.get('origin_note'))}" if change.get("origin_note") else ""))
            elif change.get("origin_note"):
                c1.caption(short_note(change["origin_note"]))
            c2.markdown(change["department"] or "(no department)")
            live_row = live_by_upc.get(upc, {})
            old_values = {f: live_row.get(ITEM_MASTER_FIELD_TO_DF_COLUMN[f]) for f in item_fields}
            if change["change_type"] == "edit":
                diffs = diff_pending_fields(old_values, change, item_fields, ITEM_MASTER_FIELD_LABELS)
                if diffs:
                    c1.caption("Changing: " + ", ".join(d[0] for d in diffs))
                else:
                    c1.caption("No fields differ from the live item master.")
                with st.expander(f"Full details ({len(diffs)} field(s) changing)", key=f"im_pending_expander_{upc}"):
                    render_pending_field_table(diffs, ["Field", "Current", "Staged"])
            elif change["change_type"] == "add":
                rows = [(ITEM_MASTER_FIELD_LABELS[f], _format_pending_value(change.get(f))) for f in item_fields if not _is_blank(change.get(f))]
                with st.expander("Full details", key=f"im_pending_expander_{upc}"):
                    render_pending_field_table(rows, ["Field", "Value"])
            else:  # delete
                rows = [(ITEM_MASTER_FIELD_LABELS[f], _format_pending_value(old_values.get(f))) for f in item_fields if not _is_blank(old_values.get(f))]
                with st.expander("What will be removed", key=f"im_pending_expander_{upc}"):
                    render_pending_field_table(rows, ["Field", "Current Value"])
            if c3.button("Undo", key=f"undo_item_master_pending_{upc}", width='stretch'):
                dept_mapping.delete_item_master_pending(ENGINE, upc)
                activity("Items", f"Took back a staged {action_label.lower()} (staged by {change.get('staged_by') or '?'})", upc, 1)
                load_item_master_pending.clear()
                st.rerun()

    included_upcs = [upc for upc in pending if st.session_state.get(f"im_pending_include_{upc}", True)]
    left_out = len(pending) - len(included_upcs)
    st.warning(f"Push makes the **{len(included_upcs):,}** included change(s) live right away"
               + (f" ({left_out:,} left out stay staged)" if left_out else "") + ".")
    confirm_push = st.checkbox(
        "I've reviewed these changes and I'm ready to update the database.", key="confirm_push_item_master",
    )
    if st.button(
        f"Push {len(included_upcs):,} Included Change(s) to the Database", type="primary",
        key="push_item_master_pending", disabled=not confirm_push or not included_upcs,
    ):
        push_actor = st.session_state["name"]
        push_fns = {"add": push_item_master_add, "delete": push_item_master_delete}
        edits = {upc: pending[upc] for upc in included_upcs if pending[upc]["change_type"] == "edit"}
        with st.spinner(f"Pushing {len(included_upcs):,} change(s)…"):
            dept_mapping.push_item_master_edits(ENGINE, edits, push_actor)
            for upc in included_upcs:
                change = pending[upc]
                if change["change_type"] != "edit":
                    push_fns[change["change_type"]](upc, change, push_actor)
                    dept_mapping.delete_item_master_pending(ENGINE, upc)
                st.session_state.pop(f"im_pending_include_{upc}", None)
        pushed_count = len(included_upcs)
        n_types = Counter(pending[u]["change_type"] for u in included_upcs if pending[u]["change_type"] != "edit")
        if n_types:
            activity("Pushed live", "Pushed " + ", ".join(f"{n:,} item {'add' if k == 'add' else 'delete'}(s)" for k, n in n_types.items()),
                     ", ".join(u for u in included_upcs if pending[u]["change_type"] != "edit")[:500], sum(n_types.values()))
        load_item_master_pending.clear()
        load_items.clear()
        load_manual_items.clear()
        load_deleted_items.clear()
        st.session_state["_reset_confirm_push_item_master"] = True
        st.success(f"Pushed {pushed_count:,} change(s) to the database.")
        st.rerun()
    st.divider()
    return pending


@st.cache_data(show_spinner=False)
def load_source_pending_changes() -> dict:
    """Staged Sources tab edits/adds — {source_key: {...}} — visible to
    every editor the moment they're staged, same convention as
    load_item_master_pending."""
    return dept_mapping.get_source_pending_changes(ENGINE)


def source_pending_cross_link() -> dict:
    """Loads the pending source-change queue for the Sources tab's own
    use (nothing to filter on there since the config grid is small, just
    a pointer to where to review/push/undo it)."""
    pending = load_source_pending_changes()
    if pending:
        st.caption(f"\U0001f4cb {len(pending):,} source change(s) staged, not yet pushed — see Pending Changes.")
    return pending


def push_source_add(source_key: str, config: dict, actor: str) -> None:
    cols = ", ".join(dept_mapping.SOURCE_CONFIG_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in dept_mapping.SOURCE_CONFIG_COLUMNS)
    with db_begin() as conn:
        conn.execute(
            text(f"INSERT INTO dbo.sources (source_key, {cols}) VALUES (:source_key, {placeholders})"),
            {"source_key": source_key, **{c: config.get(c) for c in dept_mapping.SOURCE_CONFIG_COLUMNS}},
        )


def push_source_edit(source_key: str, config: dict, apply_now: bool, actor: str) -> str | None:
    """Applies the config unconditionally, then — if apply_now — tries to
    re-run ingestion against the last uploaded file. The config update
    always succeeds or fails on its own; a bad new mapping (e.g. a
    Department Column that doesn't exist in the file actually on record)
    must not leave this source's pending row stuck forever or abort the
    rest of a multi-source Push, so the re-run step is caught separately
    and reported back as a warning string instead of raising — the
    config change still goes through, and the source key stays in
    Ingestion history if a person wants to try a fresh upload instead."""
    set_clause = ", ".join(f"{c} = :{c}" for c in dept_mapping.SOURCE_CONFIG_COLUMNS)
    with db_begin() as conn:
        conn.execute(
            text(f"UPDATE dbo.sources SET {set_clause}, updated_at = SYSUTCDATETIME() WHERE source_key = :source_key"),
            {"source_key": source_key, **{c: config.get(c) for c in dept_mapping.SOURCE_CONFIG_COLUMNS}},
        )
    if apply_now:
        upload = load_raw_upload(ENGINE, source_key)
        if upload is None:
            return (
                f"'{source_key}': configuration was saved, but there's no stored upload to re-run "
                "against — Apply Now had nothing to do. This happens when the source's raw data came "
                "from outside this app's Upload & Ingest tab (e.g. an earlier bulk load), or predates "
                "this feature. Upload the file once on Upload & Ingest to apply this configuration to "
                "it now and enable Apply Now for future edits."
            )
        try:
            cleaned_df, stats, rejected_df = map_and_clean(upload["df"], config)
            stage_source(
                ENGINE, source_key, cleaned_df, rejected_df, stats,
                uploaded_by=actor, original_filename=upload["filename"], on_retry=_warn_retrying,
            )
            load_raw_item_counts.clear()
            load_ingestion_log.clear()
            load_stale_sources.clear()
            load_stale_sources_since_compute.clear()
        except Exception as e:
            return (
                f"'{source_key}': configuration was saved, but re-running ingestion against "
                f"{upload['filename']} failed ({e}) — check the column mapping and upload the "
                "file again on Upload & Ingest."
            )
    return None


def render_source_pending_section() -> dict:
    """Pending Sources-tab changes queue — an edit to an existing source
    or a brand new one, staged here instead of written to dbo.sources
    directly. Push actually applies it: an 'add' just inserts; an 'edit'
    updates the config and, if apply_now was checked when it was staged,
    also re-runs ingestion against that source's last uploaded file with
    the new configuration (dbo.source_raw_uploads) — no fresh upload
    needed. Undo just deletes the pending row either way: for an edit,
    dbo.sources is left exactly as it was since nothing was written yet;
    for an add, the source is simply never created."""
    # Must run BEFORE the checkbox widget below is instantiated — same
    # StreamlitWidgetAlreadyInstantiatedError this pattern avoids
    # everywhere else it's used (Department Review / Item Master Push).
    if st.session_state.pop("_reset_confirm_push_source_changes", False):
        st.session_state["confirm_push_source_changes"] = False
    push_warnings = st.session_state.pop("_source_push_warnings", None)
    if push_warnings:
        for w in push_warnings:
            st.warning(w)
    # A push with Apply Now already triggers auto_recompute_and_push_merge()
    # right in the button handler below — this just shows that result once,
    # on the rerun it triggers, the same "stash in session_state, pop and
    # show on the next render" pattern as _source_push_warnings.
    render_auto_merge_result(st.session_state.pop("_source_push_auto_merge_result", None))
    pending = load_source_pending_changes()
    if not pending:
        return pending
    live_by_key = load_sources().set_index("source_key").to_dict("index") if any(c["change_type"] == "edit" for c in pending.values()) else {}
    for source_key, change in list(pending.items()):
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 2, 1])
            action_label = "Add" if change["change_type"] == "add" else "Edit"
            c1.markdown(f"**{action_label}** {change['source_label'] or '(no label)'} ({source_key})")
            if change["change_type"] == "edit":
                live_config = live_by_key.get(source_key, {})
                diffs = diff_pending_fields(live_config, change, dept_mapping.SOURCE_CONFIG_COLUMNS, SOURCE_FIELD_LABELS)
                if diffs:
                    c1.caption("Changing: " + ", ".join(d[0] for d in diffs))
                elif change["apply_now"]:
                    c1.caption("No configuration fields changed — Apply Now will re-run ingestion as-is.")
                else:
                    c1.caption("No configuration fields changed.")
                new_apply_now = c2.checkbox(
                    "Apply immediately", value=change["apply_now"], key=f"apply_now_toggle_{source_key}",
                    help="Checked: Push also re-runs ingestion against this source's last uploaded "
                         "file with this new configuration. Unchecked: Push only updates the "
                         "configuration, for the next upload.",
                )
                if new_apply_now != change["apply_now"]:
                    dept_mapping.set_source_pending_apply_now(ENGINE, source_key, new_apply_now)
                    load_source_pending_changes.clear()
                    st.rerun()
                with st.expander(f"Full details ({len(diffs)} field(s) changing)", key=f"source_pending_expander_{source_key}"):
                    render_pending_field_table(diffs, ["Field", "Current", "Staged"])
            else:
                rows = [(SOURCE_FIELD_LABELS.get(col, col), _format_pending_value(change[col])) for col in dept_mapping.SOURCE_CONFIG_COLUMNS if not _is_blank(change[col])]
                with st.expander("Full details", key=f"source_pending_expander_{source_key}"):
                    render_pending_field_table(rows, ["Field", "Value"])
            if c3.button("Undo", key=f"undo_source_pending_{source_key}", width='stretch'):
                dept_mapping.delete_source_pending_change(ENGINE, source_key)
                activity("Sources", f"Took back a staged source {'add' if change['change_type'] == 'add' else 'edit'}", source_key)
                load_source_pending_changes.clear()
                st.rerun()
    st.warning(f"Push makes all **{len(pending):,}** staged source change(s) live right away.")
    confirm_push = st.checkbox(
        "I've reviewed these source changes and I'm ready to update the database.",
        key="confirm_push_source_changes",
    )
    if st.button(
        f"Push {len(pending):,} Source Change(s) to the Database", type="primary",
        key="push_source_pending", disabled=not confirm_push,
    ):
        push_actor = st.session_state["name"]
        rerun_warnings = []
        applied_now_sources = []
        for source_key, change in pending.items():
            config = {col: change[col] for col in dept_mapping.SOURCE_CONFIG_COLUMNS}
            if change["change_type"] == "add":
                push_source_add(source_key, config, push_actor)
            else:
                warning = push_source_edit(source_key, config, change["apply_now"], push_actor)
                if warning:
                    rerun_warnings.append(warning)
                elif change["apply_now"]:
                    applied_now_sources.append(source_key)
            dept_mapping.delete_source_pending_change(ENGINE, source_key)
            activity("Pushed live", ("Pushed a new source" if change["change_type"] == "add" else "Pushed a source edit")
                     + (" (re-ran its last file)" if change.get("apply_now") and change["change_type"] != "add" else "")
                     + f" (staged by {change.get('staged_by') or '?'})", source_key)
        pushed_count = len(pending)
        load_sources.clear()
        load_source_pending_changes.clear()
        load_stale_sources.clear()
        load_stale_sources_since_compute.clear()
        st.session_state["_reset_confirm_push_source_changes"] = True
        if rerun_warnings:
            st.session_state["_source_push_warnings"] = rerun_warnings
        st.success(f"Pushed {pushed_count:,} source change(s) to the database.")
        # Apply Now already refreshed raw_items for these sources — auto-
        # recompute a fresh merge draft right now instead of leaving that
        # new data invisible until a separate, easy-to-forget trip to the
        # Merge tab (the exact confusion this used to cause: "I pushed and
        # Apply'd, why doesn't Item Master show it?"). The draft still
        # needs a review + push on the Merge tab before it goes live — see
        # auto_recompute_and_push_merge's docstring for why that's never
        # automatic. A source WITHOUT Apply Now checked is the other half
        # of the same design — its config is saved, but nothing re-ingests
        # and nothing computes until a real file shows up next.
        if applied_now_sources:
            with st.spinner(f"Recomputing the merge draft with {', '.join(applied_now_sources)}'s refreshed data..."):
                st.session_state["_source_push_auto_merge_result"] = auto_recompute_and_push_merge()
        st.rerun()
    st.divider()
    return pending


DEPT_DIALOG_KEYS = ("dept_confirm_send_back", "dept_confirm_break_out", "dept_undo_picker", "dept_admin_override", "dept_topbar_step")


# ===========================================================================
# Broken Out grids and the Excel file for a group
# ===========================================================================

def match_department(value, options: list):
    """Typed or pasted text -> a real Department: exact (any case), else the
    only one it starts, else the only one containing it. None if unclear."""
    v = str(value or "").strip().upper()
    if not v:
        return None
    upper = {o.upper(): o for o in options}
    if v in upper:
        return upper[v]
    for test in (lambda o: o.startswith(v), lambda o: v in o):
        hits = [o for u, o in upper.items() if test(u)]
        if len(hits) == 1:
            return hits[0]
    return None


def _row_matches(text_blob: str, query: str) -> bool:
    # "cola, pepsi" = either; "diet cola" = both words.
    for alt in [a.strip() for a in query.lower().split(",") if a.strip()]:
        if all(w in text_blob for w in alt.split()):
            return True
    return False


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _draft_record(where: str, label: str, changes: dict, file: dict | None = None) -> None:
    """Grid work that isn't staged yet still goes on the top-bar Undo list.
    changes: {grid key: (before draft, after draft)} — one step can touch
    several grids (an Excel file filling a whole group)."""
    diffs = {}
    for k, (b, a) in changes.items():
        rows = [u for u in set(b) | set(a) if (b.get(u) or "") != (a.get(u) or "")]
        if rows:
            diffs[k] = {"before": {u: b.get(u) or "" for u in rows}, "after": {u: a.get(u) or "" for u in rows}}
    changes = diffs
    if not changes:
        return
    hist = st.session_state.setdefault("_draft_undo", [])
    hist.append({"changes": changes, "where": where, "label": label, "at": _utcnow(), "file": file})
    st.session_state["_draft_redo"] = []
    trim_grid_undo()


GRID_UNDO_KEEP = 50        # grid Undo steps kept per person
GRID_UNDO_FILES_KEEP = 3   # uploaded Excel files kept for undo (newest)


def trim_grid_undo() -> None:
    """Grid Undo / Redo keeps the newest GRID_UNDO_KEEP steps from the last
    UNDO_KEEP_HOURS hours. Only the newest GRID_UNDO_FILES_KEEP steps keep
    their uploaded Excel file — undoing an older one still puts the grid
    values back, just without re-offering the file."""
    cutoff = _utcnow() - pd.Timedelta(hours=dept_mapping.UNDO_KEEP_HOURS)
    for k, stamp in (("_draft_undo", "at"), ("_draft_redo", "undone_at")):
        steps = [e for e in st.session_state.get(k) or [] if (e.get(stamp) or e.get("at")) and (e.get(stamp) or e.get("at")) >= cutoff]
        del steps[:-GRID_UNDO_KEEP]
        st.session_state[k] = steps
    with_files = [e for e in (st.session_state.get("_draft_undo") or []) + (st.session_state.get("_draft_redo") or []) if e.get("file")]
    for e in sorted(with_files, key=lambda e: e.get("undone_at") or e["at"])[:-GRID_UNDO_FILES_KEEP]:
        e["file"] = None
    staged = st.session_state.get("_excel_staged_actions") or {}
    for aid in sorted(staged)[:-GRID_UNDO_FILES_KEEP]:
        staged.pop(aid)


def _draft_apply(entry: dict, which: str) -> None:
    for key, ch in entry["changes"].items():
        state = st.session_state.setdefault(f"wb_{key}", {"draft": {}, "ver": 0, "filter": "", "show": "All"})
        state["draft"].update(ch[which])
        state["ver"] += 1


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""


def render_item_workbench(key: str, items: pd.DataFrame, options: list, **kw) -> dict | None:
    """A spreadsheet-style grid for giving many items a Department fast (see
    _workbench_grid). Returns {upc: department} on the run right after its
    stage button is clicked, otherwise None."""
    ready = st.session_state.pop(f"_wb_stage_{key}", None)
    st.session_state.setdefault(f"wb_{key}", {"draft": {}, "ver": 0, "filter": "", "show": "All"})
    _workbench_grid(key, items, options, **kw)
    return ready


def _workbench_body(key: str, items: pd.DataFrame, options: list, *, value_label: str = "Department",
                    info_cols: list, stage_label: str = "Stage decisions", title: str = "",
                    suggestion_col: str | None = None, fill_word: str = "suggested", show_stage: bool = True) -> None:
    """Every row starts blank. The person's work-in-progress (their draft)
    lives in session state, so filtering, sorting and the bulk tools never
    lose it. Runs as a fragment: picking from a cell's dropdown, pasting or
    drag-filling refreshes only this grid (the counts stay live), and each
    edit or button is one step on the top-bar Undo."""
    state = st.session_state[f"wb_{key}"]
    toast = st.session_state.pop(f"_wb_toast_{key}", None)
    if toast:
        st.toast(toast[0])
    upcs = items["UPC"].tolist()
    live = set(upcs)
    # Kept for items that left the grid (staged), so undoing the stage
    # brings them back filled in exactly as they were.
    draft = state["draft"]
    v = state["ver"]

    f1, f2 = st.columns([3, 1.2])
    state["filter"] = f1.text_input(
        "Filter", value=state["filter"], key=f"wb_filter_{key}_{v}", label_visibility="collapsed",
        placeholder="Filter rows — e.g.  cola   or   coke, pepsi   or   diet cola",
    )
    state["show"] = f2.selectbox("Show", ["All", "Still blank", "Filled in"],
                                 index=["All", "Still blank", "Filled in"].index(state["show"]),
                                 key=f"wb_show_{key}_{v}", label_visibility="collapsed")
    view = items.copy()
    view.insert(0, value_label, view["UPC"].map(lambda u: draft.get(u, "") or ""))
    view.insert(0, "✓", False)
    q = state["filter"].strip()
    if q:
        blob = view[[c for c in info_cols if c in view.columns] + ["UPC"]].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        view = view[blob.map(lambda b: _row_matches(b, q))]
    if state["show"] == "Still blank":
        view = view[view[value_label].astype(str).str.strip() == ""]
    elif state["show"] == "Filled in":
        view = view[view[value_label].astype(str).str.strip() != ""]
    shown = view["UPC"].tolist()
    cell_options = [""] + options + sorted({x for x in view[value_label] if x and x not in options})

    t1, t2, t3 = st.columns([1.6, 1.0, 1.2])
    t4, t6, t5 = st.columns([1.0, 1.0, 2.0])
    pick = t1.selectbox(f"Set {value_label}", [""] + options, key=f"wb_pick_{key}_{v}",
                        label_visibility="collapsed", placeholder=f"Pick a {value_label}…")
    set_checked = t2.button("Set rows", width='stretch', key=f"wb_setchk_{key}_{v}",
                            help=f"Give every checked row the {value_label} picked on the left.")
    set_shown = t3.button(f"Set all {len(shown):,} shown", width='stretch', key=f"wb_setall_{key}_{v}",
                          help=f"Give every row currently shown (after the filter) the {value_label} picked on the left.")
    clear_checked = t4.button("Clear rows", width='stretch', key=f"wb_clear_{key}_{v}", help="Blank out the checked rows.")
    n_filled = sum(1 for u in upcs if not _blank(draft.get(u)))
    clear_all = t6.button("Clear all", width='stretch', key=f"wb_clearall_{key}_{v}", disabled=not n_filled,
                          help="Blank out every row in this grid (not just the ones the filter shows). Undo brings them back.")
    fill_btn = t5.empty()

    edited = st.data_editor(
        view[["✓", value_label] + [c for c in info_cols if c in view.columns]],
        key=f"wb_grid_{key}_{v}", hide_index=True, width='stretch',
        height=min(640, 38 + 35 * max(len(view), 1)),
        disabled=[c for c in info_cols],
        column_config={
            "✓": st.column_config.CheckboxColumn(width="small", help="Tick rows, then use Set rows / Clear rows."),
            value_label: st.column_config.SelectboxColumn(
                width="medium", options=cell_options, required=False,
                help="Click a cell to pick from the list. Select a filled cell and drag its corner handle down to "
                     "copy it, Ctrl+D to fill down, or paste a column of Departments from Excel.",
            ),
        },
    )
    # Whatever was picked, pasted or dragged since the last run is one step.
    before = dict(draft)
    for upc, val in zip(view["UPC"], edited[value_label]):
        draft[upc] = "" if _blank(val) else str(val).strip()
    typed = sum(1 for u in view["UPC"] if (draft.get(u) or "") != (before.get(u) or ""))
    if typed:
        _draft_record(title, f"Edited {typed} cell(s)", {key: (before, draft)})

    # Live counts, after this run's edits.
    sugg = dict(zip(items["UPC"], items[suggestion_col])) if suggestion_col else {}
    fillable = Counter(sugg[u].strip() for u in upcs
                       if _blank(draft.get(u)) and isinstance(sugg.get(u), str) and sugg[u].strip())
    fill_sugg = False
    if suggestion_col:
        n_fill = sum(fillable.values())
        what = " · ".join(f"{d} {c}" for d, c in fillable.most_common(2)) + (" …" if len(fillable) > 2 else "")
        text_ = (f"Fill {n_fill} blank(s) with {fill_word}" + (f": {what}" if fill_word == "suggested" else "")
                 if n_fill else f"No blanks with a {fill_word} department")
        fill_sugg = fill_btn.button(
            text_,
            width='stretch', key=f"wb_fill_{key}_{v}", disabled=not n_fill,
            help=f"Every blank row gets its {fill_word} department.",
        )
    n_valid = sum(1 for u in upcs if match_department(draft.get(u), options))
    n_bad = sum(1 for u in upcs if not _blank(draft.get(u)) and not match_department(draft.get(u), options))
    stage = st.button(f"{stage_label} ({n_valid:,})", type="primary", key=f"wb_stage_{key}_{v}",
                      disabled=not n_valid) if show_stage else False
    st.caption(
        f"{len(view):,} of {len(items):,} row(s) shown · **{n_valid:,} ready to stage**"
        + (f" · :red[{n_bad} not a Department — pick a real one or clear them]" if n_bad else ""),
        help="Sort by a column (e.g. Brand) so similar items sit together · drag a cell's corner handle to fill down · "
             "Ctrl+D fills a selection down · Ctrl+V pastes a column from Excel · the top-bar Undo takes back each step.",
    )

    checked = [u for u, c in zip(view["UPC"], edited["✓"]) if c]
    if not any([set_checked, set_shown, clear_checked, clear_all, fill_sugg, stage]):
        return
    before = dict(draft)
    label = None
    if (set_checked or set_shown) and not pick:
        st.session_state["_toast"] = (f"Pick a {value_label} on the left first.", "")
    elif set_checked and not checked:
        st.session_state["_toast"] = ("Check some rows first (the ✓ column).", "")
    elif set_checked:
        for u in checked:
            draft[u] = pick
        label = f"Set {len(checked)} checked row(s) to {pick}"
    elif set_shown:
        for u in shown:
            draft[u] = pick
        label = f"Set {len(shown)} shown row(s) to {pick}"
    elif clear_checked:
        for u in checked:
            draft[u] = ""
        label = f"Cleared {len(checked)} row(s)"
    elif clear_all:
        n = sum(1 for u in upcs if not _blank(draft.get(u)))
        for u in upcs:
            draft[u] = ""
        label = f"Cleared all {n} row(s)"
    elif fill_sugg:
        n = 0
        for u in upcs:
            if _blank(draft.get(u)) and isinstance(sugg.get(u), str) and sugg[u].strip():
                draft[u] = sugg[u].strip()
                n += 1
        label = f"Filled {n} blank(s) with {fill_word} departments"
    if label:
        _draft_record(title, label, {key: (before, draft)})
    state["ver"] += 1
    if stage:
        st.session_state[f"_wb_stage_{key}"] = {u: match_department(draft[u], options) for u in upcs
                                                if match_department(draft.get(u), options)}
        st.rerun()
    if "_toast" in st.session_state:
        st.session_state[f"_wb_toast_{key}"] = st.session_state.pop("_toast")
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()  # this run wasn't a fragment-only one (e.g. the page itself just reloaded)


def _workbench_frag(*a, **kw) -> None:
    _workbench_body(*a, **kw)
    save_workspace()


_workbench_grid = st.fragment(_workbench_frag)


def render_broken_out_editor(combo_id: int, review: pd.DataFrame, auto: pd.DataFrame, options: list,
                             title: str, file_stem: str) -> dict | None:
    """Both grids of a claimed Broken Out group — items still to decide, and
    auto-decided ones — with ONE Stage button for the whole group (live
    count) and one Excel file for the whole group. Returns
    {upc: department} on the run right after something is staged."""
    ready = st.session_state.pop(f"_bo_stage_{combo_id}", None)
    for k in (f"bo_{combo_id}", f"ba_{combo_id}"):
        st.session_state.setdefault(f"wb_{k}", {"draft": {}, "ver": 0, "filter": "", "show": "All"})
    _broken_out_group(combo_id, review, auto, options, title, file_stem)
    return ready


@st.fragment
def _broken_out_group(combo_id: int, review: pd.DataFrame, auto: pd.DataFrame, options: list,
                      title: str, file_stem: str) -> None:
    bo, ba = f"bo_{combo_id}", f"ba_{combo_id}"
    just_opened = st.session_state.pop(f"_open_bo_{combo_id}", False)  # just started working on it
    with st.expander(f"Items to decide ({len(review):,})", expanded=just_opened and not review.empty,
                     key=f"broken_out_expander_{combo_id}"):
        if review.empty:
            st.caption("Nothing left to decide here.")
        else:
            manual_count = review["Manually Edited By"].notna().sum()
            if manual_count:
                st.caption(f"{manual_count} item(s) have a manual correction on file — see the column below.")
            _workbench_body(bo, review, options, info_cols=["Description", "Brand", "Pack", "Size", "UOM", "Manually Edited By", "UPC"],
                            suggestion_col="Suggested", title=title, show_stage=False)
    if not auto.empty:
        with st.expander(f"Auto-matched items to confirm ({len(auto):,})", key=f"broken_out_auto_expander_{combo_id}"):
            st.caption("Matched automatically (Brand / UPC root / Description). Keep them with **Fill … with auto**, "
                       "or pick a different Department — only rows you fill in are staged.")
            _workbench_body(ba, auto, options, info_cols=["Auto Department", "Decided Via", "Description", "Brand", "Pack", "Size",
                                                          "UOM", "Manually Edited By", "UPC"],
                            suggestion_col="Auto Department", fill_word="auto", title=f"{title} (auto-decided)", show_stage=False)
    ready = {}
    for key, df in ((bo, review), (ba, auto)):
        live = set(df["UPC"]) if not df.empty else set()
        for u, d in st.session_state[f"wb_{key}"]["draft"].items():
            if u in live and match_department(d, options):
                ready[u] = match_department(d, options)
    n_rev = sum(1 for u in ready if not review.empty and u in set(review["UPC"]))
    c1, c2 = st.columns([1.6, 3], vertical_alignment="center")
    if c1.button(f"Stage all {len(ready):,} decision(s) in this group", type="primary", disabled=not ready,
                 key=f"bo_stage_all_{combo_id}", width='stretch'):
        st.session_state[f"_bo_stage_{combo_id}"] = ready
        st.rerun()
    c2.caption(f"{n_rev:,} to decide + {len(ready) - n_rev:,} auto-matched filled in → Pending Changes.")
    grids = []
    if not review.empty:
        grids.append((bo, review, {"Auto Department": {}, "Suggested": dict(zip(review["UPC"], review["Suggested"]))}))
    if not auto.empty:
        grids.append((ba, auto, {"Auto Department": dict(zip(auto["UPC"], auto["Auto Department"])), "Suggested": {}}))
    if grids:
        render_group_excel(grids, options, file_stem, title, stage_key=f"_bo_stage_{combo_id}")
    save_workspace()


def reset_item_workbench(key: str) -> None:
    st.session_state.pop(f"wb_{key}", None)


USE_AUTO = "USE AUTO/SUGGESTED"


def _excel_with_dropdown(df: pd.DataFrame, value_label: str, options: list) -> bytes:
    """An Excel file whose value_label column is a dropdown of the real
    Departments (listed on a second sheet), so it can't be mistyped."""
    from openpyxl.worksheet.datavalidation import DataValidation
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Items", index=False)
        pd.DataFrame({"Department": options}).to_excel(xw, sheet_name="Departments", index=False)
        ws = xw.sheets["Items"]
        col = df.columns.get_loc(value_label) + 1
        letter = ws.cell(row=1, column=col).column_letter
        dv = DataValidation(type="list", formula1=f"=Departments!$A$2:$A${len(options) + 1}", allow_blank=True,
                            showErrorMessage=True, errorTitle="Not a Department",
                            error="Pick a Department from the list.")
        ws.add_data_validation(dv)
        dv.add(f"{letter}2:{letter}{max(len(df), 1) + 1}")
        for i, c in enumerate(df.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 16 if c != "Description" else 40
        ws.freeze_panes = "B2"
    return buf.getvalue()


def render_group_excel(grids: list, options: list, file_stem: str, title: str, stage_key: str | None = None) -> None:
    """Work on a whole group in Excel instead: one file with every item —
    the ones still to decide and the auto-decided ones (marked with their
    auto department) — and a dropdown Department column holding whatever's
    in the grids now. Uploading it puts the Departments back into the
    right grid, as one step the top-bar Undo can take back.

    grids: [(grid key, items DataFrame with UPC + display columns, extra
    {column: values-by-UPC})]."""
    with st.expander("Prefer Excel? Download this whole group, fill it in there, upload it back"):
        st.caption(f"Every item in the group, with what's in the grids now. Pick a Department in the dropdown column "
                   f"(or **{USE_AUTO}** to keep a row's own auto/suggested one), then upload it back.")
        rows, where = [], {}
        for key, items, extra in grids:
            state = st.session_state.setdefault(f"wb_{key}", {"draft": {}, "ver": 0, "filter": "", "show": "All"})
            for r in items.to_dict("records"):
                where[r["UPC"]] = key
                rows.append({"UPC": r["UPC"], "Description": r.get("Description"), "Brand": r.get("Brand"),
                             "Pack": r.get("Pack"), "Size": r.get("Size"), "UOM": r.get("UOM"),
                             **{c: vals.get(r["UPC"]) for c, vals in extra.items()},
                             "Department": state["draft"].get(r["UPC"], "") or ""})
        if not rows:
            st.caption("Nothing left in this group.")
            return
        out = pd.DataFrame(rows)
        st.download_button("Download as Excel", _excel_with_dropdown(out, "Department", [USE_AUTO] + options),
                           file_name=f"{file_stem}.xlsx", key=f"grp_dl_{file_stem}",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        ver = st.session_state.get(f"_grp_up_ver_{file_stem}", 0)
        pkey = f"_grp_file_{file_stem}"
        pending = st.session_state.get(pkey)
        if pending is None:
            up = st.file_uploader("Upload it back", type=["xlsx", "csv"], key=f"grp_up_{file_stem}_{ver}")
            if up is None:
                return
            pending = {"stem": file_stem, "name": up.name, "bytes": up.getvalue()}
            st.session_state[pkey] = pending
        fc1, fc2 = st.columns([4, 1], vertical_alignment="center")
        fc1.markdown(f"**{pending['name']}** — uploaded, not used yet")
        if fc2.button("Remove file", key=f"grp_rm_{file_stem}", width='stretch'):
            st.session_state.pop(pkey, None)
            st.session_state[f"_grp_up_ver_{file_stem}"] = ver + 1
            st.rerun()

        class _Up(io.BytesIO):
            name = pending["name"]
        up = _Up(pending["bytes"])
        try:
            df = pd.read_csv(up, dtype=str) if up.name.lower().endswith(".csv") else pd.read_excel(up, dtype=str)
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")
            return
        cols = {c.strip().lower(): c for c in df.columns}
        if "upc" not in cols or "department" not in cols:
            st.error("The file needs its UPC and Department columns.")
            return
        auto_of = {}
        for _key, _items, extra in grids:
            for u in _items["UPC"]:
                a_ = (extra.get("Auto Department") or {}).get(u) or (extra.get("Suggested") or {}).get(u)
                if isinstance(a_, str) and a_.strip():
                    auto_of[u] = a_.strip()
        got = {}
        for u, d in zip(df[cols["upc"]], df[cols["department"]]):
            u, d = clean_upc(u), ("" if _blank(d) else str(d).strip())
            if d.upper() == USE_AUTO:
                d = auto_of.get(u, USE_AUTO)
            got[u] = d
        matched = {u: d for u, d in got.items() if u in where}
        filled = {u: match_department(d, options) for u, d in matched.items() if match_department(d, options)}
        bad = {u: d for u, d in matched.items() if d and not match_department(d, options)}
        n_diff = sum(1 for u, d in filled.items() if d != (st.session_state[f"wb_{where[u]}"]["draft"].get(u) or ""))
        st.caption(f"{len(filled):,} row(s) have a Department, {n_diff:,} differ from the grids"
                   + (f"; {len(got) - len(matched):,} UPC(s) aren't in this group and are ignored" if len(got) > len(matched) else "")
                   + (f"; :red[{len(bad)} aren't a real Department (or have no auto/suggested one) and are skipped]" if bad else "")
                   + ".")

        def _into_grids() -> dict:
            st.session_state.pop(pkey, None)
            changes = {}
            for key in {where[u] for u in filled}:
                state = st.session_state[f"wb_{key}"]
                before = dict(state["draft"])
                for u, d in filled.items():
                    if where[u] == key:
                        state["draft"][u] = d
                changes[key] = (before, dict(state["draft"]))
                state["ver"] += 1
            _draft_record(title, f"Filled {n_diff} row(s) from {up.name}", changes, file=pending)
            st.session_state[f"_grp_up_ver_{file_stem}"] = ver + 1

        b1, b2 = st.columns(2)
        if stage_key and b1.button(f"Stage these {len(filled):,} decision(s)", type="primary", key=f"grp_stage_{file_stem}",
                                   disabled=not filled, width='stretch',
                                   help="Straight to Pending Changes. Undo brings this file back with these same choices."):
            st.session_state.pop(pkey, None)
            st.session_state[f"_grp_up_ver_{file_stem}"] = ver + 1
            st.session_state[stage_key] = dict(filled)
            # remembered so that undoing this stage brings the file back
            st.session_state[f"_excel_staging_{stage_key}"] = {**pending, "staged": False}
            st.rerun()
        if (b2 if stage_key else b1).button(f"Put them into the grids ({n_diff:,} change(s))", key=f"grp_apply_{file_stem}",
                                             disabled=not n_diff, width='stretch'):
            _into_grids()
            st.session_state["_toast"] = (f"Put {n_diff} Department(s) from {up.name} into the grids — Undo takes it back.", "")
            st.rerun()


# ===========================================================================
# Popups: admin override, Undo…, send back, break out
# ===========================================================================

def open_dept_dialog(key: str, info: dict) -> None:
    """Streamlit allows one open dialog per run, and closing one with X/Esc
    doesn't clear its session key on its own — so opening any Department
    Review popup clears every other one first (see each dialog's
    on_dismiss for the X/Esc side)."""
    for k in DEPT_DIALOG_KEYS:
        st.session_state.pop(k, None)
    st.session_state[key] = info
    if not st.session_state.pop("_dialog_from_click", False):
        st.rerun()  # (from a button's on_click there's no need: the click's own run opens it)


def show_dept_dialog() -> bool:
    """Shows whichever Department Review popup is open (one per run)."""
    if st.session_state.get("dept_confirm_send_back") is not None:
        confirm_send_back_dialog()
    elif st.session_state.get("dept_confirm_break_out") is not None:
        if st.session_state["dept_confirm_break_out"].get("reopen"):
            confirm_reopen_dialog()
        else:
            confirm_break_out_dialog()
    elif st.session_state.get("dept_undo_picker") is not None:
        undo_picker_dialog()
    elif st.session_state.get("dept_admin_override") is not None:
        admin_override_dialog()
    elif st.session_state.get("dept_topbar_step") is not None:
        topbar_step_dialog()
    else:
        return False
    return True


@st.fragment
def _popup_button_frag(label: str, key: str, on_press, kw: dict) -> None:
    if st.button(label, key=key, **kw):
        st.session_state["_dialog_from_click"] = True
        try:
            on_press()  # sets up the popup (see open_dept_dialog)
        finally:
            st.session_state.pop("_dialog_from_click", None)
        show_dept_dialog()


def popup_button(container, label: str, key: str, on_press, **kw) -> None:
    """A button that opens a Department Review popup without reloading the
    page: only this button reruns, and the popup opens in that same run.
    on_press must not depend on loop variables (bind them: functools.partial)."""
    kw.setdefault("width", "stretch")
    with container:
        _popup_button_frag(label, key, on_press, kw)


def open_on_click(fn):
    """A button's on_click that opens a popup: it's set before the click's
    run starts, so the popup opens in that one run (no second page reload)."""
    def _cb():
        st.session_state["_dialog_from_click"] = True
        fn()
    return _cb


def _dismiss(key: str):
    def _close():
        st.session_state.pop(key, None)
        st.session_state["_quiet_run"] = True  # closing a popup changes nothing on the page: no skeletons
    return _close


CARD_COLS = [0.35, 4.5, 2.9]  # [checkbox, the group, actions] on every pending-change card


def card_actions(col, undo_key: str = None, on_undo=None, override: dict = None, override_key: str = None):
    """Top row of every card's right side: [its other action][↩ Undo…] —
    Undo is always the top-right button, on every tab. The left slot holds
    🛡️ Override (admins) here; it's returned for a caller's own button."""
    left, right = col.columns([1.55, 1])
    if is_admin and override is not None:
        admin_override_button(left, override, key=override_key)
    if undo_key:
        popup_button(right, "↩ Undo…", undo_key, on_undo)
    return left


def admin_override_button(container, info: dict, key: str) -> None:
    """The one entry point to every admin override — a small button only
    admins ever see, so a non-admin's card has no empty space where admin
    controls would otherwise sit. `info` carries what the popup needs:
    kind ("combo" or "upc_group"), combo_id, label, and for a combo its
    tier/source_key/n_upcs_total."""
    if is_admin:
        popup_button(container, "🛡️ Override", key, partial(open_dept_dialog, "dept_admin_override", info), help="Admin override")


@st.dialog("Admin override", width="large", on_dismiss=_dismiss("dept_admin_override"))
def admin_override_dialog():
    info = st.session_state.get("dept_admin_override")
    if info is None:
        return
    actor = st.session_state["name"]
    combo_id = info["combo_id"]
    departments = load_departments()["department"].tolist()

    def _done(toast):
        clear_dept_suggestion_caches()
        st.session_state.pop("dept_admin_override", None)
        st.session_state["_toast"] = toast
        st.rerun()

    st.markdown(f"**{info['label']}**")
    if info["kind"] == "combo":
        change = load_dept_pending_changes().get(combo_id)
        locked_by = change.get("overridden_by") if change else None
        current = change["department"] if change else None
        if locked_by:
            st.caption(f"Locked by **{locked_by}**'s override at **{current}**. Change it, or unlock it for normal editing.")
        else:
            st.caption(
                "Forces this decision to ANY department immediately, regardless of who's backing it or "
                "the votes on file, and LOCKS it — nobody else can suggest or agree on it again until you "
                "remove the override."
            )
        dept = st.selectbox("Force to department", departments, key=f"admin_override_dialog_dept_{combo_id}")
        c1, c2, c3 = st.columns(3)
        if c3.button("Cancel", width='stretch', key="admin_override_cancel_combo"):
            st.session_state.pop("dept_admin_override", None)
            st.rerun()
        if c1.button("Update override" if locked_by else "Override", type="primary", width='stretch', disabled=dept == current):
            with track(combo_id, info["label"], f"Admin override → {dept}"):
                dept_mapping.admin_override_combo(
                    ENGINE, combo_id, dept, info.get("tier"), info["source_key"], info["raw_label"], info["n_upcs_total"], actor,
                )
            _done((f"Admin override: **{info['raw_label']}** → {dept}.", ""))
        if locked_by and c2.button("Remove override (unlock)", width='stretch'):
            with track(combo_id, info["label"], "Removed admin override"):
                dept_mapping.remove_combo_override(ENGINE, combo_id, actor)
            _done((f"Unlocked **{info['raw_label']}** — open to normal editing again.", ""))
    else:
        items = {u: c for u, c in load_dept_pending_upc_changes().items() if c["combo_id"] == combo_id}
        locked_n = sum(1 for c in items.values() if c.get("overridden_by"))
        st.caption(
            "Set **Override to** on any item to force it to that department and lock it (nobody else can "
            "change it until it's unlocked), or tick **Unlock** to release one. Paste or drag-fill down the "
            "column to set several at once."
        )
        grid = st.data_editor(
            pd.DataFrame([
                {
                    "UPC": u, "Description": c.get("description"), "Department": c["department"],
                    "Owner": c.get("staged_by") or "", "Locked": bool(c.get("overridden_by")),
                    "Override to": "", "Unlock": False,
                }
                for u, c in items.items()
            ]),
            hide_index=True, width='stretch', height=min(360, 40 + 35 * len(items)),
            key=f"admin_override_grid_{combo_id}",
            disabled=["UPC", "Description", "Department", "Owner", "Locked"],
            column_config={
                "Override to": st.column_config.SelectboxColumn(options=[""] + departments, required=False),
                "Unlock": st.column_config.CheckboxColumn(),
            },
        )
        to_override = {r["UPC"]: r["Override to"] for _, r in grid.iterrows() if r["Override to"] and not pd.isna(r["Override to"])}
        to_unlock = [r["UPC"] for _, r in grid.iterrows() if r["Unlock"] and r["Locked"] and r["UPC"] not in to_override]
        a1, a2 = st.columns(2)
        if a2.button("Cancel", width='stretch', key="admin_override_cancel_group"):
            st.session_state.pop("dept_admin_override", None)
            st.rerun()
        if a1.button(
            f"Apply ({len(to_override)} override(s), {len(to_unlock)} unlock(s))", type="primary",
            disabled=not (to_override or to_unlock), width='stretch',
        ):
            with st.spinner("Applying..."):
                with track(combo_id, info["label"], f"Admin: {len(to_override)} item override(s), {len(to_unlock)} unlock(s)"):
                    for upc, dept in to_override.items():
                        c = items[upc]
                        dept_mapping.admin_override_upc(
                            ENGINE, upc, dept, combo_id, c["label"], c.get("source_key"), c.get("description"), actor,
                        )
                    for upc in to_unlock:
                        dept_mapping.remove_upc_override(ENGINE, upc, actor)
            _done((f"Admin: {len(to_override)} item(s) overridden, {len(to_unlock)} unlocked in **{info['raw_label']}**.", ""))

        with st.expander(f"Whole group at once ({len(items):,} item(s))"):
            dept = st.selectbox("Force every item to", departments, key=f"admin_override_dialog_group_dept_{combo_id}")
            c1, c2 = st.columns(2)
            if c1.button("Override whole group", width='stretch'):
                with track(combo_id, info["label"], f"Admin override: whole group → {dept}"):
                    n = dept_mapping.admin_override_upc_group(ENGINE, combo_id, dept, actor)
                _done((f"Admin override: {n} item(s) in **{info['raw_label']}** → {dept}.", ""))
            if locked_n and c2.button(f"Unlock all {locked_n}", width='stretch'):
                with track(combo_id, info["label"], "Admin: unlocked whole group"):
                    n = dept_mapping.remove_upc_override_group(ENGINE, combo_id, actor)
                _done((f"Unlocked {n} item(s) in **{info['raw_label']}**.", ""))


def open_undo_picker(combo_id: int, label: str) -> None:
    st.session_state.pop(f"_undo_hits_{combo_id}", None)
    open_dept_dialog("dept_undo_picker", {"combo_id": combo_id, "label": label})


@st.dialog("Undo", width="large", on_dismiss=_dismiss("dept_undo_picker"))
def undo_picker_dialog():
    """Every Undo button lands here. Lists each point this combo can be
    walked back to — just its staged decisions, or back through each
    unpushed Break Out/Send Back move — and spells out exactly what each
    choice discards before anything happens."""
    info = st.session_state.get("dept_undo_picker")
    if info is None:
        return
    combo_id = info["combo_id"]
    actor = st.session_state["name"]
    path = dept_mapping.get_combo_undo_path(ENGINE, combo_id)

    seen_key = f"_undo_seen_{combo_id}"

    def _close():
        st.session_state.pop("dept_undo_picker", None)
        st.session_state.pop(seen_key, None)
        st.rerun()

    st.markdown(f"**{info['label']}**")
    if not path:
        st.info("This group no longer exists.")
        if st.button("Close"):
            _close()
        return
    staged_bits = []
    if path["staged"]["combo_decision"]:
        staged_bits.append(f"the staged decision {path['staged']['combo_decision']}")
    if path["staged"]["combo_votes"]:
        staged_bits.append(f"{path['staged']['combo_votes']} dispute vote(s)")
    if path["staged"]["upc_items"]:
        staged_bits.append(f"{path['staged']['upc_items']:,} staged item decision(s)")
    if path["staged"]["upc_suggestions"]:
        staged_bits.append(f"{path['staged']['upc_suggestions']:,} item suggestion(s)")
    staged_text = ("discards " + ", ".join(staged_bits)) if staged_bits else ""

    now = f"Now: **{path['current']}**"
    if staged_bits:
        now += " · with " + ", ".join(staged_bits)
    st.markdown(now)

    # One row per point it can go back to, newest first — each row says
    # what it lands on and exactly what it throws away, with its own button.
    options = []
    if path["has_staged"]:
        options.append((path["current"], 0, staged_text))
    undone = []
    for i, m in enumerate(path["moves"]):
        when = pd.to_datetime(m["created_at"], errors="coerce")
        when = when.strftime("%m/%d %H:%M") if pd.notna(when) else ""
        undone.append(f"{m['description']} ({m['created_by']}" + (f", {when}" if when else "") + ")")
        what = " · ".join(b for b in (staged_text, "undoes " + " and ".join(undone)) if b)
        options.append((m["before"], i + 1, what))
    if not options:
        st.info("Nothing to undo — nothing is staged on this group and it has no unpushed moves.")
        if st.button("Close"):
            _close()
        return

    # What this popup showed the LAST time it was drawn — i.e. what the
    # person actually looked at before clicking. The undo only runs if the
    # group is still exactly that (see undo_combo_to_stage).
    now_seen = (
        [m["move_id"] for m in path["moves"]],
        dept_mapping._redo_state_key(dept_mapping.get_combo_snapshot(ENGINE, combo_id)),
    )
    seen = st.session_state.get(seen_key) or now_seen
    st.session_state[seen_key] = now_seen

    can_execute = is_admin or not path["has_staged"] or actor == path["authorized"]
    st.caption("Undo back to:" if can_execute else "Points this can be undone back to:")
    current_snap = dept_mapping.get_combo_snapshot(ENGINE, combo_id)
    for i, (target, n_moves, what) in enumerate(options):
        snap = current_snap if n_moves == 0 else path["moves"][n_moves - 1]["snapshot"]
        c1, c2 = st.columns([4, 1.1], vertical_alignment="center")
        c1.markdown(f"**{target}**")
        c1.caption(what[:1].upper() + what[1:])
        auto_mode = "as_was"
        if can_execute and (snap.get("combo") or {}).get("decision_state") in ("broken_out", "decided_broken_out"):
            # Landing in Broken Out: same choice as a Break Out — keep the
            # automatic item decisions it had, re-run auto-matching, or none.
            hk = f"_undo_hits_{combo_id}"
            if hk not in st.session_state:
                with st.spinner("Checking what auto-matching can decide..."):
                    st.session_state[hk] = dept_mapping.compute_upc_decisions_for_combo(ENGINE, combo_id, ignore_own=True)
            n_auto, n_people = dept_mapping.auto_counts(snap.get("overrides"))
            # a "stay" point (n_moves 0) discards the staged work anyway
            n_staged = 0 if n_moves == 0 else len((snap.get("staged") or {}).get("dept_mapping_pending_upc_changes") or [])
            n_hits = len(st.session_state[hk])
            kept = [f"{n_staged:,} staged decision(s)"] if n_staged else []
            kept += [f"{n_auto:,} auto-matched"] if n_auto else []
            kept += [f"{n_people:,} decided earlier"] if n_people else []
            choices = {"as_was": "Everything as it was" + (f" — {', '.join(kept)}" if kept else "")}
            if n_staged and n_auto:
                choices["auto_only"] = f"Only the auto-matched ({n_auto:,}) — drop the staged decisions"
            if n_hits and n_hits != n_auto:
                choices["rerun"] = f"Re-run auto-matching now ({n_hits:,})" + (" — drop the staged decisions" if n_staged else "")
            if kept:
                choices["blank"] = "All blank — every item undecided"
            if len(choices) > 1:
                auto_mode = c1.radio(
                    "Bring back", list(choices), format_func=choices.get, key=f"undo_auto_{combo_id}_{n_moves}",
                    help="Everything as it was: every item decision the group had — staged ones included — exactly. "
                         "Only the auto-matched: just what auto-matching filled in. All blank: start the group over.",
                )
        if can_execute and c2.button("Confirm undo", key=f"undo_to_{combo_id}_{n_moves}", width='stretch',
                                     type="primary" if i == 0 else "secondary"):
            with st.spinner("Undoing..."):
                with track(combo_id, info["label"], f"Undid to {target.removeprefix('Stay in ')}"
                           + {"as_was": "", "auto_only": " (auto-matched only)", "rerun": " (auto-matching re-run)",
                              "blank": " (all items blank)"}[auto_mode]):
                    ok = dept_mapping.undo_combo_to_stage(
                        ENGINE, combo_id, n_moves, actor, expected_move_ids=seen[0], expected_state=seen[1],
                    )
                    if ok and auto_mode != "as_was":
                        dept_mapping.set_broken_out_auto(ENGINE, combo_id, auto_mode)
            st.session_state.pop(f"_undo_hits_{combo_id}", None)
            if not ok:
                st.session_state["_toast"] = (
                    f"**{info['label']}** changed while you had this open (someone else worked on it) — "
                    "nothing was undone. Here's where it stands now.",
                    "",
                )
                clear_dept_suggestion_caches()
                st.rerun()
            load_dept_recent_moves.clear()
            load_broken_out_claims.clear()
            clear_dept_suggestion_caches()
            clear_dept_review_caches()
            st.session_state["_toast"] = (f"Undone: **{info['label']}** — now {target.removeprefix('Stay in ')}.", "")
            _close()

    if not can_execute:
        st.info(
            f"Discarding the staged work here is **{path['authorized']}**'s call (or an admin's). "
            "Requesting lets them know you think it should be undone."
        )
        if st.button("Request undo", type="primary"):
            if path["staged"]["combo_decision"] or path["staged"]["combo_votes"]:
                dept_mapping.request_undo_combo(ENGINE, combo_id, actor, is_admin=False)
            else:
                dept_mapping.request_undo_upc_group_all(ENGINE, combo_id, actor, is_admin=False)
            clear_dept_suggestion_caches()
            st.session_state["_toast"] = (f"Asked **{path['authorized']}** to undo **{info['label']}**.", "")
            _close()


def summarize_combo_decisions(combo_id: int) -> list:
    """Plain-English lines describing every decision currently attached to
    a Broken Out / Decided-Broken-Out combo — both committed (in the
    database) and staged-but-not-pushed — for the "you're about to lose
    this" confirmation before an immediate Send Back. Empty list means
    nothing would actually be lost, so no confirmation is needed at all."""
    lines = []
    counts = dept_mapping.get_decision_counts_for_combo(ENGINE, combo_id)
    auto_n = sum(n for via, n in counts if via.startswith("Auto-Applied"))
    manual_n = sum(n for via, n in counts if via == "Manually Reviewed")
    if auto_n:
        lines.append(f"{auto_n:,} item(s) already auto-decided (Brand/UPC Root/Description Match)")
    if manual_n:
        lines.append(f"{manual_n:,} item(s) already manually decided")
    pending_upc_changes = load_dept_pending_upc_changes()
    staged_n = sum(1 for c in pending_upc_changes.values() if c["combo_id"] == combo_id)
    if staged_n:
        lines.append(f"{staged_n:,} item(s) staged here, not yet pushed to the database")
    return lines


def perform_send_back(
    combo_id: int, source_key: str, label: str, n_upcs_total: int, send_back_label: str, is_whole: bool,
) -> None:
    with track(combo_id, f"{source_key.upper()} — {label}", send_back_label):
        snapshot = dept_mapping.get_combo_snapshot(ENGINE, combo_id)
        if is_whole:
            dept_mapping.revert_combo(ENGINE, combo_id, st.session_state["name"])
        else:
            dept_mapping.revert_broken_out_combo(ENGINE, combo_id, st.session_state["name"])
        # Cleared BEFORE recording: if this move turns out to be a round trip,
        # recording it restores whatever was staged at the earlier point.
        clear_pending_for_combo(combo_id)
        record_recent_move(combo_id, source_key, label, n_upcs_total, send_back_label, snapshot)
    clear_dept_review_caches()
    st.session_state.pop("dept_confirm_send_back", None)
    st.session_state["_toast"] = (f"{send_back_label}: **{label}**", "")
    st.rerun()


@st.dialog("Send back to review?", on_dismiss=_dismiss("dept_confirm_send_back"))
def confirm_send_back_dialog():
    info = st.session_state.get("dept_confirm_send_back")
    if info is None:
        return
    st.markdown(f"**{info['source_key'].upper()}** — {info['label']}")
    st.markdown(f"**Now:** {info['current']} &nbsp;→&nbsp; **After:** {info['target']}")
    if info["decision_lines"]:
        st.warning(
            "Sending it back completely leaves it with **no decisions at all** until someone decides it "
            "again — this discards:"
        )
        for line in info["decision_lines"]:
            st.markdown(f"- {line}")
    elif info["current"].startswith("Decided as"):
        st.warning(
            f"Sending it back completely removes its decision (**{info['current'][len('Decided as '):]}**) — "
            "it will have **no decision** until someone decides it again. To just change the department "
            "instead, use **Change department** on its card; that goes through Pending Changes."
        )
    else:
        st.caption("Nothing decided on it yet, so nothing is lost.")
    st.caption("Undoable afterward from Pending Changes → Recent moves, back to any earlier step.")
    c1, c2 = st.columns(2)
    if c1.button("Send it back", type="primary", width='stretch'):
        perform_send_back(
            info["combo_id"], info["source_key"], info["label"], info["n_upcs_total"],
            info["send_back_label"], info["is_whole"],
        )
    if c2.button("Cancel", width='stretch'):
        st.session_state.pop("dept_confirm_send_back", None)
        st.rerun()


def request_send_back(
    combo_id: int, source_key: str, label: str, n_upcs_total: int, send_back_label: str, is_whole: bool,
) -> None:
    """Entry point for every Send Back button — always confirms first,
    showing where the combo is now, where it'll land, and anything that
    gets discarded on the way."""
    decision_lines = [] if is_whole else summarize_combo_decisions(combo_id)
    path = dept_mapping.get_combo_undo_path(ENGINE, combo_id)
    open_dept_dialog("dept_confirm_send_back", {
        "combo_id": combo_id, "source_key": source_key, "label": label,
        "n_upcs_total": n_upcs_total, "send_back_label": send_back_label,
        "is_whole": is_whole, "decision_lines": decision_lines,
        "current": path.get("current", "?"),
        "target": dept_mapping.describe_combo_state({"decision_state": "not_reviewed"}, path.get("tier")),
    })


def perform_break_out(
    combo_id: int, source_key: str, label: str, n_upcs_total: int, upc_decisions: dict, reopen: bool = False,
) -> None:
    with st.spinner(f"Breaking out {n_upcs_total:,} item(s)…"), track(
        combo_id, f"{source_key.upper()} — {label}", "Sent back to Broken Out" if reopen else "Broke out to item level",
    ):
        snapshot = dept_mapping.get_combo_snapshot(ENGINE, combo_id)
        if reopen:
            dept_mapping.reopen_broken_out(ENGINE, combo_id, st.session_state["name"], upc_decisions=upc_decisions)
            clear_pending_for_combo(combo_id)
            load_decided_combos.clear()
        else:
            dept_mapping.break_out_combo(ENGINE, combo_id, st.session_state["name"], upc_decisions=upc_decisions)
        record_recent_move(
            combo_id, source_key, label, n_upcs_total,
            "Send Back to Broken Out" if reopen else "Broken Out to UPC-Level", snapshot,
        )
    load_dept_review_queue.clear()
    load_broken_out_combos.clear()
    st.session_state.pop("dept_confirm_break_out", None)
    st.session_state["_toast"] = (f"Moved to Broken Out: **{label}**", "")
    st.rerun()


@st.dialog("Break out?", on_dismiss=_dismiss("dept_confirm_break_out"))
def confirm_break_out_dialog():
    _break_out_dialog_body()


@st.dialog("Send back to Broken Out?", on_dismiss=_dismiss("dept_confirm_break_out"))
def confirm_reopen_dialog():
    _break_out_dialog_body()


def _break_out_dialog_body():
    info = st.session_state.get("dept_confirm_break_out")
    if info is None:
        return
    n_hits = len(info["upc_decisions"])
    reopen = info.get("reopen", False)
    st.markdown(f"**{info['source_key'].upper()}** — {info['label']}")
    st.markdown(f"**Now:** {info['current']} &nbsp;→&nbsp; **After:** Broken Out ({info['n_upcs_total']:,} item(s), decided one at a time)")
    if reopen:
        st.warning(
            "Every one of its current item decisions is cleared — its items will have **no decisions** "
            "until they're decided again on Broken Out:"
        )
        for line in info.get("decision_lines", []):
            st.markdown(f"- {line}")
    st.caption("Undoable afterward from Pending Changes → Recent moves, back to any earlier step.")

    def _go(decisions):
        perform_break_out(info["combo_id"], info["source_key"], info["label"], info["n_upcs_total"], decisions, reopen=reopen)

    def _cancel():
        st.session_state.pop("dept_confirm_break_out", None)
        st.rerun()

    action = "Send it back" if reopen else "Break it out"
    # Action button(s) and Cancel always sit side by side in one row.
    if reopen and info.get("fully_auto"):
        # Every item here was already auto-decided — re-applying automation
        # would just decide the whole group again, straight back to Decided.
        st.info(
            "Every item in this group was auto-decided, so re-applying auto-matching would just decide it all "
            "again. It goes back with every item blank, for a person to decide."
        )
        c1, c2 = st.columns(2)
        if c1.button(action, type="primary", width='stretch'):
            _go({})
        if c2.button("Cancel", width='stretch'):
            _cancel()
    elif n_hits:
        st.markdown(
            f"{n_hits:,} of {info['n_upcs_total']:,} item(s) could be auto-decided right now via Brand/UPC "
            "Root/Description Match — the same matching the engine itself runs. Start with those already "
            "filled in, or leave every item blank so you decide each one yourself?"
        )
        c1, c2, c3 = st.columns(3)
        if c1.button("Apply auto-decisions", type="primary", width='stretch'):
            _go(info["upc_decisions"])
        if c2.button("Start blank", width='stretch'):
            _go({})
        if c3.button("Cancel", width='stretch'):
            _cancel()
    else:
        st.caption("None of its items can be auto-matched right now, so every item starts blank.")
        c1, c2 = st.columns(2)
        if c1.button(action, type="primary", width='stretch'):
            _go({})
        if c2.button("Cancel", width='stretch'):
            _cancel()


@st.cache_resource(show_spinner=False)
def _break_out_cache_warmer() -> dict:
    return {"thread": None}


def warm_break_out_cache() -> None:
    """Prepares Break Out's auto-match data in the background as soon as
    someone opens a tab with Break Out on it, so the click itself is
    usually instant. At most one warm-up runs at a time per server; if the
    data is already current it's a quick no-op check."""
    state = _break_out_cache_warmer()
    t = state["thread"]
    now = time.monotonic()
    # Tabs rerun on nearly every click — don't re-check the database each time.
    if (t is not None and t.is_alive()) or now - state.get("started", -1e9) < 300:
        return
    t = threading.Thread(target=dept_mapping.get_prepared_engine_data, args=(ENGINE,), daemon=True)
    state.update(thread=t, started=now)
    t.start()


def request_break_out(
    combo_id: int, source_key: str, label: str, n_upcs_total: int, reopen: bool = False, fully_auto: bool = False,
) -> None:
    """Entry point for every Break Out button — computes whether any
    per-UPC auto-matching could decide something for this combo right now
    (see dept_mapping.compute_upc_decisions_for_combo); always confirms
    first, and when there ARE hits, asks which starting point to use
    instead of silently picking one."""
    with st.spinner(
        "Checking which items can be auto-matched (Brand / UPC Root / Description)… "
        "the first check after new data is loaded can take up to ~15 seconds."
    ):
        upc_decisions = (
            {} if fully_auto else dept_mapping.compute_upc_decisions_for_combo(ENGINE, combo_id, ignore_own=reopen)
        )
    open_dept_dialog("dept_confirm_break_out", {
        "combo_id": combo_id, "source_key": source_key, "label": label,
        "n_upcs_total": n_upcs_total, "upc_decisions": upc_decisions,
        "current": dept_mapping.get_combo_undo_path(ENGINE, combo_id).get("current", "?"),
        "reopen": reopen, "fully_auto": fully_auto,
        "decision_lines": summarize_combo_decisions(combo_id) if reopen else [],
    })


# ===========================================================================
# Notifications and the app error log
# ===========================================================================

def upc_exists(upc: str) -> bool:
    with db_connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM dbo.items WHERE upc = :upc"), {"upc": upc}
        ).first() is not None


class _BackgroundRefreshed:
    """A per-server cache that never makes a click wait for a routine
    refresh: past `ttl` seconds the current value is still returned at once
    while a background thread fetches the new one (used on the next click).
    .clear() (after your own actions) makes the next call fetch right away,
    exactly like st.cache_data's clear."""

    def __init__(self, fn, ttl: int):
        self.fn, self.ttl = fn, ttl
        self.entries, self.lock = {}, threading.Lock()

    def __call__(self, *args):
        now = time.monotonic()
        with self.lock:
            e = self.entries.get(args)
            if e is not None and now - e["at"] > self.ttl and not e["refreshing"]:
                e["refreshing"] = True
                threading.Thread(target=self._refresh, args=(args,), daemon=True).start()
        if e is None:
            value = self.fn(*args)
            with self.lock:
                self.entries[args] = {"value": value, "at": time.monotonic(), "refreshing": False}
            return value
        return e["value"]

    def _refresh(self, args):
        try:
            value = self.fn(*args)
            with self.lock:
                self.entries[args] = {"value": value, "at": time.monotonic(), "refreshing": False}
        except Exception:
            with self.lock:
                if args in self.entries:
                    self.entries[args]["refreshing"] = False

    def clear(self):
        with self.lock:
            self.entries.clear()


@st.cache_resource(show_spinner=False)
def _notifications_cache() -> _BackgroundRefreshed:
    return _BackgroundRefreshed(lambda username, since, admin=False: dept_mapping.get_notifications(ENGINE, username, since, admin), ttl=60)


load_notifications = _notifications_cache()


def _notif_since() -> "datetime":
    """This user's "last visit" point, fixed for the whole session — read
    (and the visit recorded) once per login, not on every click."""
    name = st.session_state["name"]
    per_user = st.session_state.setdefault("_notif_since", {})
    if name not in per_user:
        per_user[name] = dept_mapping.start_visit(ENGINE, name)
    return per_user[name]


def _open_notification(tab: str, search: str) -> None:
    # A button callback runs before any widget is drawn, so the tab
    # selectors and search box can be set here directly.
    st.session_state["active_tab"] = "Department Review"
    st.session_state["dept_review_subtab"] = tab
    get_shared_dept_filter()["search"] = search
    tab_key = {"Pending Changes": "pending_changes", "Decided": "decided", "Broken Out": "broken_out"}.get(tab)
    if tab_key:
        get_dept_tab_filters()[tab_key] = {"search": search}


def _mark_all_read() -> None:
    name = st.session_state["name"]
    st.session_state.setdefault("_notif_since", {})[name] = dept_mapping.mark_seen(ENGINE, name)
    load_notifications.clear()


@st.cache_data(ttl=60, show_spinner=False)
def load_last_seen(username: str):
    return dept_mapping.get_last_seen(ENGINE, username)


@st.cache_data(ttl=60, show_spinner=False)
def load_app_errors() -> list:
    return dept_mapping.list_app_errors(ENGINE)


def _resolve_app_errors(ids: list) -> None:
    dept_mapping.resolve_app_errors(ENGINE, ids, st.session_state["name"])
    load_app_errors.clear()


def render_app_errors() -> None:
    """Admins: unexpected errors people hit (they only saw a short
    message) — who, where, and the full details, until marked fixed."""
    errors = load_app_errors()
    if not errors:
        return
    st.markdown(f"**App errors ({len(errors)})**")
    st.caption("Unexpected errors people hit. They only saw a short message; the full details are here.")
    for e in errors[:10]:
        with st.container(border=True):
            when = pd.to_datetime(e["occurred_at"]).tz_localize("UTC").tz_convert("America/Los_Angeles")
            st.markdown(f"**#{e['error_id']}** · {e['username'] or 'someone'} · {when:%b %d %I:%M %p}")
            st.caption(f"{e['where_in_app'] or 'unknown page'} · {e['error_type']}: {(e['message'] or '')[:160]}")
            with st.expander("Full details"):
                st.code(e["details"] or e["message"] or "", language=None, wrap_lines=True)
            st.button("Mark fixed", key=f"app_error_fix_{e['error_id']}", width='stretch',
                      on_click=_resolve_app_errors, args=([e["error_id"]],))
    if len(errors) > 10:
        st.caption(f"…and {len(errors) - 10} older.")
    st.button(f"Mark all {len(errors)} fixed", key="app_error_fix_all", width='stretch',
              on_click=_resolve_app_errors, args=([e["error_id"] for e in errors],))
    st.divider()


NOTIF_ICONS = {}
NOTIF_ROLLUP = 3  # this many of one kind in a list fold into one card


def render_notifications_sidebar() -> None:
    """What's waiting on you, and what changed on your work since your last
    visit — in the sidebar, out of the way of the page itself. Three or more
    of one kind fold into one card, so a big push or import is one line,
    not fifty. Admins can also see everyone else's, one person at a time."""
    name = st.session_state["name"]
    since = _notif_since()
    notes = load_notifications(name, since, is_admin)
    n_new = sum(1 for n in notes["action"] + notes["updates"] if n["new"])
    kinds = dept_mapping.NOTIFICATION_KINDS

    def _row(n, key):
        badge = ":blue[New] · " if n["new"] else ""
        c1, c2 = st.columns([4, 1.3], vertical_alignment="center")
        c1.markdown(f"{badge}**{n['title']}**  \n:gray[{n['detail']}]")
        c2.button("Open", key=key, width='stretch', help=f"Open it on {n['tab']}",
                  on_click=_open_notification, args=(n["tab"], n["search"]))

    def _card(n, key):
        with st.container(border=True):
            _row(n, key)

    def _rollup(kind, items, key):
        n_new_here = sum(1 for n in items if n["new"])
        with st.container(border=True):
            st.markdown(f"**{len(items)} × {kinds.get(kind, kind)}**"
                        + (f" · {n_new_here} new" if n_new_here else ""))
            st.caption(items[0]["detail"] + (" · and more" if len(items) > 1 else ""))
            with st.expander("Show them"):
                for i, n in enumerate(items):
                    _row(n, f"{key}_{i}")

    def _section(title, items, empty, key):
        st.markdown(f"**{title}**" + (f" ({len(items)})" if items else ""))
        if not items:
            st.caption(empty)
            return
        by_kind = {}
        for n in items:
            by_kind.setdefault(n.get("kind"), []).append(n)
        shown = set()
        for i, n in enumerate(items):  # newest first; a kind folds where its newest one is
            k = n.get("kind")
            if k in shown:
                continue
            if len(by_kind[k]) >= NOTIF_ROLLUP:
                _rollup(k, by_kind[k], f"notif_{key}_{k}")
                shown.add(k)
            else:
                _card(n, f"notif_{key}_{i}")

    def _filter(items, q, kind):
        if kind != "All types":
            items = [n for n in items if kinds.get(n.get("kind")) == kind]
        if q:
            items = [n for n in items if q in n["title"].lower() or q in n["detail"].lower()]
        return items

    with st.sidebar:
        st.markdown("### Notifications" + (f" · {n_new} new" if n_new else ""))
        if is_admin:
            render_app_errors()
        view = "Mine"
        if is_admin:
            view = st.segmented_control(
                "Whose", ["Mine", "Team"], default="Mine", key="notif_view", label_visibility="collapsed",
                help="Team: what's waiting on each other reviewer, one person at a time.",
            ) or "Mine"
        q, kind = "", "All types"
        if view == "Team" or len(notes["action"]) + len(notes["updates"]) > 8:
            q = st.text_input(
                "Search notifications", key="notif_search", placeholder="Search — group, person, or what happened",
                label_visibility="collapsed",
            ).strip().lower()
            kind = st.selectbox("Type", ["All types"] + list(kinds.values()), key="notif_kind", label_visibility="collapsed")

        if view == "Mine":
            action, updates = _filter(notes["action"], q, kind), _filter(notes["updates"], q, kind)
            filtered = (q or kind != "All types")
            _section("Waiting on you", action, "Nothing matches." if filtered else "Nothing is waiting on you.", "action")
            st.divider()
            _section("Since your last visit", updates, "Nothing matches." if filtered else "Nothing new.", "updates")
            if n_new:
                st.button("Mark all as read", key="notif_mark_read", width='stretch', on_click=_mark_all_read,
                          help=f"Clears the marks (new since {pd.Timestamp(since).strftime('%m/%d %H:%M')} UTC)")
            return

        others = [
            v["name"] for v in auth_config["credentials"]["usernames"].values()
            if v.get("role") in ("admin", "editor") and v["name"] != name
        ]
        for person in sorted(others):
            pn = load_notifications(person, load_last_seen(person))
            p_action, p_updates = _filter(pn["action"], q, kind), _filter(pn["updates"], q, kind)
            p_new = sum(1 for n in p_action + p_updates if n["new"])
            label = f"{person} · {len(p_action)} waiting" + (f" · {p_new} new" if p_new else "")
            with st.expander(label):
                if not (p_action or p_updates):
                    st.caption("Nothing for them right now." if not q and kind == "All types" else "Nothing matches.")
                slug = re.sub(r"\W", "_", person)
                if p_action:
                    _section("Waiting on them", p_action, "", f"team_{slug}_action")
                if p_updates:
                    _section("Since their last visit", p_updates, "", f"team_{slug}_updates")


# ===========================================================================
# Top bar (bell, Undo, Redo) and the account box
# ===========================================================================

@st.cache_data(ttl=15, show_spinner=False)
def load_undo_redo(username: str) -> dict:
    return dept_mapping.peek_undo_redo(ENGINE, username)


def next_step(name: str) -> dict:
    """What the top-bar Undo / Redo would act on: your newest step, whether
    it's a saved Department Review action or grid work not staged yet."""
    peek = load_undo_redo(name)
    du = (st.session_state.get("_draft_undo") or [None])[-1]
    dr = (st.session_state.get("_draft_redo") or [None])[-1]
    u, r = peek["undo"], peek["redo"]
    when = lambda t: pd.to_datetime(t.get("changed_at") or t["created_at"])
    undo = ({"draft": True, **du} if du and (u is None or du["at"] > when(u)) else
            ({"draft": False, **u} if u else None))
    redo = ({"draft": True, **dr} if dr and (r is None or dr["undone_at"] > when(r)) else
            ({"draft": False, **r} if r else None))
    return {"undo": undo, "redo": redo}


def _topbar_step(kind: str) -> None:
    # Opens the confirm popup; the popup lives in the Department Review tab.
    st.session_state["active_tab"] = "Department Review"
    for k in DEPT_DIALOG_KEYS:
        st.session_state.pop(k, None)
    st.session_state["dept_topbar_step"] = {"kind": kind}


@st.dialog("Undo / Redo", on_dismiss=_dismiss("dept_topbar_step"))
def topbar_step_dialog():
    """Confirms, then takes back (or puts back) exactly ONE of your own
    Department Review actions — a vote, a staged item change, an override,
    a Break Out / Send Back, an undo. Never a Merge push or anything
    already pushed live (Snapshots cover those)."""
    info = st.session_state.get("dept_topbar_step")
    if info is None:
        return
    undo = info["kind"] == "undo"
    name = st.session_state["name"]
    target = next_step(name)["undo" if undo else "redo"]

    def _close():
        st.session_state.pop("dept_topbar_step", None)
        st.rerun()

    if not target:
        st.info("Nothing to undo." if undo else "Nothing to redo.")
        if st.button("Close"):
            _close()
        return
    if target["draft"]:
        st.markdown(f"**{'Undo' if undo else 'Redo'} your last grid change?**")
        st.markdown(f"**{target['label']}** — {target['where']}")
        st.caption("Not staged yet — this only changes what's filled in on the grid.")
        c1, c2 = st.columns(2)
        if c1.button("Confirm undo" if undo else "Confirm redo", type="primary", width='stretch'):
            src, dst = ("_draft_undo", "_draft_redo") if undo else ("_draft_redo", "_draft_undo")
            entry = st.session_state[src].pop()
            _draft_apply(entry, "before" if undo else "after")
            f = entry.get("file")
            if f:
                if undo:
                    st.session_state[f"_grp_file_{f['stem']}"] = {k: f[k] for k in ("stem", "name", "bytes")}
                else:
                    st.session_state.pop(f"_grp_file_{f['stem']}", None)
            if undo:
                entry["undone_at"] = _utcnow()
            else:
                entry["at"] = _utcnow()
            st.session_state.setdefault(dst, []).append(entry)
            st.session_state["_toast"] = (f"{'Undone' if undo else 'Redone'}: {entry['label']} — **{entry['where']}**.",
                                          "" if undo else "")
            _close()
        if c2.button("Cancel", width='stretch'):
            _close()
        return
    when = pd.to_datetime(target["created_at"], errors="coerce")
    st.markdown(f"**{'Undo' if undo else 'Redo'} your last {'change' if undo else 'undo'}?**")
    st.markdown(f"**{target['description']}** — {target['label']}")
    if pd.notna(when):
        st.caption(f"Made {when.strftime('%m/%d %H:%M')} UTC. "
                   + ("Puts the group back exactly as it was before this." if undo
                      else "Puts the group back exactly as this left it."))
    c1, c2 = st.columns(2)
    if c1.button("Confirm undo" if undo else "Confirm redo", type="primary", width='stretch'):
        with st.spinner("Undoing..." if undo else "Redoing..."):
            result = (dept_mapping.undo_last_action if undo else dept_mapping.redo_last_action)(ENGINE, name)
        load_undo_redo.clear()
        load_dept_recent_moves.clear()
        load_broken_out_claims.clear()
        clear_dept_suggestion_caches()
        clear_dept_review_caches()
        e = result.get("entry") or target
        f = (st.session_state.get("_excel_staged_actions") or {}).get(e.get("action_id"))
        if result["ok"] and f:
            if undo:
                st.session_state[f"_grp_file_{f['stem']}"] = {k: f[k] for k in ("stem", "name", "bytes")}
            else:
                st.session_state.pop(f"_grp_file_{f['stem']}", None)
        if result["ok"]:
            st.session_state["_toast"] = (f"{'Undone' if undo else 'Redone'}: {e['description']} — **{e['label']}**.",
                                          "" if undo else "")
        else:
            st.session_state["_toast"] = (
                f"Can't {'undo' if undo else 'redo'} \"{e['description']}\" on **{e['label']}** — "
                f"{result.get('why') or 'that group has changed since'}, so {'undoing' if undo else 'redoing'} it would overwrite newer work. "
                f"Nothing was changed; that step is skipped{' and your next Undo goes to the one before it' if undo else ''}.",
                "",
            )
        _close()
    if c2.button("Cancel", width='stretch'):
        _close()


def _toggle_notifications() -> None:
    st.session_state["_show_notifications"] = not st.session_state.get("_show_notifications", True)


def _on_logout(_info=None) -> None:
    """Logging out ends this person's undo history and unstaged grid work."""
    name = st.session_state.get("name")
    if name:
        dept_mapping.clear_user_undo(ENGINE, name)
    st.session_state["_logged_out"] = True
    for k in [k for k in st.session_state.keys()
              if k in _WS_KEYS or k.startswith(("_grp_file_", "_ws_"))
              or (k.startswith("wb_") and isinstance(st.session_state[k], dict) and "draft" in st.session_state[k])]:
        del st.session_state[k]
    load_undo_redo.clear()


def render_topbar() -> None:
    """Undo / Redo / notifications, in Streamlit's own top strip: just right
    of the sidebar's >> button when it's closed, and just past the sidebar's
    edge when it's open. Who's signed in and Logout live in the sidebar
    (render_account_box), away from these."""
    with st.container(key="topbar"):
        st.markdown(
            """
            <style>
            /* streamlit-authenticator's login() leaves an empty placeholder
               behind after a successful login that still reserves a line. */
            div.st-key-init { display: none !important; }
            div[data-testid="stLayoutWrapper"]:has(> div.st-key-topbar) { margin-bottom: -2rem !important; }
            div.st-key-topbar {
                position: fixed !important;
                top: 0; left: 3.6rem; right: auto;
                width: fit-content !important;
                height: 60px;
                z-index: 999995;
                display: flex !important;
                flex-direction: row !important;
                align-items: center !important;
                gap: 0.5rem;
            }
            div.st-key-topbar > div[data-testid="stElementContainer"]:has(style) { display: none; }
            div.st-key-topbar > div[data-testid="stElementContainer"] {
                flex: none !important;
                width: fit-content !important;
            }
            div.st-key-topbar button { min-height: 2rem; padding: 0.1rem 0.7rem; }
            body:has(section[data-testid="stSidebar"][aria-expanded="true"]) div.st-key-topbar {
                left: calc(300px + 1rem);
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        if is_reviewer:
            name = st.session_state["name"]
            peek = next_step(name)
            u, r = peek["undo"], peek["redo"]
            describe = lambda t: (f"{t['label']} — {t['where']} (grid, not staged)" if t["draft"]
                                  else f"{t['description']} — {t['label']}")
            notes = load_notifications(name, _notif_since(), is_admin)
            n_new = sum(1 for n in notes["action"] + notes["updates"] if n["new"])
            if is_admin:
                n_new += len(load_app_errors())
            showing = st.session_state.get("_show_notifications", True)
            st.button(
                f"🔔 {n_new}" if n_new else "🔔", key="topbar_bell", on_click=_toggle_notifications,
                type="primary" if n_new else "secondary",
                help=("Hide" if showing else "Show") + " notifications (in the sidebar — open it with >>)",
            )
            st.button(
                "Undo", key="topbar_undo", on_click=_topbar_step, args=("undo",),
                help=(f"Undo: {describe(u)}" if u else "Nothing of yours to undo")
                + ". Department Review actions and grid work — not Merge pushes or anything already pushed live.",
            )
            st.button(
                "Redo", key="topbar_redo", on_click=_topbar_step, args=("redo",),
                help=(f"Redo: {describe(r)}" if r else "Nothing to redo")
                + ". Only if nobody has changed that group since your undo.",
            )


def render_account_box() -> None:
    """Who's signed in, and Logout — top of the sidebar, apart from the
    working buttons so it isn't clicked by accident."""
    with st.sidebar:
        with st.container(border=True, key="account_box"):
            c1, c2 = st.columns([1.35, 1], vertical_alignment="center")
            c1.markdown(f"**{st.session_state['name']}** · :gray[{user_role}]")
            with c2:
                authenticator.logout("Logout", "main", key="logout_btn", callback=_on_logout)
    if st.session_state.pop("_logged_out", False):
        st.rerun()  # straight to the login screen, not the rest of this page


@st.cache_resource(show_spinner=False)
def _data_version() -> dict:
    return {"fp": None}  # what the database looked like at the last check


def refresh_if_changed_elsewhere() -> None:
    """Changes made in the app clear the shared caches right away; changes
    made outside it (the scheduled monthly refresh, a script) don't. One
    cheap query per click spots those and refreshes the cached lists."""
    try:
        with db_connect() as conn:
            fp = tuple(conn.execute(text(
                "SELECT (SELECT COUNT(*) FROM dbo.dept_mapping_pending_changes), "
                "(SELECT MAX(staged_at) FROM dbo.dept_mapping_pending_changes), "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_pending_upc_changes), "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_combo_suggestions), "
                "(SELECT MAX(move_id) FROM dbo.dept_mapping_recent_moves), "
                "(SELECT COUNT(*) FROM dbo.dept_mapping_recent_moves), "
                "(SELECT MAX(last_decided_at) FROM dbo.dept_mapping_combos), "
                "(SELECT MAX(last_computed_at) FROM dbo.dept_mapping_combos), "
                "(SELECT COUNT(*) FROM dbo.item_master_pending_changes), "
                "(SELECT MAX(staged_at) FROM dbo.item_master_pending_changes), "
                "(SELECT MAX(id) FROM dbo.ingestion_log), "
                "(SELECT COUNT(*) FROM dbo.import_choices WHERE status = 'open'), "
                "(SELECT MAX(choice_id) FROM dbo.import_choices)")).one())
    except Exception:
        return
    seen = _data_version()
    if seen.get("fp") != fp:  # (the first check after a restart refreshes too — cheap, and never stale)
        clear_dept_review_caches()
        clear_dept_suggestion_caches()
        load_dept_recent_moves.clear()
        load_item_master_pending.clear()
        load_raw_item_counts.clear()
        load_ingestion_log.clear()
        load_stale_sources.clear()
        load_import_choices.clear()
        load_import_notes.clear()
    seen["fp"] = fp


load_workspace()
refresh_if_changed_elsewhere()
render_account_box()
if is_reviewer:
    render_topbar()
st.title("NWG Item Master App")

if is_reviewer and st.session_state.get("_show_notifications", True):
    render_notifications_sidebar()

tab_names = ["Item Master"]
if is_reviewer:
    tab_names += ["Department Review", "Add Item", "Delete Item", "UPC Overrides", "Pending Changes"]
if is_admin:
    tab_names += ["Sources", "Upload & Ingest", "Merge", "Snapshots"]
if is_reviewer:
    tab_names += ["Activity"]  # editors see everyone's changes too, admins' included

# st.tabs() runs the code inside EVERY tab's `with` block on every rerun,
# not just the visible one — with a 330K-row Item Master query/filter and
# several other DB-backed sections, that meant every click anywhere in the
# app (even an unrelated multiselect on the Sources tab) paid the full cost
# of all six tabs' work. A radio-based selector plus plain `if` blocks only
# runs the code for whichever section is actually being viewed — this CSS
# restyles that radio group to look like st.tabs() again (underline on the
# selected option, no visible radio circle) since data-testid="stRadioOption"
# / data-selected are stable Streamlit attributes, unlike the emotion-cache
# class names elsewhere in the DOM.
inject_css("tabs.css")
nav_request = st.session_state.pop("_nav_to_tab", None)
if nav_request and nav_request in tab_names:
    st.session_state["active_tab"] = nav_request
# The page you're on lives in the address (?tab=…&sub=…, plus its filters
# and page, and the Broken Out group you're working in), so a reload or a
# shared link opens the same place. Still one page, so switching stays instant.
DEPT_PAGE_KEYS = {"Crosswalk": "dept_review_page_num_review", "Unmatched": "dept_review_page_num_unmatched",
                  "Broken Out": "broken_out_page_num", "Decided": "decided_page_num"}
IM_PAGE_SIZES = [100, 250, 500, 1000, 2500, 5000]
RECENT_MOVES_SHOWN = 5  # Pending Changes: newest moved groups shown at the top
IM_DEFAULT_PAGE_SIZE = 1000
IM_GRID_HEIGHT = 760  # about 20 rows on screen at once; scroll the grid for the rest of the page
IM_URL = {"im_dept": "im_dept", "im_brand": "im_brand", "im_source": "im_source", "im_q": "im_search",
          "im_size": "im_page_size", "im_page": "im_page_num"}
if not st.session_state.get("_url_seeded"):
    st.session_state["_url_seeded"] = True
    qp = st.query_params
    f = {"search": qp.get("q", ""), "sort_label": qp.get("sort"), "sort_desc": qp.get("desc") != "0",
         "page_size": int(qp["size"]) if qp.get("size", "").isdigit() else DEFAULT_GROUP_PAGE_SIZE}
    if any(k in qp for k in ("q", "sort", "desc", "size")):
        st.session_state["dept_shared_filter"] = f
    if qp.get("page", "").isdigit() and qp.get("sub") in DEPT_PAGE_KEYS:
        st.session_state[DEPT_PAGE_KEYS[qp["sub"]]] = int(qp["page"])
    if qp.get("group", "").isdigit():
        st.session_state["_url_group"] = int(qp["group"])
    for param, key in IM_URL.items():
        if param in qp:
            st.session_state[key] = int(qp[param]) if param in ("im_size", "im_page") and qp[param].isdigit() else qp[param]
    if qp.get("im_manual") == "1":
        st.session_state["im_manual_only"] = True
if "active_tab" not in st.session_state and st.query_params.get("tab") in tab_names:
    st.session_state["active_tab"] = st.query_params["tab"]
active_tab = st.radio("Section", tab_names, horizontal=True, label_visibility="collapsed", key="active_tab")

# A one-shot corner popup (bottom-right, auto-fading) for actions that make
# a row/group disappear from the list it was just acted on — staging a
# Crosswalk/Unmatched Approve, for instance, immediately removes that group
# from view (it's now in Pending Changes instead), which without this reads
# as "did that even work?". Callers set st.session_state["_toast"] =
# (message, icon) right before st.rerun() instead of calling st.toast()
# directly, so every such confirmation renders from this one place.
_toast = st.session_state.pop("_toast", None)
if _toast:
    st.toast(_toast[0])

# ---------------------------------------------------------------------------
# Item Master (Browse & Edit)
# ---------------------------------------------------------------------------
NO_DEPT = "(no Department)"


def render_item_master_tab() -> None:
    """The Item Master tab (every role)."""
    render_merge_staleness_banner()
    df = load_items()
    # A human correction behind a row (a manual Add, or a manual Edit that
    # overwrote what the pipeline produced) was previously invisible here —
    # Source Key doesn't reveal it for an edited row, since editing keeps
    # whatever source it already had. Vectorized rather than a per-row
    # .apply: this runs against the full, unpaginated item master.
    has_manual_edit = df["ManuallyEditedBy"].notna()
    when_str = pd.to_datetime(df["ManuallyEditedAt"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    df["Manually Edited"] = ""
    df.loc[has_manual_edit, "Manually Edited"] = (
        "" + df.loc[has_manual_edit, "ManuallyEditedBy"].astype(str) + " (" + when_str.loc[has_manual_edit] + ")"
    )
    item_master_pending = {}
    if is_admin:
        item_master_pending = item_master_pending_cross_link()
        if item_master_pending:
            # A row with a pending Edit/Delete already staged shouldn't be
            # editable again here — it would either clobber that staged
            # change or edit a row that's about to be deleted. It's still
            # visible (and undoable) in the Pending Item Master Changes
            # section above. A pending Add never appears here anyway,
            # since it isn't in dbo.items until it's pushed.
            df = df[~df["UPC"].isin(item_master_pending.keys())]

    # Cascading filters: Department/Brand/Source options each narrow to
    # whatever the OTHER two are currently set to — pick a Source and
    # Department only lists brands that actually appear there.
    dept_key, brand_key, source_key_filter, search_key = "im_dept", "im_brand", "im_source", "im_search"
    current_dept = st.session_state.get(dept_key, "All")
    current_brand = st.session_state.get(brand_key, "All")
    current_source = st.session_state.get(source_key_filter, "All")

    def _narrow(base, exclude_col):
        out = base
        if exclude_col != "Department" and current_dept != "All":
            out = out[out["Department"].fillna("").eq("") if current_dept == NO_DEPT else out["Department"] == current_dept]
        if exclude_col != "Brand" and current_brand != "All":
            out = out[out["Brand"] == current_brand]
        if exclude_col != "SourceKey" and current_source != "All":
            out = out[out["SourceKey"] == current_source]
        return out

    narrowed = _narrow(df, "Department")["Department"]
    departments = ["All"] + ([NO_DEPT] if narrowed.fillna("").eq("").any() else []) + sorted(
        d for d in narrowed.dropna().unique().tolist() if d)
    if current_dept not in departments:
        st.session_state[dept_key] = "All"

    brands = ["All"] + sorted(_narrow(df, "Brand")["Brand"].dropna().unique().tolist())
    if current_brand not in brands:
        st.session_state[brand_key] = "All"

    sources_available = ["All"] + sorted(_narrow(df, "SourceKey")["SourceKey"].dropna().unique().tolist())
    if current_source not in sources_available:
        st.session_state[source_key_filter] = "All"

    col1, col2, col3, col4 = st.columns(4)
    dept_filter = col1.selectbox("Department", departments, key=dept_key)
    brand_filter = col2.selectbox("Brand", brands, key=brand_key)
    source_filter = col3.selectbox("Source", sources_available, key=source_key_filter)
    search = col4.text_input("Search description, brand or UPC", key=search_key)
    mcol1, mcol2 = st.columns([1.6, 2.4], vertical_alignment="center")
    manual_only = mcol1.checkbox("Only manually-edited items", key="im_manual_only")
    edited_by = "Anyone"
    if manual_only:
        people = sorted(df["ManuallyEditedBy"].dropna().astype(str).unique())
        edited_by = mcol2.selectbox("Edited by", ["Anyone"] + people, key="im_manual_by", label_visibility="collapsed",
                                    format_func=lambda p: "Edited by anyone" if p == "Anyone" else f"Edited by {p}")

    filtered = df.copy()
    if dept_filter == NO_DEPT:
        filtered = filtered[filtered["Department"].fillna("").eq("")]
    elif dept_filter != "All":
        filtered = filtered[filtered["Department"] == dept_filter]
    if brand_filter != "All":
        filtered = filtered[filtered["Brand"] == brand_filter]
    if source_filter != "All":
        filtered = filtered[filtered["SourceKey"] == source_filter]
    if manual_only:
        filtered = filtered[filtered["ManuallyEditedBy"].notna()]
        if edited_by != "Anyone":
            filtered = filtered[filtered["ManuallyEditedBy"].astype(str) == edited_by]
    if search:
        s = search.lower()
        filtered = filtered[
            filtered["Description"].str.lower().str.contains(s, na=False, regex=False)
            | filtered["Brand"].str.lower().str.contains(s, na=False, regex=False)
            | filtered["UPC"].str.contains(s, na=False, regex=False)
        ]

    matched_count = len(filtered)

    # Real pagination instead of a flat cap — with 330K+ items, rendering
    # them all in the browser at once would be very slow, but a page
    # control (unlike a hard cutoff) still lets you reach every row.
    page_size_key, page_num_key = "im_page_size", "im_page_num"
    # A big page by default — the whole item master is already loaded, and
    # the grid only draws the rows in view, so 1,000 costs about the same as 50.
    if st.session_state.get(page_size_key) not in IM_PAGE_SIZES:
        st.session_state[page_size_key] = IM_DEFAULT_PAGE_SIZE
    page_size = st.session_state[page_size_key]
    total_pages = max(1, (matched_count - 1) // page_size + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages

    pcol1, pcol2, pcol3 = st.columns([1, 1, 3])
    page_size = pcol1.selectbox("Rows per page", IM_PAGE_SIZES, key=page_size_key)
    total_pages = max(1, (matched_count - 1) // page_size + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages
    page_num = pcol2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=page_num_key)
    with pcol3.container(key="im_page_caption"):
        st.caption(f"{matched_count:,} matching items ({len(df):,} total) — page {page_num:,} of {total_pages:,}")

    start = (page_num - 1) * page_size
    page_df = filtered.iloc[start:start + page_size]

    if is_admin:
        st.caption(
            'Edit cells, then Stage Changes — Push on Pending Changes makes them live.',
            help=("Edit cells directly, then click Stage Changes — they'll show up on the Pending "
            "Changes tab for every editor immediately; Push there actually applies them. Only this "
            "page's rows are staged."),
        )
        department_options = load_departments()["department"].tolist()
        edited = st.data_editor(
            page_df,
            key="items_editor",
            width='stretch',
            height=min(IM_GRID_HEIGHT, 38 + 35 * max(len(page_df), 1)),
            hide_index=True,
            disabled=["UPC", "CreatedAt", "UpdatedAt", "Manually Edited"],
            column_order=[
                "UPC", "Description", "Department", "Category", "Subcategory", "Brand",
                "Pack", "Size", "UOM", "SourceKey", "Manually Edited", "CreatedAt", "UpdatedAt",
            ],
            column_config={
                "Department": st.column_config.SelectboxColumn(options=[""] + department_options, required=False),
            },
        )
        if st.button("Stage Changes", type="primary"):
            merged = edited.set_index("UPC")
            original = page_df.set_index("UPC")
            changed = {}
            for upc, row in merged.iterrows():
                if item_row_changed(row, original.loc[upc]):
                    changed[upc] = {
                        "change_type": "edit",
                        "description": sql_value(row["Description"]),
                        "department": sql_value(row["Department"]),
                        "category": sql_value(row["Category"]),
                        "subcategory": sql_value(row["Subcategory"]),
                        "brand": sql_value(row["Brand"]),
                        "pack": sql_value(row["Pack"]),
                        "size": sql_value(row["Size"]),
                        "uom": sql_value(row["UOM"]),
                        "source_key": sql_value(row["SourceKey"]),
                    }
            if not changed:
                st.info("No cells were changed.")
            else:
                # A UPC already flagged "Manually Edited" above has someone's
                # deliberate correction sitting in manual_overrides — staging
                # over it here would silently replace that correction with
                # no record of what was lost. Anything else stages normally;
                # these go into ITEM_MASTER_CONFLICT_KEY instead, to be shown
                # with the existing correction's actual details and confirmed
                # (or dropped) explicitly, one at a time, below.
                manually_edited_upcs = set(page_df.loc[page_df["ManuallyEditedBy"].notna(), "UPC"])
                conflicts = {upc: c for upc, c in changed.items() if upc in manually_edited_upcs}
                safe = {upc: c for upc, c in changed.items() if upc not in manually_edited_upcs}
                blocked = {}
                if safe:
                    blocked = dept_mapping.save_item_master_pending_bulk(ENGINE, safe, st.session_state["name"])
                    load_item_master_pending.clear()
                if conflicts:
                    st.session_state.setdefault(ITEM_MASTER_CONFLICT_KEY, {}).update(conflicts)
                staged_count = len(safe) - len(blocked)
                if staged_count and not conflicts:
                    st.success(f"Staged {staged_count} changed row(s) — see Pending Changes to review and push.")
                if blocked:
                    render_blocked_item_master_edits(blocked)
                if staged_count and not conflicts and not blocked:
                    st.rerun()

        pending_conflicts = st.session_state.get(ITEM_MASTER_CONFLICT_KEY) or {}
        if pending_conflicts:
            st.warning(
                f"{len(pending_conflicts)} of your edited row(s) already have a manual "
                "correction on file — review each before staging it over that correction."
            )
            existing = dept_mapping.get_manual_override_details(ENGINE, list(pending_conflicts.keys()))
            for upc, change in list(pending_conflicts.items()):
                prior = existing.get(upc, {})
                with st.container(border=True):
                    st.markdown(
                        f"**{upc}** — manually corrected by **{prior.get('updated_by', 'unknown')}** "
                        f"on {prior.get('updated_at')}"
                    )
                    dcol1, dcol2 = st.columns(2)
                    dcol1.caption("Current manual correction")
                    dcol1.write({f: prior.get(f) for f in ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]})
                    dcol2.caption("Your new edit")
                    dcol2.write({f: change.get(f) for f in ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]})
                    ccol1, ccol2 = st.columns(2)
                    if ccol1.button("Stage this over the existing correction", key=f"confirm_conflict_{upc}"):
                        blocked = dept_mapping.save_item_master_pending_bulk(ENGINE, {upc: change}, st.session_state["name"])
                        load_item_master_pending.clear()
                        del st.session_state[ITEM_MASTER_CONFLICT_KEY][upc]
                        if blocked:
                            render_blocked_item_master_edits(blocked)
                        else:
                            st.rerun()
                    if ccol2.button("Discard this edit, keep the correction", key=f"discard_conflict_{upc}"):
                        del st.session_state[ITEM_MASTER_CONFLICT_KEY][upc]
                        st.rerun()
    else:
        st.caption("(Read only for your role.)")
        st.dataframe(
            page_df, width='stretch', hide_index=True, height=min(IM_GRID_HEIGHT, 38 + 35 * max(len(page_df), 1)),
            column_order=[
                "UPC", "Description", "Department", "Category", "Subcategory", "Brand",
                "Pack", "Size", "UOM", "SourceKey", "Manually Edited", "CreatedAt", "UpdatedAt",
            ],
        )


def render_dr_settings() -> None:
    """Department Review → Settings, for admins."""
    render_settings_requests_admin()
    actor = st.session_state["name"]
    sources = sorted(load_sources()["source_key"].tolist())

    # ---- Departments ----
    st.markdown("#### Departments")
    st.caption("The list everyone picks from. Scan Advantage's own Departments are added automatically.")
    add_col1, add_col2 = st.columns([3, 1])
    new_dept = add_col1.text_input("Add a new Department", key="new_department_input", label_visibility="collapsed",
                                   placeholder="Add a new Department (e.g. BULK)")
    if add_col2.button("Add", key="add_department_btn", width='stretch'):
        if new_dept.strip():
            dept_mapping.add_department(ENGINE, new_dept, actor)
            clear_settings_caches()
            st.session_state["_toast"] = (f"Added **{new_dept.strip().upper()}** — it's in every Department list now.", "")
            st.rerun()
    usage = load_department_usage()
    st.dataframe(
        usage.rename(columns={"department": "Department", "source_type": "From", "Groups": "Groups decided",
                              "Staged": "Staged changes", "Defaults": "Unmatched Defaults", "Items": "Items now"}),
        hide_index=True, width='stretch', height=min(420, 40 + 35 * len(usage)),
        column_config={"From": st.column_config.TextColumn(help="auto = one of Scan Advantage's own; manual = added here")},
    )
    manual_depts = usage.loc[usage["source_type"] == "manual", "department"].tolist()
    if manual_depts:
        rem_col1, rem_col2 = st.columns([3, 1])
        to_remove = rem_col1.selectbox("Remove a Department you added", manual_depts, index=None,
                                       placeholder="Remove a Department you added…",
                                       key="remove_department_select", label_visibility="collapsed")
        in_use = {}
        if to_remove:
            u = dept_mapping.department_usage(ENGINE, to_remove)
            in_use = {k: n for k, n in u.items() if n}
        if rem_col2.button("Remove", key="remove_department_btn", width='stretch', disabled=not to_remove or bool(in_use)):
            dept_mapping.remove_department(ENGINE, to_remove, actor)
            clear_settings_caches()
            st.session_state["_toast"] = (f"Removed **{to_remove}** from the Department list.", "")
            st.rerun()
        if in_use:
            st.warning(f"**{to_remove}** can't be removed while it's in use — "
                       + ", ".join(f"{n:,} {k}" for k, n in in_use.items())
                       + ". Change those to another Department first.")

    # ---- Strict Departments ----
    st.divider()
    st.markdown("#### Strict Departments")
    st.caption("A distributor Department listed here is never auto-decided — its groups always wait for a person.",
               help="Use it for a catch-all a distributor uses for very different things (a generic \"Bulk\" or "
                    "\"Specialty\"). Trust direct evidence: an item Scan Advantage itself carries can still decide it. "
                    "Source \"any\" covers every distributor; a row for one source wins over \"any\".")
    strict_df = load_strict_departments()
    texts = sorted(set(load_old_department_texts()) | set(strict_df["old_department"].dropna().astype(str).str.upper()))
    edited_strict = st.data_editor(
        strict_df.assign(old_department=strict_df["old_department"].astype(str).str.upper()),
        key="strict_departments_editor", width='stretch', hide_index=True, num_rows="dynamic",
        column_config={
            "source_key": st.column_config.SelectboxColumn("Source", options=["any"] + sources, required=True),
            "old_department": st.column_config.SelectboxColumn("Distributor's Department", options=texts, required=True),
            "trust_direct_evidence": st.column_config.CheckboxColumn("Trust direct evidence", default=False),
        },
    )
    if st.button("Save Strict Departments", type="primary", key="save_strict_btn"):
        rows = edited_strict.dropna(subset=["source_key", "old_department"])
        rows = rows[rows["old_department"].astype(str).str.strip() != ""]
        with db_begin() as conn:
            conn.execute(text("DELETE FROM dbo.dept_mapping_strict_departments"))
            if not rows.empty:
                conn.execute(
                    text("INSERT INTO dbo.dept_mapping_strict_departments (source_key, old_department, trust_direct_evidence, updated_by) "
                         "VALUES (:source_key, :old_department, :trust_direct_evidence, :updated_by)"),
                    [{"source_key": r["source_key"], "old_department": str(r["old_department"]).strip(),
                      "trust_direct_evidence": bool(r["trust_direct_evidence"]), "updated_by": actor}
                     for _, r in rows.iterrows()],
                )
        load_strict_departments.clear()
        activity("Settings", f"Saved Strict Departments ({len(rows)} row(s))", ", ".join(rows["old_department"].astype(str).tolist())[:500])
        apply_settings_change("Strict Departments")

    # ---- Unmatched Department Defaults ----
    st.divider()
    st.markdown("#### Unmatched Department Defaults")
    st.caption("One row per distributor Department the Unmatched groups use. A default is the Department their "
               "groups are suggested as — someone still approves each group.")
    render_unmatched_defaults_editor(editable=True)

    render_workbook_section()


def render_unmatched_defaults_editor(editable: bool) -> None:
    """The Unmatched Defaults grid, built from the Unmatched groups
    themselves — one row per distributor Department text."""
    olds = load_unmatched_old_departments()
    departments = sorted(load_departments()["department"].tolist())
    if olds.empty:
        st.caption("No Unmatched groups right now.")
        return
    f1, f2 = st.columns([3, 1.6])
    q = f1.text_input("Find", key="umd_search", placeholder="Find a distributor Department…",
                      label_visibility="collapsed").strip().upper()
    only = f2.segmented_control("Show", ["All", "Waiting", "No default"], default="All", key="umd_show",
                                label_visibility="collapsed") or "All"
    view = olds
    if q:
        view = view[view["old_department"].fillna("").str.contains(q, regex=False)]
    if only == "Waiting":
        view = view[view["n_waiting"] > 0]
    elif only == "No default":
        view = view[view["default"].isna()]
    grid = pd.DataFrame({
        "Distributor's Department": view["old_department"].replace("", "(blank)"),
        "Used by": view["sources"], "Groups waiting": view["n_waiting"], "Groups": view["n_groups"],
        "Items": view["n_items"], "Default": view["default"], "Per-source exceptions": view["exceptions"],
    })
    st.caption(f"{len(view):,} of {len(olds):,} · {int((olds['n_waiting'] > 0).sum()):,} with groups waiting · "
               f"{int(olds['default'].notna().sum()):,} with a default")
    edited = st.data_editor(
        grid, key=f"umd_editor_{only}_{q}", hide_index=True, width="stretch", height=min(520, 40 + 35 * len(grid)),
        disabled=True if not editable else ["Distributor's Department", "Used by", "Groups waiting", "Groups", "Items",
                                            "Per-source exceptions"],
        column_config={
            "Default": st.column_config.SelectboxColumn("Default", options=departments,
                                                        help="The Department these groups are suggested as. Clear it for none."),
            "Groups waiting": st.column_config.NumberColumn(help="Unmatched groups still waiting on a decision"),
            "Per-source exceptions": st.column_config.TextColumn(help="A different default for one source (below)"),
        },
    )
    if not editable:
        return
    changes = []
    for i in range(len(grid)):
        before, after = grid.iloc[i]["Default"], edited.iloc[i]["Default"]
        before = None if pd.isna(before) else before
        after = None if pd.isna(after) or after == "" else after
        if before != after:
            changes.append((view.iloc[i]["old_department"], after))
    if st.button(f"Save {len(changes)} change(s)" if changes else "Save Unmatched Defaults", type="primary",
                 key="save_unmatched_defaults_btn", disabled=not changes):
        with db_begin() as conn:
            for old, new in changes:
                dept_mapping.set_unmatched_default(conn, "any", old, new, st.session_state["name"])
        activity("Settings", f"Saved {len(changes)} Unmatched Default(s)",
                 "; ".join(f"{old or '(blank)'} → {new or '(none)'}" for old, new in changes)[:600])
        clear_settings_caches()
        apply_settings_change("Unmatched Defaults")
    with st.expander("Per-source exceptions"):
        st.caption("Rare: a different default when one particular source uses that Department text.")
        ex = load_unmatched_defaults()
        ex = ex[ex["source_key"] != "any"].reset_index(drop=True)
        texts = sorted(set(olds["old_department"].dropna()) | set(ex["old_department"].str.upper()))
        ex_edit = st.data_editor(
            ex.assign(old_department=ex["old_department"].str.upper()), key="umd_exceptions", hide_index=True,
            width="stretch", num_rows="dynamic",
            column_config={
                "source_key": st.column_config.SelectboxColumn("Source", options=sorted(load_sources()["source_key"]), required=True),
                "old_department": st.column_config.SelectboxColumn("Distributor's Department", options=texts, required=True),
                "new_department": st.column_config.SelectboxColumn("Default", options=departments, required=True),
            },
        )
        if st.button("Save exceptions", key="save_umd_exceptions"):
            rows = ex_edit.dropna(subset=["source_key", "old_department", "new_department"])
            with db_begin() as conn:
                conn.execute(text("DELETE FROM dbo.dept_mapping_unmatched_defaults WHERE source_key <> 'any'"))
                for r in rows.itertuples():
                    dept_mapping.set_unmatched_default(conn, r.source_key, r.old_department, r.new_department, st.session_state["name"])
            activity("Settings", f"Saved {len(rows)} per-source Unmatched Default exception(s)")
            clear_settings_caches()
            apply_settings_change("Unmatched Default exceptions")


@st.cache_data(ttl=30, show_spinner=False)
def load_recent_pushes(limit: int = 10) -> pd.DataFrame:
    df = dept_mapping.list_activity(ENGINE, limit=400, areas=["Pushed live"])
    df = df[df["combo_id"].notna()].drop_duplicates("combo_id").head(limit)
    if not df.empty:
        df["when"] = pd.to_datetime(df["at"]).dt.tz_localize("UTC").dt.tz_convert("America/Los_Angeles")
    return df


def render_recent_pushes() -> None:
    """The last few groups pushed live — they've left Pending Changes for
    Decided (a pushed decision is changed by sending it back, not undone)."""
    df = load_recent_pushes()
    if df.empty:
        return
    st.divider()
    with st.expander(f"Recently pushed ({len(df)})", key="dr_recent_pushes"):
        for r in df.itertuples():
            c1, c2 = st.columns([4, 1.2], vertical_alignment="center")
            c1.markdown(f"**{r.target}**" + "  \n" + f":gray[{r.action} · pushed by {r.actor} · {r.when:%b %d %I:%M %p}]")
            c2.button("Open in Decided", key=f"recent_push_{int(r.combo_id)}", width="stretch",
                      on_click=_open_notification, args=("Decided", (r.target or "").split(" — ", 1)[-1]))


def render_dr_pending_changes(pending_changes, pending_upc_changes, combo_suggestions, upc_change_suggestions, recent_moves, total_pending_upcs) -> None:
    """Department Review → Pending Changes."""
    if st.session_state.pop("_reset_confirm_push", False):
        st.session_state["confirm_push_dept_changes"] = False
    render_discard_notices("dept_review")
    pc_search = render_search_bar("pending_changes", "Search Pending Changes", "Filter by source or group label…")

    def _pc_matches(source_key, label):
        if not pc_search:
            return True
        s = pc_search.lower()
        return s in (source_key or "").lower() or s in (label or "").lower()

    actor = st.session_state["name"]
    pc_department_options = load_departments()["department"].tolist()
    visible_recent_moves = [m for m in recent_moves if _pc_matches(m["source_key"], m["label"])]

    # Everything the one-time old-workbook import staged or moved is shown
    # in its own section (below the Push controls), apart from
    # everyone's regular work.
    is_import = lambda note: isinstance(note, str) and note.startswith(old_workbook_import.NOTE_PREFIX)
    facts = load_group_facts()

    def _recent_move_card(combo_id, moves):
        latest = moves[0]
        with st.container(border=True, key=f"card_move_{combo_id}"):
            c1, c3 = st.columns(CARD_COLS[1:])
            c1.markdown(f"**{latest['source_key'].upper()}** — {latest['label']}  \n"
                        + " → ".join(MOVE_WORDS.get(m["description"], m["description"]) for m in reversed(moves)))
            card_actions(c3, f"undo_recent_{combo_id}",
                         lambda: open_undo_picker(combo_id, f"{latest['source_key'].upper()} — {latest['label']}"))
            twice = load_import_notes().get(combo_id)
            bits = [f"{latest['n_upcs_total']:,} items"]
            if latest.get("origin_note"):
                bits.append(short_note(latest["origin_note"]))
            if twice:
                bits.append("listed twice in the old workbook")
            c1.caption(" · ".join(bits), help=twice)

    def _moves_list(moves_list):
        """The 5 groups moved most recently, older ones folded away."""
        moves_by_combo = {}
        for move in moves_list:  # newest first
            moves_by_combo.setdefault(move["combo_id"], []).append(move)
        moved = list(moves_by_combo.items())
        for combo_id, moves in moved[:RECENT_MOVES_SHOWN]:
            _recent_move_card(combo_id, moves)
        if len(moved) > RECENT_MOVES_SHOWN:
            with st.expander(f"{len(moved) - RECENT_MOVES_SHOWN} older move(s)"):
                for combo_id, moves in moved[RECENT_MOVES_SHOWN:]:
                    _recent_move_card(combo_id, moves)

    def _group_text(combo_id):
        f = facts.get(combo_id, {})
        bits = [b for b in (f.get("raw_department"), f.get("raw_category"), f.get("raw_subcategory")) if b]
        return f.get("source_key") or "", " / ".join(bits)

    def _override_group_card(combo_id, g, key):
        """UPC overrides staged for the items of one group: what they'll
        become, where they came from, the items themselves, and Undo."""
        src, text_ = _group_text(combo_id)
        upcs = g["upc"].tolist()
        with st.container(border=True, key=f"card_ov_{key}_{combo_id}"):
            c1, c3 = st.columns(CARD_COLS[1:])
            c1.markdown(f"**{src.upper()}** — {text_ or '(blank Department/Category/Subcategory)'}  \nUPC overrides: "
                        + ", ".join(f"**{d or '(blank)'}** {n:,}" for d, n in g["department"].fillna("").value_counts().items()))
            who = g.sort_values("staged_at").iloc[-1]["staged_by"]
            bits = [f"Staged by {who}", "pushed from the Pending Changes tab"]
            bits += [short_note(t) + ("" if n == len(g) else f" ({n:,} of {len(g):,})") for t, n in g["origin_note"].dropna().value_counts().items()]
            c1.caption(" · ".join(bits))
            if who == actor or is_admin:
                with c3.columns(2)[1].popover("↩ Undo…", width="stretch"):
                    st.markdown(f"Remove these **{len(upcs):,}** staged UPC override(s)? Nothing live changes — "
                                "they're just taken back out of Pending Changes.")
                    if st.button("Remove them", key=f"undo_overrides_{key}_{combo_id}", type="primary"):
                        dept_mapping.delete_item_master_pending_many(ENGINE, upcs)
                        load_item_master_pending.clear()
                        load_pending_overrides_by_group.clear()
                        st.session_state["_toast"] = (f"Took back {len(upcs):,} staged UPC override(s) for **{text_ or src.upper()}**.", "")
                        st.rerun()
            with st.expander(f"Show affected items ({len(upcs):,})", key=f"ov_items_{key}_{combo_id}"):
                st.dataframe(g[["upc", "description", "now", "department"]].rename(columns={
                    "upc": "UPC", "description": "Description", "now": "Now", "department": "Will be"}),
                    hide_index=True, width="stretch", height=min(300, 40 + 35 * len(g)))

    def _override_groups(df, key):
        per_group = sorted(df.groupby("combo_id", sort=False), key=lambda kv: -len(kv[1]))
        for combo_id, g in per_group[:RECENT_MOVES_SHOWN]:
            _override_group_card(int(combo_id), g, key)
        if len(per_group) > RECENT_MOVES_SHOWN:
            with st.expander(f"{len(per_group) - RECENT_MOVES_SHOWN} more group(s)"):
                for combo_id, g in per_group[RECENT_MOVES_SHOWN:]:
                    _override_group_card(int(combo_id), g, key)

    import_moves = [m for m in visible_recent_moves if is_import(m.get("origin_note"))]
    regular_moves = [m for m in visible_recent_moves if not is_import(m.get("origin_note"))]
    ov_all = load_pending_overrides_by_group()
    if not ov_all.empty:
        ov_all = ov_all[[_pc_matches(*_group_text(c)) for c in ov_all["combo_id"]]]
    ov_import = ov_all[ov_all["origin_note"].map(is_import)] if not ov_all.empty else ov_all
    ov_regular = ov_all[~ov_all["origin_note"].map(is_import)] if not ov_all.empty else ov_all

    # ---- Recent moves, at the top (the 5 most recent groups) ----
    if regular_moves:
        st.markdown("#### Recent moves")
        st.caption("Break Out / Send Back — already applied, no push needed. Undo… picks how far back to go.")
        _moves_list(regular_moves)
        st.divider()

    # ---- UPC overrides staged for items in these groups (pushed from
    # the Pending Changes tab, but listed here by group) ----
    if not ov_regular.empty:
        st.markdown("#### UPC overrides staged for items in these groups")
        st.caption("Listed by group here; pushed from the **Pending Changes** tab.")
        _override_groups(ov_regular, "reg")
        st.divider()

    def _render_import_section(group_ids=(), upc_ids=(), render_group=None, render_upc=None, needs=()):
        """The one-time old-workbook import, apart from everyone's
        regular changes — sub-headed by where each change lands."""
        if not (group_ids or upc_ids or import_moves or not ov_import.empty or load_import_choices(None)):
            return
        st.divider()
        h1, h2 = st.columns([4, 1.6])
        h1.markdown("#### Old-workbook import")
        if is_admin:
            with h2.popover("Undo the whole import…", width="stretch"):
                summ = dept_mapping.old_workbook_import_summary(ENGINE)
                st.markdown(
                    f"Takes back everything the import still has here: **{summ['groups']}** group decision(s), "
                    f"**{summ['items']:,}** Broken Out item decision(s), **{summ['overrides']:,}** UPC override(s), "
                    f"and undoes its **{summ['moves']}** move(s). Anything already pushed stays; anything "
                    "someone else has since voted on, moved or staged on is left alone (you'll see which).")
                if st.button("Undo the import", key="undo_whole_import", type="primary"):
                    r = dept_mapping.undo_old_workbook_import(ENGINE, st.session_state["name"])
                    st.cache_data.clear()
                    load_notifications.clear()
                    msg = (f"Undid the old-workbook import: {r['groups']} group decision(s), {r['items']:,} item "
                           f"decision(s), {r['overrides']:,} UPC override(s) taken back; {r['moves']} move(s) undone.")
                    if r["kept"]:
                        msg += " Left alone: " + "; ".join(r["kept"])
                    st.session_state["_toast"] = (msg, "")
                    st.rerun()
        st.caption("What a workbook upload staged or moved, kept apart. Its decisions go out with the Push above; "
                   "its UPC overrides from the Pending Changes tab.")
        render_import_choices()
        if import_moves:
            st.markdown(f"##### Moves — made straight away ({len({m['combo_id'] for m in import_moves})})")
            _moves_list(import_moves)
        for area in ("Crosswalk", "Unmatched", "Decided"):
            ids = [cid for cid in group_ids if facts.get(cid, {}).get("where") == area]
            if ids:
                st.markdown(f"##### {area} groups ({len(ids)})")
                for cid in ids:
                    render_group(cid)
        if upc_ids:
            st.markdown(f"##### Broken Out items ({len(upc_ids)} group(s))")
            for cid in upc_ids:
                render_upc(cid, "needs_agreement" if cid in needs else "ready")
        if not ov_import.empty:
            st.markdown(f"##### UPC overrides ({len(ov_import):,} item(s) in {ov_import['combo_id'].nunique()} group(s))")
            _override_groups(ov_import, "imp")

    visible_combo_disputes = {
        cid: s for cid, s in combo_suggestions.items() if _pc_matches(s[0]["source_key"], s[0]["label"])
    }
    visible_upc_change_suggestions = {
        upc: s for upc, s in upc_change_suggestions.items() if _pc_matches(s[0]["source_key"], s[0]["label"])
    }
    # Defaults for the sections below that render regardless of
    # whether there's anything Ready to push (Needs Agreement,
    # Undo Requested, Recent Moves) — overwritten by the real
    # values inside the `else` branch when there's staged work.
    visible_pending_changes, visible_by_combo = {}, {}
    combo_undo_requests, upc_group_undo_requests, upc_group_primaries = {}, {}, {}
    needs_agreement_upc_combo_ids = []
    active_combo_disputes, snoozed_combo_disputes = {}, {}
    active_needs_agreement_upc_combo_ids, snoozed_needs_agreement_upc_combo_ids = [], []

    if not total_pending_upcs:
        st.info("Nothing staged yet.")
        _render_import_section()
    else:
        visible_pending_changes = {
            cid: c for cid, c in pending_changes.items() if _pc_matches(c["source_key"], c["label"])
        }
        # Who already backs each resolved combo's decision (staged
        # it, agreed with it, or suggested it independently) — used
        # to hide "I also agree" from someone who's already one of
        # them, since clicking it again would be a no-op.
        combo_backers = dept_mapping.get_combo_backers(ENGINE, list(visible_pending_changes.keys()))
        # Who has so far asked to undo each combo — an Undo click
        # from a combo with 2+ backers doesn't execute by itself,
        # it registers here; it only actually fires once every
        # backer has clicked it too (or an admin/single-backer
        # short-circuits it), see dept_mapping.request_undo_combo.
        combo_undo_requests = dept_mapping.get_undo_requests(ENGINE, "combo", list(visible_pending_changes.keys()))
        by_combo = {}
        for upc, change in pending_upc_changes.items():
            by_combo.setdefault(change["combo_id"], []).append((upc, change))
        visible_by_combo = {
            cid: items for cid, items in by_combo.items()
            if _pc_matches(items[0][1].get("source_key", ""), items[0][1]["label"])
        }
        # A Broken Out group with ANY pending suggestion moves
        # ENTIRELY to Needs Agreement — never split between there
        # and Ready to push, so a half-agreed group can never get
        # pushed just because most of its items are uncontested.
        combo_ids_with_upc_suggestions = {s[0]["combo_id"] for s in visible_upc_change_suggestions.values()}
        needs_agreement_upc_combo_ids = [cid for cid in visible_by_combo if cid in combo_ids_with_upc_suggestions]
        eligible_upc_combo_ids_all = [cid for cid in visible_by_combo if cid not in combo_ids_with_upc_suggestions]
        upc_group_primaries = dept_mapping.get_broken_out_group_primaries(ENGINE, list(visible_by_combo.keys()))
        upc_group_undo_requests = dept_mapping.get_undo_requests(ENGINE, "upc_group", list(visible_by_combo.keys()))

        def _render_group_row(combo_id, change):
            with st.container(border=True, key=f"card_pc_{combo_id}"):
                c0, c1, c_act = st.columns(CARD_COLS)
                c0.checkbox(
                    "Include", value=True, key=f"dept_pending_include_combo_{combo_id}",
                    label_visibility="collapsed",
                    help="Included in the next push — uncheck to save this one for later.",
                )
                f_ = facts.get(combo_id, {})
                was = f_.get("decided_department") if f_.get("where") == "Decided" else None
                c1.markdown(f"**{change['source_key'].upper()}** — {change['label']}  \n"
                            + (f"Change **{was}** → **{change['department']}**" if was and was != change["department"]
                               else f"Approve as **{change['department']}**"))
                is_primary_stager = actor == change.get("staged_by")
                card_actions(c_act, f"undo_pending_{combo_id}",
                             lambda: open_undo_picker(combo_id, f"{change['source_key'].upper()} — {change['label']}"),
                             {"kind": "combo", "combo_id": combo_id, "tier": change.get("tier"),
                              "source_key": change["source_key"], "raw_label": change["label"],
                              "n_upcs_total": change["n_upcs_total"],
                              "label": f"{change['source_key'].upper()} — {change['label']}"},
                             f"admin_override_btn_ready_{combo_id}")
                requested = combo_undo_requests.get(combo_id, set())
                if requested and not is_primary_stager:
                    c1.caption(f"Undo requested by {', '.join(sorted(requested))} — waiting on **{change.get('staged_by') or 'the stager'}**.")
                line, twice = group_line(combo_id, change, change.get("origin_note"))
                c1.caption(line, help=twice)

                def _submit_new_dept(new_dept, toast_verb):
                    with track(combo_id, f"{change['source_key'].upper()} — {change['label']}", f"{toast_verb} {new_dept}"):
                        result = dept_mapping.upsert_combo_suggestion(
                            ENGINE, combo_id, change["tier"], new_dept, change["source_key"],
                            change["label"], change["n_upcs_total"], actor,
                        )
                    clear_dept_suggestion_caches()
                    if result.get("blocked"):
                        st.session_state["_toast"] = (
                            f"**{change['label']}** already has {dept_mapping.MAX_DISTINCT_SUGGESTIONS} "
                            f"different suggestions ({', '.join(result['departments'])}) — pick one of "
                            "those instead, or ask an admin to override.",
                            "",
                        )
                    elif result["disputed"]:
                        st.session_state["_toast"] = (
                            f"**{change['label']}** now has more than one suggested department "
                            f"({', '.join(result['departments'])}) — see the Needs Agreement section below.",
                            "",
                        )
                    else:
                        st.session_state["_toast"] = (f"{toast_verb} **{change['label']}** → {new_dept}.", "")
                    st.session_state[f"_reset_suggest_{combo_id}"] = True
                    st.rerun()

                is_locked = bool(change.get("overridden_by"))
                if is_locked and not is_admin:
                    c1.caption(
                        f"Locked by **{change['overridden_by']}**'s admin override — only an admin "
                        "can change this until they remove it."
                    )
                elif not is_locked:
                    # "I also agree" and the change / suggest dropdown sit
                    # in a small Change… popover, keeping the card one row.
                    show_agree = actor not in combo_backers.get(combo_id, set())
                    # under ↩ Undo… / 🛡️ Override: "I also agree", then the dropdown (type to search) and its button
                    agree_col = c_act if show_agree else None
                    scol1, scol2 = c_act.columns([2.3, 1], vertical_alignment="bottom")
                    if show_agree:
                        if agree_col.button(
                            f"I also agree — {change['department']}", key=f"dept_pending_agree_{combo_id}",
                            width='stretch',
                            help="Records that you reviewed this and agree, without changing anything. If "
                                 "you'd rather suggest something else, use the box to the right instead.",
                        ):
                            with track(combo_id, f"{change['source_key'].upper()} — {change['label']}", f"Agreed with {change['department']}"):
                                dept_mapping.upsert_combo_suggestion(
                                    ENGINE, combo_id, change["tier"], change["department"], change["source_key"],
                                    change["label"], change["n_upcs_total"], actor,
                                )
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Recorded your agreement on **{change['label']}**.", "")
                            st.rerun()
                    suggest_key = f"dept_pending_suggest_{combo_id}"
                    # Must run BEFORE the selectbox below is instantiated —
                    # st.session_state can't reassign an already-
                    # instantiated widget's own key in the same run (see
                    # the identical "_reset_confirm_push" pattern used for
                    # the main push checkbox, for the exact same reason).
                    if st.session_state.pop(f"_reset_suggest_{combo_id}", False):
                        st.session_state[suggest_key] = None
                    if is_primary_stager:
                        # Only the person who staged this gets to call
                        # it an "Update" — they're editing their own
                        # submission, not raising a fresh disagreement.
                        new_dept = scol1.selectbox(
                            "Suggest a different department instead", pc_department_options, index=None,
                            placeholder="Change Department", key=suggest_key, label_visibility="collapsed",
                            help="Doesn't discard anything — just restages this same group with a "
                                 "different department. Any approvals on the batch reset, since what's "
                                 "being pushed changed.",
                        )
                        if scol2.button("Update", key=f"dept_pending_update_{combo_id}", width='stretch', disabled=not new_dept):
                            _submit_new_dept(new_dept, "Updated")
                    else:
                        # Anyone else picking a different department
                        # here is casting their OWN vote, same as in
                        # Needs Agreement — it doesn't overwrite
                        # Kristi's (or whoever's) decision, it just
                        # adds a competing suggestion, which sends
                        # this back to Needs Agreement to be settled.
                        new_dept = scol1.selectbox(
                            "Suggest a different department", pc_department_options, index=None,
                            placeholder="Suggest Department", key=suggest_key, label_visibility="collapsed",
                            help="Casts your own vote for a different department — doesn't change what's "
                                 "being pushed by itself, it sends this to Needs Agreement so it can be "
                                 "settled the normal way.",
                        )
                        if scol2.button("Suggest", key=f"dept_pending_update_{combo_id}", width='stretch', disabled=not new_dept):
                            _submit_new_dept(new_dept, "Suggested")
                render_affected_items_expander(combo_id, change["n_upcs_total"], "pending_items")

        def _render_upc_group_row(combo_id, items, mode="ready"):
            # A Broken Out combo can have hundreds of individually-
            # staged items — a widget-per-row doesn't scale to
            # that. One editable grid covers reviewing AND
            # correcting: fill in "New Department" for whichever
            # rows you want to touch (paste a column, drag-fill a
            # range — the grid supports both natively) and Apply
            # once. Filling a row you don't own doesn't overwrite
            # it — it becomes a suggestion for that row's owner,
            # exactly like any other Broken Out suggestion.
            first = items[0][1]
            actor = st.session_state["name"]
            group_suggestions = {
                upc: s for upc, s in upc_change_suggestions.items() if s[0]["combo_id"] == combo_id
            }
            with st.container(border=True, key=f"card_pcu_{combo_id}"):
                c0, c1, c_act = st.columns(CARD_COLS)
                if mode == "ready":
                    c0.checkbox(
                        "Include", value=True, key=f"dept_pending_include_upc_group_{combo_id}",
                        label_visibility="collapsed",
                        help="Included in the next push — uncheck to save this whole group for later.",
                    )
                else:
                    c0.checkbox(
                        "Save for later", value=False, key=f"dept_pending_snooze_upc_group_{combo_id}",
                        label_visibility="collapsed",
                        help="Move this group to Saved for later — it's still unresolved (still needs "
                             "agreement), just out of the main list until you check it again.",
                    )
                depts = Counter(c["department"] for _, c in items)
                c1.markdown(f"**{first.get('source_key', '').upper()}** — {first['label']}  \n"
                            f"Decide **{len(items):,}** item{'s' if len(items) != 1 else ''} one by one: "
                            + ", ".join(f"**{d}** {n:,}" for d, n in depts.most_common(3))
                            + (" …" if len(depts) > 3 else ""))
                who = Counter(c.get("staged_by") or "?" for _, c in items)
                bits = ["Staged by " + ", ".join(who)]
                if group_suggestions:
                    bits.append(f"{len(group_suggestions)} awaiting agreement")
                gl, twice = group_line(combo_id)
                bits += [b for b in gl.split(" · ")[1:] if b]  # (its item count is said above)
                item_notes = Counter(short_note(c.get("origin_note")) for _, c in items if c.get("origin_note"))
                bits += [t + ("" if n == len(items) else f" ({n:,} of {len(items):,})") for t, n in item_notes.items()]
                c1.caption(" · ".join(bits), help=twice)
                primary = upc_group_primaries.get(combo_id)
                is_primary_editor = actor == primary
                card_actions(c_act, f"undo_pending_upc_combo_{combo_id}",
                             lambda: open_undo_picker(combo_id, f"{first.get('source_key', '').upper()} — {first['label']}"),
                             {"kind": "upc_group", "combo_id": combo_id, "raw_label": first["label"],
                              "label": f"{first.get('source_key', '').upper()} — {first['label']}"},
                             f"admin_override_btn_group_{mode}_{combo_id}")
                requested = upc_group_undo_requests.get(combo_id, set())
                if requested and not is_primary_editor:
                    c1.caption(f"Undo requested by {', '.join(sorted(requested))} — waiting on **{primary or 'the first editor'}**.")

                if group_suggestions:
                    render_upc_change_suggestions(group_suggestions, pending_upc_changes, combo_id)

                with st.expander(f"Change items ({len(items):,})", key=f"pending_upc_expander_{combo_id}"):
                    display_df = pd.DataFrame([
                        {
                            "UPC": upc, "Description": change.get("description"),
                            "Department": change["department"], "Owner": change.get("staged_by") or "unknown",
                            "Revised By": change.get("revised_by") or "",
                            "Admin Override By": change.get("overridden_by") or "",
                        }
                        for upc, change in items
                    ])
                    st.caption("Fill in **New Department** for any row you want to change — your own items "
                               "change directly, anyone else's becomes a suggestion for its owner.")
                    picked = render_item_workbench(
                        f"pc_{mode}_{combo_id}", display_df, pc_department_options, value_label="New Department",
                        title=f"{first.get('source_key', '').upper()} — {first['label']}",
                        info_cols=["Department", "Description", "Owner", "Revised By", "Admin Override By", "UPC"],
                        stage_label="Apply changes",
                    )
                    if picked:
                        descs = dict(zip(display_df["UPC"], display_df["Description"]))
                        decisions = {
                            upc: {
                                "department": dept, "combo_id": combo_id,
                                "label": first["label"], "source_key": first.get("source_key"),
                                "description": descs.get(upc),
                            }
                            for upc, dept in picked.items()
                        }
                        with track(combo_id, f"{first.get('source_key', '').upper()} — {first['label']}", f"Changed {len(decisions)} item(s) in the grid"):
                            results = dept_mapping.stage_broken_out_decisions(ENGINE, decisions, actor, is_admin=is_admin)
                        reset_item_workbench(f"pc_{mode}_{combo_id}")
                        clear_dept_suggestion_caches()
                        decided_n = sum(1 for r in results.values() if r["status"] == "decided")
                        suggested_n = sum(1 for r in results.values() if r["status"] == "suggested")
                        blocked_n = sum(1 for r in results.values() if r["status"] == "blocked")
                        locked_n = sum(1 for r in results.values() if r["status"] == "locked")
                        msg_bits = []
                        if decided_n:
                            msg_bits.append(f"{decided_n} changed (you own them)")
                        if suggested_n:
                            msg_bits.append(f"{suggested_n} sent as suggestion(s) for the owner")
                        if blocked_n:
                            msg_bits.append(f"{blocked_n} already have too many suggestions")
                        if locked_n:
                            msg_bits.append(f"{locked_n} locked by an admin override")
                        st.session_state["_toast"] = ((", ".join(msg_bits) or "Nothing changed") + ".", "")
                        st.rerun()


        # Partitioned BEFORE anything renders, off whatever's already in
        # session_state (defaulting to included) — this is what lets
        # "Ready to push" and "Saved for later" be two separate sections
        # instead of one list with a checkbox buried in each row; an
        # Include toggle just moves a row's OWN checkbox to the other
        # section on the next rerun. Broken Out groups toggle as ONE
        # unit now (dept_pending_include_upc_group_{combo_id}) — a
        # group is either entirely in the push batch or entirely
        # saved for later, matching how a combo already worked.
        included_combo_ids = [cid for cid in visible_pending_changes if st.session_state.get(f"dept_pending_include_combo_{cid}", True)]
        excluded_combo_ids = [cid for cid in visible_pending_changes if cid not in included_combo_ids]
        included_upc_combo_ids = [
            cid for cid in eligible_upc_combo_ids_all
            if st.session_state.get(f"dept_pending_include_upc_group_{cid}", True)
        ]
        excluded_upc_combo_ids = [cid for cid in eligible_upc_combo_ids_all if cid not in included_upc_combo_ids]

        # A disputed combo/group can also be moved to Saved for
        # later WITHOUT resolving it — same idea as Include above,
        # just a separate checkbox/key since "unresolved but
        # deferred" is a different state than "decided but
        # deferred." Still fully rendered wherever it lands (full
        # voting/suggestion controls), just relocated.
        snoozed_combo_disputes = {
            cid: s for cid, s in visible_combo_disputes.items()
            if st.session_state.get(f"dept_pending_snooze_combo_{cid}", False)
        }
        active_combo_disputes = {cid: s for cid, s in visible_combo_disputes.items() if cid not in snoozed_combo_disputes}
        snoozed_needs_agreement_upc_combo_ids = [
            cid for cid in needs_agreement_upc_combo_ids
            if st.session_state.get(f"dept_pending_snooze_upc_group_{cid}", False)
        ]
        active_needs_agreement_upc_combo_ids = [cid for cid in needs_agreement_upc_combo_ids if cid not in snoozed_needs_agreement_upc_combo_ids]

        included_item_count = (
            sum(visible_pending_changes[cid]["n_upcs_total"] for cid in included_combo_ids)
            + sum(len(visible_by_combo[cid]) for cid in included_upc_combo_ids)
        )

        # ---- Push controls up top: what's about to happen, and the
        # button to make it happen, before scrolling through the
        # (potentially long) lists below. ----
        left_out = total_pending_upcs - included_item_count
        st.warning(f"Push makes the **{included_item_count:,}** included item(s) live right away"
                   + (f" ({left_out:,} left out stay staged)" if left_out else "") + ". A push can't be undone — "
                   "a pushed group can be sent back or re-decided later.")
        dept_push_approvals = dept_mapping.get_dept_push_approvals(ENGINE)
        dept_distinct_approvers = sorted({a["approver"] for a in dept_push_approvals})
        dept_required = dept_mapping.DEPT_PUSH_REQUIRED_APPROVALS
        if is_admin:
            st.caption("You're an admin, so you can push this batch on your own.")
        elif dept_distinct_approvers:
            st.caption(f"Approved by: {', '.join(dept_distinct_approvers)} ({len(dept_distinct_approvers)} of {dept_required} needed)")
        else:
            st.caption(f"No approvals yet — {dept_required} needed before this can be pushed (admins exempt).")
        if not is_admin:
            if st.session_state["name"] not in dept_distinct_approvers:
                if st.button("Approve this batch", key="approve_dept_push"):
                    dept_mapping.approve_dept_push(ENGINE, st.session_state["name"])
                    st.rerun()
            else:
                st.caption("You've already approved this batch.")
        dept_enough_approvals = is_admin or len(dept_distinct_approvers) >= dept_required
        reopen_undone_choices()
        n_questions = len(load_import_choices())
        if n_questions:
            st.error(f"Answer the {n_questions} **Needs your choice** question{'s' if n_questions > 1 else ''} above before pushing.")
        confirm_push = st.checkbox(
            "I've reviewed these changes and I'm ready to update the database.", key="confirm_push_dept_changes",
        )
        if st.button(
            f"Push {included_item_count:,} Included Item(s) to the Database", type="primary",
            key="push_pending_changes",
            disabled=not (confirm_push and dept_enough_approvals) or not included_item_count or bool(n_questions),
        ):
            included_upcs = [upc for cid in included_upc_combo_ids for upc, _ in visible_by_combo[cid]]
            included_pending_changes = {cid: pending_changes[cid] for cid in included_combo_ids}
            included_pending_upc_changes = {upc: pending_upc_changes[upc] for upc in included_upcs}
            touched_combo_ids = set(included_pending_changes.keys())
            for combo_id, change in included_pending_changes.items():
                dept_mapping.approve_combo(ENGINE, combo_id, change["department"], change.get("staged_by") or actor, pushed_by=actor,
                                           note=change.get("origin_note"))
                dept_mapping.delete_pending_change(ENGINE, combo_id)
                st.session_state.pop(f"dept_pending_include_combo_{combo_id}", None)
            if included_pending_upc_changes:
                touched_combo_ids.update(c["combo_id"] for c in included_pending_upc_changes.values())
                dept_mapping.apply_upc_decisions(
                    ENGINE,
                    {upc: {"department": c["department"], "staged_by": c.get("staged_by"), "origin_note": c.get("origin_note")}
                     for upc, c in included_pending_upc_changes.items()},
                    actor,
                )
                dept_mapping.delete_pending_upc_changes(ENGINE, list(included_pending_upc_changes))
                for combo_id in included_upc_combo_ids:
                    st.session_state.pop(f"dept_pending_include_upc_group_{combo_id}", None)
            # A pushed decision IS the new database state — any
            # earlier Break Out/Send Back snapshots for these same
            # combos are no longer meaningful undo targets, so
            # their move history resets here too (per explicit
            # design: undo only ever walks back through unpushed
            # steps, never behind a real, committed decision). Skip
            # a combo that still has other staged item(s) left for
            # later — those decisions aren't final yet, so its undo
            # history is still meaningful.
            remaining_upc_combo_ids = {
                c["combo_id"] for upc, c in pending_upc_changes.items() if upc not in included_pending_upc_changes
            }
            for combo_id in touched_combo_ids - remaining_upc_combo_ids:
                dept_mapping.clear_recent_moves_for_combo(ENGINE, combo_id)
            # Pushed is final: going back from here is a Send Back (itself
            # undoable), never a top-bar Undo of the push.
            dept_mapping.retire_undo_for_combos(ENGINE, touched_combo_ids)
            load_undo_redo.clear()
            load_recent_pushes.clear()
            for combo_id, change in included_pending_changes.items():
                activity("Pushed live", f"Pushed: {change['department']} (staged by {change.get('staged_by') or '?'})",
                         f"{change['source_key'].upper()} — {change['label']}", change["n_upcs_total"], combo_id)
            per_group = {}
            for upc, c in included_pending_upc_changes.items():
                per_group.setdefault(c["combo_id"], []).append(c)
            for combo_id, items in per_group.items():
                stagers = Counter(c.get("staged_by") or "?" for c in items)
                activity("Pushed live", f"Pushed {len(items):,} item decision(s) (staged by "
                         + ", ".join(f"{k} {n:,}" for k, n in stagers.items()) + ")",
                         f"{(items[0].get('source_key') or '').upper()} — {items[0]['label']}", len(items), combo_id,
                         details=dict(Counter(c["department"] for c in items)))
            pushed_count = included_item_count
            load_dept_pending_changes.clear()
            load_dept_pending_upc_changes.clear()
            load_dept_recent_moves.clear()
            load_dept_review_queue.clear()
            load_broken_out_combos.clear()
            load_pending_upc_overrides.clear()
            load_combo_upc_decisions.clear()
            load_decided_combos.clear()
            st.session_state["_reset_confirm_push"] = True
            # Carried straight into the item master's Department (and
            # any computed Merge draft) — no separate Merge needed.
            with st.spinner("Updating Department in the item master..."):
                synced = dept_mapping.sync_item_departments(ENGINE)
            load_items.clear()
            st.session_state["_toast"] = (
                f"Pushed {pushed_count:,} item(s). Item Master updated — {synced['items']:,} item "
                "department(s) changed.", "",
            )
            st.rerun()

        imp_combo_ids = [cid for cid in visible_pending_changes if is_import(visible_pending_changes[cid].get("origin_note"))]
        imp_upc_ids = [cid for cid in visible_by_combo
                       if all(is_import(c.get("origin_note")) for _, c in visible_by_combo[cid])]
        prefetch_affected_items((cid, visible_pending_changes[cid]["n_upcs_total"]) for cid in visible_pending_changes)
        _render_import_section(
            imp_combo_ids, imp_upc_ids,
            render_group=lambda cid: _render_group_row(cid, visible_pending_changes[cid]),
            render_upc=lambda cid, mode: _render_upc_group_row(cid, visible_by_combo[cid], mode=mode),
            needs=set(needs_agreement_upc_combo_ids),
        )
        imp_set = set(imp_combo_ids) | set(imp_upc_ids)
        reg = lambda ids: [cid for cid in ids if cid not in imp_set]
        active_needs_agreement_upc_combo_ids = reg(active_needs_agreement_upc_combo_ids)
        snoozed_needs_agreement_upc_combo_ids = reg(snoozed_needs_agreement_upc_combo_ids)

        st.divider()
        st.markdown("#### Ready to push")
        all_ids = reg(list(visible_pending_changes)) + reg(eligible_upc_combo_ids_all)
        if len(all_ids) > 1:
            b1, b2, _ = st.columns([1, 1, 2.5])
            if b1.button(f"Include all {len(all_ids)}", key="dept_pending_include_all", width="stretch"):
                for cid in reg(list(visible_pending_changes)):
                    st.session_state[f"dept_pending_include_combo_{cid}"] = True
                for cid in reg(eligible_upc_combo_ids_all):
                    st.session_state[f"dept_pending_include_upc_group_{cid}"] = True
                st.rerun()
            if b2.button("Leave all out", key="dept_pending_include_none", width="stretch"):
                for cid in reg(list(visible_pending_changes)):
                    st.session_state[f"dept_pending_include_combo_{cid}"] = False
                for cid in reg(eligible_upc_combo_ids_all):
                    st.session_state[f"dept_pending_include_upc_group_{cid}"] = False
                st.rerun()
        if not (reg(included_combo_ids) or reg(included_upc_combo_ids)):
            st.caption("Nothing currently included — check \"Include\" on a Saved for Later item below, or resolve a Needs Agreement one.")
        else:
            st.caption("Uncheck to leave one out of the push. Suggesting a different Department sends it to Needs agreement.")
            for combo_id in reg(included_combo_ids):
                _render_group_row(combo_id, visible_pending_changes[combo_id])
            for combo_id in reg(included_upc_combo_ids):
                _render_upc_group_row(combo_id, visible_by_combo[combo_id])

        if reg(excluded_combo_ids) or reg(excluded_upc_combo_ids) or snoozed_combo_disputes or snoozed_needs_agreement_upc_combo_ids:
            st.divider()
            st.markdown("#### Saved for later")
            st.caption("Left out of the push — tick one to include it again.")
            for combo_id in reg(excluded_combo_ids):
                _render_group_row(combo_id, visible_pending_changes[combo_id])
            for combo_id in reg(excluded_upc_combo_ids):
                _render_upc_group_row(combo_id, visible_by_combo[combo_id])
            if snoozed_combo_disputes or snoozed_needs_agreement_upc_combo_ids:
                st.caption(
                    "Still needs agreement (just moved out of the way below) — "
                    "uncheck \"Save for later\" on one to bring it back up top."
                )
                if snoozed_combo_disputes:
                    render_combo_suggestion_disputes(snoozed_combo_disputes)
                for combo_id in snoozed_needs_agreement_upc_combo_ids:
                    _render_upc_group_row(combo_id, visible_by_combo[combo_id], mode="needs_agreement")

    # ---- Needs agreement (bottom, per explicit layout request) ----
    if active_combo_disputes or active_needs_agreement_upc_combo_ids:
        st.divider()
        st.markdown("#### Needs agreement")
        st.caption("People suggested different Departments. It's settled when the ones who disagree change their "
                   "vote to match (or an admin overrides). Tick to save one for later.")
        if active_combo_disputes:
            render_combo_suggestion_disputes(active_combo_disputes)
        for combo_id in active_needs_agreement_upc_combo_ids:
            _render_upc_group_row(combo_id, visible_by_combo[combo_id], mode="needs_agreement")

    # ---- Undo requested — a third bucket, separate from Ready to
    # push/Saved for later/Needs agreement, for anything with an
    # unactioned request sitting on it (see dept_mapping.
    # request_undo_combo/request_undo_upc_group_all). ----
    undo_requested_combos = [cid for cid in visible_pending_changes if combo_undo_requests.get(cid)]
    undo_requested_upc_groups = [cid for cid in visible_by_combo if upc_group_undo_requests.get(cid)]
    if undo_requested_combos or undo_requested_upc_groups:
        st.divider()
        st.markdown("#### Undo requested")
        st.caption("Only the person who staged it (or an admin) can undo these.")
        for combo_id in undo_requested_combos:
            change = visible_pending_changes[combo_id]
            requested = combo_undo_requests.get(combo_id, set())
            with st.container(border=True, key=f"card_ureq_{combo_id}"):
                rc1, rc2 = st.columns(CARD_COLS[1:])
                rc1.markdown(f"**{change['source_key'].upper()}** — {change['label']}")
                rc1.caption(f"Requested by {', '.join(sorted(requested))} — waiting on **{change.get('staged_by') or 'the stager'}**.")
                if actor == change.get("staged_by") or is_admin:
                    if rc2.columns(2)[1].button("↩ Undo…", key=f"undo_requested_combo_{combo_id}", width='stretch', type="primary"):
                        open_undo_picker(combo_id, f"{change['source_key'].upper()} — {change['label']}")
        for combo_id in undo_requested_upc_groups:
            items = visible_by_combo[combo_id]
            first = items[0][1]
            requested = upc_group_undo_requests.get(combo_id, set())
            primary = upc_group_primaries.get(combo_id)
            with st.container(border=True, key=f"card_ureqg_{combo_id}"):
                rc1, rc2 = st.columns(CARD_COLS[1:])
                rc1.markdown(f"**{first.get('source_key', '').upper()}** — {first['label']}")
                rc1.caption(f"Requested by {', '.join(sorted(requested))} — waiting on **{primary or 'the first editor'}**.")
                if actor == primary or is_admin:
                    if rc2.columns(2)[1].button("↩ Undo…", key=f"undo_requested_upc_group_{combo_id}", width='stretch', type="primary"):
                        open_undo_picker(combo_id, f"{first.get('source_key', '').upper()} — {first['label']}")
    render_recent_pushes()


def render_dr_broken_out(pending_changes, pending_upc_changes, upc_change_suggestions) -> None:
    """Department Review → Broken Out."""
    url_group = st.session_state.pop("_url_group", None)
    if url_group and not get_shared_dept_filter()["search"]:
        # reopened from the address: show just that group
        with ENGINE.connect() as _c:
            lbl = dept_mapping._combo_labels(_c, [url_group]).get(url_group)
        if lbl:
            get_shared_dept_filter()["search"] = lbl[1]
            get_dept_tab_filters()["broken_out"] = {**get_shared_dept_filter()}
    broken_df = load_broken_out_combos()
    broken_df = broken_df[~broken_df["combo_id"].isin(pending_changes.keys())].copy()
    # "Left" = still-undecided items plus auto-decided ones nobody has
    # confirmed yet, minus whatever's already staged on Pending
    # Changes. A group whose every remaining item is staged drops off
    # this tab entirely (it lives on Pending Changes now), and comes
    # back automatically if those staged items are undone.
    staged_per_combo = pd.Series(
        [c["combo_id"] for c in pending_upc_changes.values()], dtype="int64"
    ).value_counts()
    broken_df["staged_count"] = broken_df["combo_id"].map(staged_per_combo).fillna(0).astype(int)
    broken_df["pending_count"] = (
        broken_df["override_count"] - broken_df["decided_count"] + broken_df["auto_count"]
        - broken_df["staged_count"]
    ).clip(lower=0)
    broken_df = broken_df[~((broken_df["pending_count"] == 0) & (broken_df["staged_count"] > 0))]
    if broken_df.empty:
        st.success("Nothing in Broken Out right now.")
    else:
        claims_all = load_broken_out_claims()
        me = st.session_state["name"]
        broken_df["worked_by"] = broken_df["combo_id"].map(
            lambda c: ("Mine" if (claims_all.get(c) or {}).get("claimed_by") == me
                       else "Someone else" if claims_all.get(c) else "Nobody"))
        broken_df["progress"] = (broken_df["decided_count"] / broken_df["override_count"].clip(lower=1)).round(3)
        search, sort_column, sort_desc, picks = render_filter_bar(
            "broken_out",
            {
                "Items Left": "pending_count", "# Items": "n_upcs_total", "Progress": "progress", "Source": "source_key",
                "Old Department": "raw_department", "Category": "raw_category", "Subcategory": "raw_subcategory",
            },
            "Search Broken Out Groups", "Search source, department, category, subcategory…",
            facets={"Source": "source_key", "Old Department": "raw_department", "Being worked on by": "worked_by"},
            df=broken_df,
        )
        filtered = apply_facets(search_groups(broken_df, search), picks)
        filtered = sort_full_df(filtered, sort_column, sort_desc)

        page_num_key = "broken_out_page_num"
        page_size, page_num, total_pages = render_page_controls(
            "broken_out", page_num_key, len(filtered),
            f"{len(filtered):,} group{'s' if len(filtered) != 1 else ''} · {int(filtered['pending_count'].sum()):,} items to decide",
        )

        start = (page_num - 1) * page_size
        page_df = filtered.iloc[start:start + page_size]
        if filtered.empty:
            st.info("No groups match these filters — Clear filters shows them all.")
        department_options = load_departments()["department"].tolist()
        actor = st.session_state["name"]
        page_combo_ids = [int(r["combo_id"]) for _, r in page_df.iterrows()]
        claims = dept_mapping.get_broken_out_claims(ENGINE, page_combo_ids)
        mine = [c for c in page_combo_ids if (claims.get(c) or {}).get("claimed_by") == actor]
        st.session_state["_url_group_now"] = mine[0] if mine else None
        upc_change_suggestions = load_upc_change_suggestions()
        undoable = undoable_groups()

        for _, row in page_df.iterrows():
            combo_id = int(row["combo_id"])
            label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
            label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
            staged_here = sum(1 for c in pending_upc_changes.values() if c["combo_id"] == combo_id)
            pending_sugg_here = sum(1 for s in upc_change_suggestions.values() if s[0]["combo_id"] == combo_id)
            send_back_label = "Send Back to Crosswalk" if row["tier"] == "review" else "Send Back to Unmatched"
            claim = claims.get(combo_id)
            with st.container(border=True, key=f"card_bo_{combo_id}"):
                c1, c_act = st.columns(CARD_COLS[1:], vertical_alignment="center")
                all_n, auto_n = int(row["override_count"]), int(row["auto_count"])
                left_n = all_n - int(row["decided_count"])
                done_n = all_n - left_n - auto_n
                c1.markdown(f"**{row['source_key'].upper()}** — {label}")
                # One bar, two colours: decided and live (green), then staged but
                # not pushed yet (orange); the legend under it says which is which.
                staged_n = min(staged_here, max(all_n - done_n, 0))
                pct = lambda n: f"{(100 * n / all_n) if all_n else 0:.2f}%"
                c1.markdown(
                    "<div style='display:flex;height:8px;border-radius:4px;overflow:hidden;margin:2px 0 6px;"
                    "background:rgba(250,250,250,0.1)'>"
                    f"<div style='width:{pct(done_n)};background:#21c354'></div>"
                    f"<div style='width:{pct(staged_n)};background:#ffa421'></div></div>",
                    unsafe_allow_html=True)
                bits = [f":green[●] **{done_n:,}** done", f":orange[●] **{staged_n:,}** staged", f"**{left_n - staged_n if left_n > staged_n else 0:,}** to decide"]
                if auto_n:
                    bits.append(f"{auto_n:,} auto")
                bits.append(f"{all_n:,} items")
                if pending_sugg_here:
                    bits.append(f"{pending_sugg_here} suggestion(s) waiting on Pending Changes")
                if claim and claim["claimed_by"] != actor:
                    claimed_when = pd.to_datetime(claim["claimed_at"], errors="coerce")
                    bits.append(f"{claim['claimed_by']} is working on this"
                                + (f" (since {claimed_when:%b %d %I:%M %p})" if pd.notna(claimed_when) else ""))
                elif claim:
                    bits.append("You're working on this group")
                c1.caption(" · ".join(bits))

                a1, a2 = top_row(c_act, combo_id in undoable)
                b1, b2 = c_act.columns(2)
                # Discards the item-level work and reopens the group for a whole-group
                # decision (undoable), after a popup that says what gets discarded.
                popup_button(a1, send_back_label.replace("Send Back to ", "Back to "), f"revert_broken_out_{combo_id}",
                             partial(request_send_back, combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                     send_back_label, is_whole=False),
                             help=f"{send_back_label} — discards this group's item decisions (undoable)")
                card_undo_button(a2, combo_id, f"{row['source_key'].upper()} — {label}", undoable, f"undo_card_bo_{combo_id}")

                # Working on a group is a temporary lock on bulk-deciding its
                # undecided items (never where a decision lives).
                if claim and claim["claimed_by"] != actor:
                    b1.button("Being worked on", key=f"claimed_by_other_{combo_id}", width='stretch', disabled=True)
                    if is_admin and b2.button("Force release", key=f"force_release_claim_{combo_id}", width='stretch'):
                        dept_mapping.release_broken_out_claim(ENGINE, combo_id, actor, is_admin=True)
                        load_broken_out_claims.clear()
                        st.session_state["_toast"] = (f"Force-released the claim on **{label}**.", "")
                        st.rerun()
                    continue

                if not claim:
                    if c_act.button("Work on this group", key=f"claim_broken_out_{combo_id}", type="primary", width='stretch',
                                 help="Opens its items for you — others see it's yours until you're done"):
                        result = dept_mapping.claim_broken_out_group(ENGINE, combo_id, actor)
                        load_broken_out_claims.clear()
                        if not result["claimed"]:
                            st.session_state["_toast"] = (f"{result['claimed_by']} started on this just before you — try another.", "")
                        else:
                            st.session_state[f"_open_bo_{combo_id}"] = True
                        st.rerun()
                    continue

                # claim["claimed_by"] == actor from here on.
                dept_mapping.touch_broken_out_claim(ENGINE, combo_id, actor)
                if c_act.button("Done — release it", key=f"release_claim_{combo_id}", width='stretch'):
                    dept_mapping.release_broken_out_claim(ENGINE, combo_id, actor)
                    load_broken_out_claims.clear()
                    st.rerun()

                def _stage_upc_decisions(decisions: dict) -> None:
                    # Re-checked against the DB fresh for every UPC,
                    # not trusted from this (possibly stale, possibly
                    # hours-old) browser tab — see
                    # stage_broken_out_decisions. A UPC someone else
                    # has since decided routes to a suggestion instead
                    # of silently overwriting or corrupting anything.
                    via = "Group Excel file" if st.session_state.get(f"_excel_staging__bo_stage_{combo_id}") else None
                    with st.spinner(f"Staging {len(decisions):,} item(s)..."), dept_mapping.activity_via(via or "In the app"):
                        with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Staged {len(decisions)} item decision(s)"):
                            results = dept_mapping.stage_broken_out_decisions(ENGINE, decisions, actor)
                    clear_dept_suggestion_caches()
                    decided_n = sum(1 for r in results.values() if r["status"] == "decided")
                    suggested_n = sum(1 for r in results.values() if r["status"] == "suggested")
                    blocked_n = sum(1 for r in results.values() if r["status"] == "blocked")
                    msg = f"Staged {decided_n} item decision(s) for **{row['source_key'].upper()} — {label}**." if decided_n else ""
                    if suggested_n:
                        msg += f" {suggested_n} item(s) were already decided by someone else since you started — sent as suggestion(s) instead."
                    if blocked_n:
                        msg += f" {blocked_n} item(s) already have too many suggestions — ask an admin."
                    if not msg:
                        msg = "Nothing staged."
                    st.session_state["_toast"] = (msg.strip(), "" if (suggested_n or blocked_n) else "")
                    tag = st.session_state.pop(f"_excel_staging__bo_stage_{combo_id}", None)
                    if tag:
                        last = dept_mapping.peek_undo_redo(ENGINE, actor)["undo"]
                        if last and last["combo_id"] == combo_id:
                            st.session_state.setdefault("_excel_staged_actions", {})[last["action_id"]] = tag
                    st.rerun()

                items_df = load_pending_upc_overrides(combo_id)
                items_df = items_df[~items_df["upc"].isin(pending_upc_changes.keys())]
                auto_decided_df = load_auto_decided_upc_overrides(combo_id)
                auto_decided_df = auto_decided_df[~auto_decided_df["upc"].isin(pending_upc_changes.keys())]
                display_items = items_df.rename(columns={
                    "upc": "UPC", "description": "Description", "brand": "Brand",
                    "pack": "Pack", "size": "Size", "uom": "UOM",
                    "suggested_department": "Suggested", "manually_edited_by": "Manually Edited By",
                })
                display_auto = auto_decided_df.rename(columns={
                    "upc": "UPC", "description": "Description", "brand": "Brand",
                    "pack": "Pack", "size": "Size", "uom": "UOM",
                    "department": "Auto Department", "decided_via": "Decided Via",
                    "manually_edited_by": "Manually Edited By",
                })
                picked = render_broken_out_editor(
                    combo_id, display_items, display_auto, department_options,
                    f"{row['source_key'].upper()} — {label}", f"{row['source_key']}_{combo_id}_items",
                )
                if picked:
                    descs = {**dict(zip(display_items["UPC"], display_items["Description"])),
                             **dict(zip(display_auto["UPC"], display_auto["Description"]))}
                    _stage_upc_decisions({
                        upc: {
                            "department": dept, "combo_id": combo_id, "label": label,
                            "description": descs.get(upc), "source_key": row["source_key"],
                        }
                        for upc, dept in picked.items()
                    })

        render_bottom_pagination("broken_out_page_size", page_num_key, "broken_out", total_pages)


def render_dr_decided(pending_changes, pending_upc_changes) -> None:
    """Department Review → Decided."""
    decided_df = load_decided_combos()
    decided_df = decided_df[~decided_df["combo_id"].isin(pending_changes.keys())]
    if decided_df.empty:
        st.info("Nothing decided yet.")
    else:
        decided_df = decided_df.assign(decided_by_person=decided_df["last_decided_by"].fillna("(automatic)"),
                                       decided_dept=decided_df["decided_department"].fillna("(item by item)"))
        search, sort_column, sort_desc, picks = render_filter_bar(
            "decided",
            {
                "# Items": "n_upcs_total", "Decided (newest)": "last_decided_at", "Source": "source_key",
                "Old Department": "raw_department", "Category": "raw_category", "Subcategory": "raw_subcategory",
                "Status": "status", "Decided Department": "decided_department",
            },
            "Search Decided Groups", "Search source, department, category, subcategory…",
            facets={"Source": "source_key", "Decided as": "decided_dept", "Status": "status", "Decided by": "decided_by_person"},
            df=decided_df,
        )
        filtered = apply_facets(search_groups(decided_df, search, extra_cols=("decided_department", "status")), picks)
        filtered = sort_full_df(filtered, sort_column, sort_desc)

        page_num_key = "decided_page_num"
        n_whole = int(filtered["status"].str.startswith("Whole Group").sum())
        page_size, page_num, total_pages = render_page_controls(
            "decided", page_num_key, len(filtered),
            f"{len(filtered):,} group{'s' if len(filtered) != 1 else ''} ({n_whole:,} as a whole, {len(filtered) - n_whole:,} item by item)"
            f" · {int(filtered['n_upcs_total'].sum()):,} items",
        )

        start = (page_num - 1) * page_size
        page_df = filtered.iloc[start:start + page_size]
        if filtered.empty:
            st.info("No groups match these filters — Clear filters shows them all.")
        actor = st.session_state["name"]
        department_options = load_departments()["department"].tolist()

        prefetch_affected_items(zip(page_df["combo_id"], page_df["n_upcs_total"]))
        undoable = undoable_groups()
        for _, row in page_df.iterrows():
            combo_id = int(row["combo_id"])
            label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
            label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
            is_whole = row["status"].startswith("Whole Group")
            with st.container(border=True, key=f"card_dec_{combo_id}"):
                c1, c_act = st.columns(CARD_COLS[1:])
                queue_name = {"review": "Crosswalk", "unmatched": "Unmatched"}.get(
                    dept_mapping.origin_tier(row["tier"], row["n_evidence"]), "Review")
                how = str(row["status"]).split(" — ")[-1]  # Auto / Manual / Partially Auto / Fully Auto
                c1.markdown(f"**{row['source_key'].upper()}** — {label}  \n"
                            + (f"Decided as **{row['decided_department']}**" if is_whole else "Decided item by item"))
                bits = [f"{int(row['n_upcs_total']):,} items", how]
                if pd.notna(row.get("last_decided_by")):
                    when = pd.to_datetime(row["last_decided_at"], errors="coerce")
                    bits.append(f"by {row['last_decided_by']}" + (f", {when:%b %d %Y}" if pd.notna(when) else ""))
                pushed_by = row.get("pushed_by")
                if pd.notna(pushed_by) and pushed_by != row.get("last_decided_by"):
                    bits.append(f"pushed by {pushed_by}")
                bits.append(f"from {queue_name}" + ("" if is_whole else " → Broken Out"))
                if is_whole and isinstance(row.get("decided_note"), str) and row["decided_note"]:
                    bits.append(short_note(row["decided_note"]))
                if not is_whole:
                    bits += [f"{short_note(t)} ({n:,} of {int(row['n_upcs_total']):,})"
                             for t, n in load_noted_item_counts().get(combo_id, {}).items()]
                n_ov = load_override_counts().get(combo_id, 0)
                if n_ov and is_whole:
                    bits.append(f"{n_ov:,} with a UPC override")
                c1.caption(" · ".join(bits))

                a1, a2 = top_row(c_act, combo_id in undoable)
                # Sends it back to where it came from: a whole group to its
                # queue; an item-by-item group to Broken Out or its queue.
                if is_whole:
                    popup_button(a1, f"Back to {queue_name}", f"revert_decided_{combo_id}",
                                 partial(request_send_back, combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                         f"Send Back to {queue_name}", is_whole),
                                 help=f"Send Back to {queue_name} for a fresh decision (undoable)")
                    card_undo_button(a2, combo_id, f"{row['source_key'].upper()} — {label}", undoable, f"undo_card_dec_{combo_id}")
                    dc1, dc2 = c_act.columns([2.3, 1], vertical_alignment="bottom")
                    new_dept = dc1.selectbox(
                        "New department", [d for d in department_options if d != row["decided_department"]], index=None,
                        key=f"decided_change_dept_{combo_id}", label_visibility="collapsed", placeholder="Change Department",
                        help="Stages the new Department on Pending Changes — the current one stays until it's pushed.",
                    )
                    if dc2.button("Stage", key=f"decided_change_dept_btn_{combo_id}", width='stretch', disabled=not new_dept):
                        with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Staged a change to {new_dept}"):
                            result = dept_mapping.upsert_combo_suggestion(
                                ENGINE, combo_id, row.get("tier"), new_dept, row["source_key"],
                                label, int(row["n_upcs_total"]), actor, is_admin=is_admin,
                            )
                        clear_dept_suggestion_caches()
                        if result.get("locked"):
                            st.session_state["_toast"] = (f"**{label}** is locked by an admin override — ask them to remove it first.", "")
                        elif not result["disputed"]:
                            st.session_state["_toast"] = (f"Staged: **{label}** → {new_dept}. See Pending Changes to push.", "")
                        else:
                            st.session_state["_toast"] = (f"Staged as a suggestion for **{label}** — see Pending Changes.", "")
                        st.rerun()
                    if row["status"] == "Whole Group — Auto":
                        # confirming an automatic decision just marks it as reviewed by a person
                        if c_act.button("Mark as reviewed", key=f"confirm_whole_{combo_id}", width='stretch'):
                            with track(combo_id, f"{row['source_key'].upper()} — {label}", "Marked as reviewed"):
                                dept_mapping.confirm_combo_decision(ENGINE, combo_id, st.session_state["name"])
                            load_decided_combos.clear()
                            st.rerun()
                else:
                    popup_button(a1, "Back to Broken Out", f"revert_decided_{combo_id}",
                                 partial(request_break_out, combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                         reopen=True, fully_auto=row["status"] == "Broken Out — Fully Auto"),
                                 help="Send back to Broken Out to decide its items again")
                    card_undo_button(a2, combo_id, f"{row['source_key'].upper()} — {label}", undoable, f"undo_card_dec_{combo_id}")
                    b1, _ = c_act.columns(2)
                    popup_button(b1, f"Back to {queue_name}", f"revert_decided_queue_{combo_id}",
                                 partial(request_send_back, combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                         f"Send Back to {queue_name}", is_whole),
                                 help=f"Send back to {queue_name} for a fresh whole-group decision")
                if is_whole:
                    render_affected_items_expander(combo_id, int(row["n_upcs_total"]), "decided_items")
                else:
                    with st.expander(f"Show item decisions ({int(row['n_upcs_total']):,})", key=f"decided_broken_expander_{combo_id}"):
                        items_df = load_combo_upc_decisions(combo_id)
                        if items_df.empty:
                            st.caption("No item data found.")
                        else:
                            auto_upcs = items_df.loc[items_df["decided_via"].str.startswith("Auto-Applied", na=False), "upc"].tolist()
                            if auto_upcs:
                                if st.button(
                                    f"Confirm all {len(auto_upcs)} auto-decided item(s) as reviewed",
                                    key=f"confirm_broken_{combo_id}",
                                ):
                                    with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Confirmed {len(auto_upcs)} auto-decided item(s)"):
                                        dept_mapping.confirm_upc_decisions(ENGINE, auto_upcs, st.session_state["name"])
                                    load_combo_upc_decisions.clear()
                                    load_decided_combos.clear()
                                    st.rerun()
                            # Changing an item here is a real decision, so it's
                            # staged to Pending Changes (push + undo there) rather
                            # than applied — same as deciding it on Broken Out.
                            st.caption("Fill in **New Department** to change an item — it's staged on Pending Changes.")
                            display = items_df.rename(columns={
                                "upc": "UPC", "description": "Description", "brand": "Brand",
                                "department": "Department", "decided_via": "Decided Via",
                                "decided_by": "Decided By", "pushed_by": "Pushed By",
                            }).copy()
                            display["Staged"] = display["UPC"].map(
                                lambda u: pending_upc_changes[u]["department"] if u in pending_upc_changes else ""
                            )
                            picked = render_item_workbench(
                                f"dc_{combo_id}", display, department_options, value_label="New Department",
                                title=f"{row['source_key'].upper()} — {label}",
                                info_cols=["Department", "Description", "Staged", "Decided Via", "Decided By",
                                           "Brand", "Pushed By", "UPC"],
                                stage_label="Stage changes",
                            )
                            if picked:
                                current = dict(zip(display["UPC"], display["Department"]))
                                descs = dict(zip(display["UPC"], display["Description"]))
                                decisions = {
                                    upc: {
                                        "department": dept, "combo_id": combo_id, "label": label,
                                        "description": descs.get(upc), "source_key": row["source_key"],
                                    }
                                    for upc, dept in picked.items() if dept != current.get(upc)
                                }
                                reset_item_workbench(f"dc_{combo_id}")
                                if not decisions:
                                    st.session_state["_toast"] = ("Those are already the current departments — nothing to stage.", "ℹ️")
                                    st.rerun()
                                with st.spinner(f"Staging {len(decisions):,} item(s)..."):
                                    with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Staged {len(decisions)} item change(s) on Decided"):
                                        results = dept_mapping.stage_broken_out_decisions(ENGINE, decisions, actor, is_admin=is_admin)
                                clear_dept_suggestion_caches()
                                n_dec = sum(1 for r in results.values() if r["status"] == "decided")
                                n_sugg = sum(1 for r in results.values() if r["status"] == "suggested")
                                st.session_state["_toast"] = (
                                    f"Staged {n_dec} change(s) for **{label}** — see Pending Changes."
                                    + (f" {n_sugg} sent as suggestion(s) to whoever already staged them." if n_sugg else ""),
                                    "",
                                )
                                st.rerun()

        render_bottom_pagination("decided_page_size", page_num_key, "decided", total_pages)


def undoable_groups() -> set:
    """Groups with something Undo… can take back: unpushed moves or staged work."""
    ids = {m["combo_id"] for m in load_dept_recent_moves()}
    ids |= {c["combo_id"] for c in load_dept_pending_upc_changes().values()}
    ids |= set(load_dept_pending_changes()) | set(load_combo_suggestions())
    return ids


def top_row(col, has_undo: bool) -> tuple:
    """A card's top-right row: [its action][↩ Undo…] — or, with nothing to
    undo, the action alone across the whole row."""
    return tuple(col.columns([1.55, 1])) if has_undo else (col.container(), None)  # a container keeps its place at the top


def card_undo_button(container, combo_id: int, label: str, undoable: set, key: str) -> None:
    """Every group card's Undo…: back through its unpushed steps, or — once
    pushed — nothing to undo (Send Back changes it instead)."""
    # Only shown when there's something to take back; otherwise its top-right
    # slot stays empty, so the card's other buttons don't move.
    if combo_id not in undoable:
        return
    popup_button(container, "↩ Undo…", key, partial(open_undo_picker, combo_id, label))


def render_dr_review_queue(review_subtab, pending_changes, combo_suggestions) -> None:
    """Department Review → Crosswalk or Unmatched."""
    tier = "review" if review_subtab == "Crosswalk" else "unmatched"
    warm_break_out_cache()

    queue_df = load_dept_review_queue(tier)
    combo_suggestions = load_combo_suggestions()
    queue_df = queue_df[~queue_df["combo_id"].isin(set(pending_changes.keys()) | set(combo_suggestions.keys()))]
    department_options = load_departments()["department"].tolist()

    if queue_df.empty:
        st.success(f"Nothing waiting in {review_subtab} — every {review_subtab.lower()} group has already been decided.")
    else:
        search, sort_column, sort_desc, picks = render_filter_bar(
            f"dept_review_{tier}",
            {
                "# Items": "n_upcs_total", "Source": "source_key", "Old Department": "raw_department",
                "Category": "raw_category", "Subcategory": "raw_subcategory",
                "Suggested Department": "suggested_department",
            },
            f"Search {review_subtab}", "Search source, department, category, subcategory…",
            facets={"Source": "source_key", "Old Department": "raw_department", "Suggested": "suggested_department"},
            df=queue_df,
        )
        filtered = apply_facets(search_groups(queue_df, search), picks)
        filtered = sort_full_df(filtered, sort_column, sort_desc)

        page_num_key = f"dept_review_page_num_{tier}"
        page_size, page_num, total_pages = render_page_controls(
            f"dept_review_{tier}", page_num_key, len(filtered),
            f"{len(filtered):,} group{'s' if len(filtered) != 1 else ''} · {int(filtered['n_upcs_total'].sum()):,} items",
        )

        start = (page_num - 1) * page_size
        page_df = filtered.iloc[start:start + page_size]
        if filtered.empty:
            st.info("No groups match these filters — Clear filters shows them all.")

        # Many at once: every group on this page with a suggestion, as suggested.
        suggested_rows = page_df[page_df["suggested_department"].notna()]
        if len(suggested_rows) > 1:
            with st.popover(f"Approve all {len(suggested_rows)} on this page as suggested…"):
                st.markdown(", ".join(f"**{d}** ×{n}" for d, n in suggested_rows["suggested_department"].value_counts().items()))
                st.caption(f"{int(suggested_rows['n_upcs_total'].sum()):,} item(s). Each is staged on Pending Changes "
                           "(nothing goes live until pushed) and has its own Undo….")
                if st.button(f"Approve these {len(suggested_rows)}", key=f"approve_page_{tier}", type="primary"):
                    n_disputed = 0
                    for _, r in suggested_rows.iterrows():
                        lbl = " / ".join(b for b in (r["raw_department"], r["raw_category"], r["raw_subcategory"]) if b) \
                            or "(blank Department/Category/Subcategory)"
                        with track(int(r["combo_id"]), f"{r['source_key'].upper()} — {lbl}", f"Approved as {r['suggested_department']}"):
                            res = dept_mapping.upsert_combo_suggestion(
                                ENGINE, int(r["combo_id"]), tier, r["suggested_department"], r["source_key"], lbl,
                                int(r["n_upcs_total"]), st.session_state["name"])
                        n_disputed += bool(res.get("disputed"))
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Staged {len(suggested_rows) - n_disputed} group(s) as suggested"
                                                  + (f"; {n_disputed} went to Needs agreement" if n_disputed else "")
                                                  + " — see Pending Changes.", "")
                    st.rerun()

        prefetch_affected_items(zip(page_df["combo_id"], page_df["n_upcs_total"]))
        undoable = undoable_groups()
        for _, row in page_df.iterrows():
            combo_id = int(row["combo_id"])
            label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
            label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
            with st.container(border=True, key=f"card_q_{tier}_{combo_id}"):
                c1, c_act = st.columns(CARD_COLS[1:])
                suggested = row["suggested_department"]
                c1.markdown(f"**{row['source_key'].upper()}** — {label}  \n"
                            + (f"Suggested **{suggested}**" if isinstance(suggested, str) and suggested else "No suggestion yet"))
                bits = [f"{int(row['n_upcs_total']):,} items", evidence_sentence(row)]
                n_ov = load_override_counts().get(combo_id, 0)
                if n_ov:
                    bits.append(f"{n_ov:,} with a UPC override" + (" (all of them)" if n_ov >= int(row["n_upcs_total"]) else ""))
                c1.caption(" · ".join(b for b in bits if b))

                break_col, undo_col = top_row(c_act, combo_id in undoable)
                dept_col, approve_col = c_act.columns([2.3, 1], vertical_alignment="bottom")
                options_with_blank = [""] + department_options
                default_index = options_with_blank.index(suggested) if suggested in options_with_blank else 0
                chosen_dept = dept_col.selectbox(
                    "Department", options_with_blank, index=default_index,
                    key=f"dept_choice_{tier}_{combo_id}", label_visibility="collapsed",
                    placeholder="Pick a Department",
                )
                if approve_col.button("Approve", key=f"approve_{tier}_{combo_id}", type="primary", width='stretch'):
                    if not chosen_dept:
                        st.error("Pick a Department first.")
                    else:
                        with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Approved as {chosen_dept}"):
                            result = dept_mapping.upsert_combo_suggestion(
                                ENGINE, combo_id, tier, chosen_dept, row["source_key"], label,
                                int(row["n_upcs_total"]), st.session_state["name"],
                            )
                        clear_dept_suggestion_caches()
                        if result["disputed"]:
                            st.session_state["_toast"] = (
                                f"**{row['source_key'].upper()} — {label}** now has more than one suggested "
                                f"department ({', '.join(result['departments'])}) — see Pending Changes to discuss and agree.",
                                "",
                            )
                        else:
                            st.session_state["_toast"] = (
                                f"Staged: **{row['source_key'].upper()} — {label}** → {chosen_dept} "
                                f"({int(row['n_upcs_total']):,} item(s)). See Pending Changes to push it.",
                                "",
                            )
                        st.rerun()
                card_undo_button(undo_col, combo_id, f"{row['source_key'].upper()} — {label}", undoable, f"undo_card_{tier}_{combo_id}")
                # Not a department decision: it moves the group to the item-by-item
                # queue (undoable from Undo…), after a popup that asks where to start.
                popup_button(break_col, "Break Out", f"breakout_{tier}_{combo_id}",
                             partial(request_break_out, combo_id, row["source_key"], label, int(row["n_upcs_total"])),
                             help="Decide this group item by item instead (moves it to Broken Out now)")
                render_affected_items_expander(combo_id, int(row["n_upcs_total"]), "review_items")

        render_bottom_pagination(f"dept_review_{tier}_page_size", page_num_key, f"dept_review_{tier}", total_pages)


DR_SUBTAB_HELP = {
    "Crosswalk": "Some evidence, not enough to decide on its own. Approve a Department, or Break Out.",
    "Unmatched": "No evidence at all. Pick a Department, or Break Out.",
    "Broken Out": "Decided item by item. A group moves to Decided once every item is decided.",
    "Pending Changes": "Staged, not live yet. Push makes it live.",
    "Decided": "Finished groups. Stage a new Department, or send one back.",
}
DR_SUBTAB_MORE = {
    "Crosswalk": "Approve stages the decision on Pending Changes (Push makes it live). Break Out moves the group to "
                 "Broken Out right away — undoable from its Undo… button.",
    "Unmatched": "A saved Unmatched Default (Settings) fills in the suggestion. Approve stages the decision; Break Out "
                 "moves the group right away — undoable from its Undo… button.",
    "Broken Out": "Work on a group to open its items. Stage sends your picks to Pending Changes. Send Back discards the "
                  "group's item decisions and returns it for a whole-group decision.",
    "Pending Changes": "Include decides what the next Push takes. A different suggestion from someone else sends a group to "
                       "Needs agreement. Break Out / Send Back moves are listed under Recent moves, each with Undo….",
    "Decided": "Status says how it was decided. A pushed decision can't be undone — Send Back (itself undoable) or "
               "stage a different Department instead.",
}


def render_department_review_tab() -> None:
    """The Department Review tab: shared loading, then the chosen sub-tab."""
    st.subheader("Department Review")
    render_merge_staleness_banner()
    pending_changes = load_dept_pending_changes()
    pending_upc_changes = load_dept_pending_upc_changes()
    combo_suggestions = load_combo_suggestions()
    upc_change_suggestions = load_upc_change_suggestions()
    recent_moves = load_dept_recent_moves()
    # Counted in items, not "changes" — a single Approve on a
    # 13,000-item Crosswalk group and 176 individually-staged Broken
    # Out items used to both just say "1" and "176" respectively,
    # which made the number meaningless as a "how much is about to
    # change" warning. Counting both the same way (by UPC) makes it
    # an honest measure of real scale either way. Disputed combos
    # count too (staged, not push-eligible yet) — a pending UPC
    # suggestion doesn't add to the count separately, since the UPC
    # it targets is already decided and counted via pending_upc_changes.
    total_pending_upcs = (
        sum(c["n_upcs_total"] for c in pending_changes.values()) + len(pending_upc_changes)
        + sum(s[0]["n_upcs_total"] for s in combo_suggestions.values())
    )
    # Settings (Departments, Strict Departments, defaults, the workbook
    # tools) is for admins; editors see a form to request a change instead.
    review_subtabs = ["Crosswalk", "Unmatched", "Broken Out", "Pending Changes", "Decided", "Settings"]
    if st.session_state.get("dept_review_subtab") not in (None, *review_subtabs):
        st.session_state.pop("dept_review_subtab")
    if "dept_review_subtab" not in st.session_state and st.query_params.get("sub") in review_subtabs:
        st.session_state["dept_review_subtab"] = st.query_params["sub"]
    review_subtab = st.radio(
        "Department Review section", review_subtabs,
        horizontal=True, label_visibility="collapsed", key="dept_review_subtab",
    )

    line = DR_SUBTAB_HELP.get(review_subtab)
    if total_pending_upcs and review_subtab != "Pending Changes":
        line = (line + " · " if line else "") + f"{total_pending_upcs:,} item(s) staged, not pushed yet"
    if line:
        st.caption(line, help=DR_SUBTAB_MORE.get(review_subtab))

    if not show_dept_dialog():
        st.empty()  # holds the popup's place, so closing one doesn't shift (and redraw) the page below

    if review_subtab == 'Settings' and not is_admin:
        render_settings_request_form()
    elif review_subtab == 'Settings':
        render_dr_settings()
    elif review_subtab == 'Pending Changes':
        render_dr_pending_changes(pending_changes, pending_upc_changes, combo_suggestions, upc_change_suggestions, recent_moves, total_pending_upcs)
    elif review_subtab == 'Broken Out':
        render_dr_broken_out(pending_changes, pending_upc_changes, upc_change_suggestions)
    elif review_subtab == 'Decided':
        render_dr_decided(pending_changes, pending_upc_changes)
    elif review_subtab in ('Crosswalk', 'Unmatched'):
        render_dr_review_queue(review_subtab, pending_changes, combo_suggestions)


# -------------------------------------------------------------------
# Add Item
# -------------------------------------------------------------------
def render_add_item_tab() -> None:
    """The Add Item tab."""
    st.subheader("Add a new item")
    item_master_pending = item_master_pending_cross_link()
    render_bulk_upload("add", "Adding many items")
    # A dropdown, not free text — Department is a real, bounded list
    # elsewhere in the app (Item Master edit, UPC Overrides), and typed
    # free text here let one typo silently create a bogus new
    # department that then had to be cleaned up by hand later. Click
    # the dropdown and type to filter/search it, same as any other
    # Streamlit selectbox — no separate search box needed.
    add_item_department_options = [""] + load_departments()["department"].tolist()
    with st.form("add_item_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        new_upc = c1.text_input("UPC *")
        new_description = c2.text_input("Description *")
        new_department = c1.selectbox("Department", add_item_department_options)
        new_category = c2.text_input("Category")
        new_subcategory = c1.text_input("Subcategory")
        new_brand = c2.text_input("Brand")
        new_pack = c1.text_input("Pack (optional)")
        new_size = c2.text_input("Size (optional)")
        new_uom = c1.text_input("UOM (optional)")
        submitted = st.form_submit_button("Add Item", type="primary")

        if submitted:
            typed_upc = new_upc
            new_upc = clean_upc(new_upc) if new_upc else new_upc
            if not typed_upc or not new_description:
                st.error("UPC and Description are required.")
            elif new_upc == INVALID_UPC:
                st.error(f"“{typed_upc}” isn't a valid UPC.")
            elif upc_exists(new_upc):
                st.error(f"UPC {new_upc} already exists.")
            elif new_upc in item_master_pending:
                render_blocked_item_master_edits({new_upc: item_master_pending[new_upc]})
            else:
                blocked = dept_mapping.save_item_master_pending(
                    ENGINE, new_upc, "add", new_description, new_department or None,
                    new_category or None, new_subcategory or None, new_brand or None,
                    st.session_state["name"], pack=new_pack or None, size=new_size or None,
                    uom=new_uom or None,
                )
                load_item_master_pending.clear()
                if blocked:
                    render_blocked_item_master_edits({new_upc: blocked})
                else:
                    st.success(f"Staged adding {new_upc} — {new_description}. See Pending Changes to review and push.")
                    st.rerun()

    st.divider()
    st.subheader("Manually added items")
    st.caption(
        'Items added by hand. They always survive a Merge; removed ones can be restored from Delete Item.',
        help=("Items added here by hand (not pulled in from any distributor file) — these always "
        "survive a Merge. Remove one to delete it from the item master entirely; it's kept "
        "under Deleted Items on the Delete Item tab so it can be restored later if needed."),
    )
    manual_df = load_manual_items()
    manual_df = manual_df[~manual_df["UPC"].isin(item_master_pending.keys())]
    if manual_df.empty:
        st.caption("No manually-added items yet.")
    else:
        manual_search = st.text_input(
            "Search manually-added items by description or UPC (optional — narrows the table below)",
            key="manual_items_search",
        )
        manual_matches = manual_df
        if manual_search:
            ms = manual_search.lower()
            manual_matches = manual_df[
                manual_df["Description"].fillna("").str.lower().str.contains(ms)
                | manual_df["UPC"].str.contains(ms)
            ]

        matched_count = len(manual_matches)
        if matched_count == 0:
            st.info("No manually-added items match that search.")
        else:
            # Same paging pattern as Item Master / Deleted Items — a page
            # is always shown by default, search just narrows it.
            mpage_size_key, mpage_num_key = "manual_items_page_size", "manual_items_page_num"
            page_size_options = [50, 100, 200, 500, 1000]
            default_index = page_size_options.index(200)
            page_size = st.session_state.get(mpage_size_key, 200)
            total_pages = max(1, (matched_count - 1) // page_size + 1)
            if st.session_state.get(mpage_num_key, 1) > total_pages:
                st.session_state[mpage_num_key] = total_pages

            mcol1, mcol2, mcol3 = st.columns([1, 1, 3])
            page_size = mcol1.selectbox("Rows per page", page_size_options, index=default_index, key=mpage_size_key)
            total_pages = max(1, (matched_count - 1) // page_size + 1)
            if st.session_state.get(mpage_num_key, 1) > total_pages:
                st.session_state[mpage_num_key] = total_pages
            page_num = mcol2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=mpage_num_key)
            with mcol3.container(key="manual_page_caption"):
                st.caption(f"{matched_count} manually-added item(s) — page {page_num} of {total_pages}")

            start = (page_num - 1) * page_size
            manual_page_df = manual_matches.iloc[start:start + page_size]

            st.dataframe(manual_page_df, width='stretch', hide_index=True)

            remove_options = manual_page_df.apply(
                lambda r: f"{r['UPC']} — {r['Description']}", axis=1
            )
            remove_choice = st.selectbox("Select item to remove (from this page)", remove_options)
            upc_to_remove = remove_choice.split(" — ")[0]
            if st.button("Remove Manually-Added Item", type="secondary"):
                remove_row = manual_page_df[manual_page_df["UPC"] == upc_to_remove].iloc[0]
                push_item_master_delete(upc_to_remove, {
                    f: sql_value(remove_row[ITEM_MASTER_FIELD_TO_DF_COLUMN[f]])
                    for f in ("description", "department", "category", "subcategory", "brand", "pack", "size", "uom")
                }, st.session_state["name"])
                activity("Items", "Removed a manually-added item (live right away)", upc_to_remove, 1)
                load_items.clear()
                load_manual_items.clear()
                load_deleted_items.clear()
                st.success(f"Removed {upc_to_remove}. It's kept under Deleted Items if you need to restore it.")
                st.rerun()


# -------------------------------------------------------------------
# Delete Item
# -------------------------------------------------------------------
def render_delete_item_tab() -> None:
    """The Delete Item tab."""
    st.subheader("Delete an item")
    item_master_pending = item_master_pending_cross_link()
    render_bulk_upload("delete", "Deleting many items")
    df = load_items()
    df = df[~df["UPC"].isin(item_master_pending.keys())]
    if df.empty:
        st.info("No items in the table.")
    else:
        delete_search = st.text_input("Search by description or UPC to find the item to delete")
        if not delete_search:
            st.caption(f"Type part of a description or UPC to find it among {len(df):,} items.")
        else:
            s = delete_search.lower()
            matches = df[
                df["Description"].str.lower().str.contains(s, na=False)
                | df["UPC"].str.contains(s, na=False)
            ]
            if matches.empty:
                st.info("No items match that search.")
            else:
                MAX_DELETE_MATCHES = 200
                if len(matches) > MAX_DELETE_MATCHES:
                    st.warning(f"{len(matches)} items match — showing the first {MAX_DELETE_MATCHES}. Narrow your search to find a specific item.")
                    matches = matches.head(MAX_DELETE_MATCHES)

                options = matches.apply(lambda r: f"{r['UPC']} — {r['Description']}", axis=1)
                choice = st.selectbox(f"Select item to delete ({len(matches)} match(es))", options)
                upc_to_delete = choice.split(" — ")[0]
                st.warning(f"Stages deleting {upc_to_delete}. Once pushed, no Merge brings it back; it can be restored "
                           "from Deleted Items below.")
                if st.button("Delete Item", type="secondary"):
                    item_row = matches[matches["UPC"] == upc_to_delete].iloc[0]
                    blocked = dept_mapping.save_item_master_pending(
                        ENGINE, upc_to_delete, "delete",
                        sql_value(item_row["Description"]), sql_value(item_row["Department"]),
                        sql_value(item_row["Category"]), sql_value(item_row["Subcategory"]),
                        sql_value(item_row["Brand"]), st.session_state["name"],
                        pack=sql_value(item_row["Pack"]), size=sql_value(item_row["Size"]),
                        uom=sql_value(item_row["UOM"]),
                    )
                    load_item_master_pending.clear()
                    if blocked:
                        render_blocked_item_master_edits({upc_to_delete: blocked})
                    else:
                        st.success(f"Staged deleting {upc_to_delete}. See Pending Changes to review and push.")
                        st.rerun()

    st.divider()
    st.subheader("Deleted items")
    st.caption("Items deleted here are excluded from every future Merge until restored.")
    deleted_df = load_deleted_items()
    if deleted_df.empty:
        st.caption("No items have been deleted.")
    else:
        restore_search = st.text_input("Search deleted items by description or UPC (optional — narrows the table below)", key="restore_search")
        deleted_matches = deleted_df
        if restore_search:
            rs = restore_search.lower()
            deleted_matches = deleted_df[
                deleted_df["description"].fillna("").str.lower().str.contains(rs)
                | deleted_df["upc"].str.contains(rs)
            ]

        matched_count = len(deleted_matches)
        if matched_count == 0:
            st.info("No deleted items match that search.")
        else:
            # Same paging pattern as Item Master — a page is always shown
            # by default (no search required); as the deleted list grows,
            # search narrows it and the table/dropdown refresh to match.
            dpage_size_key, dpage_num_key = "restore_page_size", "restore_page_num"
            page_size_options = [50, 100, 200, 500, 1000]
            default_index = page_size_options.index(200)
            page_size = st.session_state.get(dpage_size_key, 200)
            total_pages = max(1, (matched_count - 1) // page_size + 1)
            if st.session_state.get(dpage_num_key, 1) > total_pages:
                st.session_state[dpage_num_key] = total_pages

            dcol1, dcol2, dcol3 = st.columns([1, 1, 3])
            page_size = dcol1.selectbox("Rows per page", page_size_options, index=default_index, key=dpage_size_key)
            total_pages = max(1, (matched_count - 1) // page_size + 1)
            if st.session_state.get(dpage_num_key, 1) > total_pages:
                st.session_state[dpage_num_key] = total_pages
            page_num = dcol2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=dpage_num_key)
            with dcol3.container(key="deleted_page_caption"):
                st.caption(f"{matched_count} deleted item(s) — page {page_num} of {total_pages}")

            start = (page_num - 1) * page_size
            deleted_page_df = deleted_matches.iloc[start:start + page_size]

            st.dataframe(deleted_page_df, width='stretch', hide_index=True)

            restore_options = deleted_page_df.apply(
                lambda r: f"{r['upc']} — {r['description'] or '(no description saved)'}", axis=1
            )
            restore_choice = st.selectbox("Select item to restore (from this page)", restore_options)
            upc_to_restore = restore_choice.split(" — ")[0]
            if st.button("Restore Item", type="secondary"):
                restore_row = deleted_page_df[deleted_page_df["upc"] == upc_to_restore].iloc[0]
                activity("Items", "Restored a deleted item (live right away)", upc_to_restore, 1)
                with db_begin() as conn:
                    conn.execute(text("DELETE FROM dbo.deleted_upcs WHERE upc = :upc"), {"upc": upc_to_restore})
                    conn.execute(
                        text(
                            """
                            MERGE dbo.items AS target
                            USING (SELECT :upc AS upc) AS src ON target.upc = src.upc
                            WHEN MATCHED THEN UPDATE SET
                                description = :description, department = :department,
                                category = :category, subcategory = :subcategory, brand = :brand,
                                pack = :pack, size = :size, uom = :uom,
                                updated_at = SYSUTCDATETIME()
                            WHEN NOT MATCHED THEN INSERT
                                (upc, description, department, category, subcategory, brand, pack, size, uom)
                            VALUES
                                (:upc, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom);
                            """
                        ),
                        {
                            "upc": upc_to_restore,
                            "description": sql_value(restore_row["description"]) or upc_to_restore,
                            "department": sql_value(restore_row["department"]),
                            "category": sql_value(restore_row["category"]),
                            "subcategory": sql_value(restore_row["subcategory"]),
                            "brand": sql_value(restore_row["brand"]),
                            "pack": sql_value(restore_row["pack"]),
                            "size": sql_value(restore_row["size"]),
                            "uom": sql_value(restore_row["uom"]),
                        },
                    )
                    # Restoring is a manual decision, same as Add Item —
                    # persist it so it also survives the next Merge.
                    conn.execute(
                        text(
                            """
                            MERGE dbo.manual_overrides AS target
                            USING (SELECT :upc AS upc) AS src ON target.upc = src.upc
                            WHEN MATCHED THEN UPDATE SET
                                description = :description, department = :department,
                                category = :category, subcategory = :subcategory, brand = :brand,
                                pack = :pack, size = :size, uom = :uom,
                                updated_by = :updated_by, updated_at = SYSUTCDATETIME()
                            WHEN NOT MATCHED THEN INSERT
                                (upc, description, department, category, subcategory, brand, pack, size, uom, updated_by)
                            VALUES
                                (:upc, :description, :department, :category, :subcategory, :brand, :pack, :size, :uom, :updated_by);
                            """
                        ),
                        {
                            "upc": upc_to_restore,
                            "description": sql_value(restore_row["description"]) or upc_to_restore,
                            "department": sql_value(restore_row["department"]),
                            "category": sql_value(restore_row["category"]),
                            "subcategory": sql_value(restore_row["subcategory"]),
                            "brand": sql_value(restore_row["brand"]),
                            "pack": sql_value(restore_row["pack"]),
                            "size": sql_value(restore_row["size"]),
                            "uom": sql_value(restore_row["uom"]),
                            "updated_by": st.session_state["name"],
                        },
                    )
                load_items.clear()
                load_deleted_items.clear()
                load_manual_items.clear()
                st.success(f"Restored {upc_to_restore}.")
                st.rerun()


# -------------------------------------------------------------------
# UPC Overrides
# -------------------------------------------------------------------
def render_upc_overrides_tab() -> None:
    """The UPC Overrides tab."""
    st.subheader("Edit one item's attributes")
    st.caption(
        "Find one item and change its fields. It's staged on Pending Changes.",
        help=("Search for a single item and edit its fields directly — Department is a dropdown here, "
        "so there's no risk of a typo creating a department that doesn't really exist (unlike the "
        "free-text grid on Item Master). Submitting stages the change; it shows up on Pending "
        "Changes for every editor immediately, and Push there actually applies it."),
    )
    item_master_pending = item_master_pending_cross_link()
    render_bulk_upload("edit", "Changing many items (Departments or any other fields)")
    df = load_items()
    df = df[~df["UPC"].isin(item_master_pending.keys())]
    search = st.text_input("Search by description or UPC", key="upc_override_search")
    if not search:
        st.caption(f"Type part of a description or UPC to find it among {len(df):,} items.")
    else:
        s = search.lower()
        matches = df[
            df["Description"].str.lower().str.contains(s, na=False)
            | df["UPC"].str.contains(s, na=False)
        ]
        if matches.empty:
            st.info("No items match that search.")
        else:
            MAX_OVERRIDE_MATCHES = 200
            if len(matches) > MAX_OVERRIDE_MATCHES:
                st.warning(f"{len(matches)} items match — showing the first {MAX_OVERRIDE_MATCHES}. Narrow your search to find a specific item.")
                matches = matches.head(MAX_OVERRIDE_MATCHES)

            options = matches.apply(lambda r: f"{r['UPC']} — {r['Description']}", axis=1)
            choice = st.selectbox(f"Select item to edit ({len(matches)} match(es))", options, key="upc_override_choice")
            upc = choice.split(" — ")[0]
            row = matches[matches["UPC"] == upc].iloc[0]
            department_options = load_departments()["department"].tolist()
            current_description = sql_value(row["Description"]) or ""
            current_department_raw = sql_value(row["Department"])
            # If the item's current Department isn't one of the real
            # configured departments (legacy raw text, or blank),
            # inject it as an extra option so the dropdown can default
            # to it exactly — defaulting to blank instead would mean
            # submitting an unrelated edit (e.g. just fixing Brand)
            # without touching this field would silently CLEAR a real
            # non-standard value the user never meant to touch.
            dept_select_options = [""] + department_options
            if current_department_raw and current_department_raw not in department_options:
                dept_select_options = [current_department_raw] + dept_select_options
            current_category = sql_value(row["Category"]) or ""
            current_subcategory = sql_value(row["Subcategory"]) or ""
            current_brand = sql_value(row["Brand"]) or ""
            current_pack = sql_value(row["Pack"]) or ""
            current_size = sql_value(row["Size"]) or ""
            current_uom = sql_value(row["UOM"]) or ""
            current_source_key = sql_value(row["SourceKey"]) or ""

            with st.form(f"upc_override_form_{upc}"):
                c1, c2 = st.columns(2)
                new_description = c1.text_input("Description", value=current_description, key=f"ov_desc_{upc}")
                new_department = c2.selectbox(
                    "Department", dept_select_options,
                    index=dept_select_options.index(current_department_raw or ""), key=f"ov_dept_{upc}",
                )
                if current_department_raw and current_department_raw not in department_options:
                    c2.caption(f"“{current_department_raw}” isn't one of the configured departments — pick a real one to fix it, or leave as-is to keep it unchanged.")
                new_category = c1.text_input("Category", value=current_category, key=f"ov_cat_{upc}")
                new_subcategory = c2.text_input("Subcategory", value=current_subcategory, key=f"ov_subcat_{upc}")
                new_brand = c1.text_input("Brand", value=current_brand, key=f"ov_brand_{upc}")
                new_source_key = c2.text_input("Source Key", value=current_source_key, key=f"ov_source_{upc}")
                new_pack = c1.text_input("Pack (optional)", value=current_pack, key=f"ov_pack_{upc}")
                new_size = c2.text_input("Size (optional)", value=current_size, key=f"ov_size_{upc}")
                new_uom = c1.text_input("UOM (optional)", value=current_uom, key=f"ov_uom_{upc}")
                submitted = st.form_submit_button("Stage Change", type="primary")

                if submitted:
                    new_values = {
                        "Description": new_description or None, "Department": new_department or None,
                        "Category": new_category or None, "Subcategory": new_subcategory or None,
                        "Brand": new_brand or None, "Pack": new_pack or None, "Size": new_size or None,
                        "UOM": new_uom or None, "SourceKey": new_source_key or None,
                    }
                    old_values = {col: sql_value(row[col]) for col in new_values}
                    any_changed = any(
                        not (pd.isna(new_values[col]) and pd.isna(old_values[col])) and new_values[col] != old_values[col]
                        for col in new_values
                    )
                    if not any_changed:
                        st.info("No changes made.")
                    else:
                        blocked = dept_mapping.save_item_master_pending(
                            ENGINE, upc, "edit", new_values["Description"], new_values["Department"],
                            new_values["Category"], new_values["Subcategory"], new_values["Brand"],
                            st.session_state["name"], source_key=new_values["SourceKey"],
                            pack=new_values["Pack"], size=new_values["Size"], uom=new_values["UOM"],
                        )
                        load_item_master_pending.clear()
                        if blocked:
                            render_blocked_item_master_edits({upc: blocked})
                        else:
                            st.success(f"Staged edit for {upc}. See Pending Changes to review and push.")
                            st.rerun()


# -------------------------------------------------------------------
# Sources
# -------------------------------------------------------------------
def render_sources_tab() -> None:
    """The Sources tab."""
    st.subheader("Configured distributor sources")
    st.caption(
        "Each distributor's file layout and cleaning rules. Edits are staged on Pending Changes.",
        help=("Replaces script.py's Data Source Definitions.xlsx — add a new distributor here (no code "
        "needed), then upload its file on the Upload & Ingest tab. Edits and new sources are "
        "staged, not applied immediately — see Pending Changes to review, undo, and push them."),
    )
    sources_df = load_sources()
    pending_source_changes = source_pending_cross_link()
    st.caption(f"{len(sources_df)} source(s) configured, {int((sources_df['enabled'] == True).sum())} enabled. Scroll right for cleaning-rule columns.")
    display_sources = sources_df.copy()
    display_sources["Apply Now"] = False
    # A source that already has a staged edit shows its PENDING values
    # here, not the live ones — otherwise a second, unrelated edit
    # (e.g. fixing Category Column after already staging a Pack
    # Column change) would diff against live dbo.sources, restage
    # the row with today's live value for every untouched field, and
    # silently wipe out the first staged field the moment the second
    # edit goes through, since staging always replaces the whole row.
    # Showing pending-so-far values lets edits layer correctly instead.
    rows_with_pending_edit = []
    for idx, row in display_sources.iterrows():
        pending = pending_source_changes.get(row["source_key"])
        if pending and pending["change_type"] == "edit":
            for col in dept_mapping.SOURCE_CONFIG_COLUMNS:
                display_sources.at[idx, col] = pending[col]
            display_sources.at[idx, "Apply Now"] = pending["apply_now"]
            rows_with_pending_edit.append(row["source_key"])
    if rows_with_pending_edit:
        st.caption(
            f"Showing already-staged values (not live) for: {', '.join(rows_with_pending_edit)} — "
            "editing further amends that same pending change rather than starting a new one."
        )
    edited_sources = st.data_editor(
        display_sources,
        key="sources_editor",
        width='stretch',
        hide_index=True,
        disabled=["source_key", "created_at", "updated_at"],
        num_rows="fixed",
        column_config={
            "size_format": st.column_config.SelectboxColumn(
                "Size Format", options=list(SIZE_FORMATS.keys()), required=True,
                help=" / ".join(f"{k}: {v}" for k, v in SIZE_FORMATS.items()),
            ),
            "Apply Now": st.column_config.CheckboxColumn(
                "Apply Now", default=False,
                help="Checked: pushing this staged change also re-runs ingestion against this "
                     "source's last uploaded file using the new configuration — no fresh upload "
                     "needed. Unchecked: just updates the configuration for the next upload.",
            ),
        },
    )
    if st.button("Stage Source Changes", type="primary"):
        staged_count = 0
        for _, row in edited_sources.iterrows():
            original_row = sources_df[sources_df["source_key"] == row["source_key"]].iloc[0]
            # Checking "Apply Now" alone (no other field edited) must still
            # stage something — it's the whole point when a code-level parsing
            # fix needs to be re-run against a source's existing config/file.
            if row_changed(row, original_row, dept_mapping.SOURCE_CONFIG_COLUMNS) or bool(row["Apply Now"]):
                config = {col: source_grid_value(row, col) for col in dept_mapping.SOURCE_CONFIG_COLUMNS}
                dept_mapping.save_source_pending_change(
                    ENGINE, row["source_key"], "edit", config, bool(row["Apply Now"]),
                    st.session_state["name"],
                )
                staged_count += 1
        load_source_pending_changes.clear()
        if staged_count == 0:
            st.info("No changes made.")
        else:
            st.success(f"Staged {staged_count} source change(s) — see Pending Changes to review and push.")
            st.rerun()

    st.divider()
    st.subheader("Add a new source")

    st.caption(
        'Upload a sample file; each sheet you pick becomes a pre-filled source form to check and add.',
        help=("Upload one or more sample files below — for each file, pick which sheet(s) should "
        "become sources (different sheets can be completely different layouts, e.g. a "
        "distributor's own catalog vs. its Food Service items). Analyzing creates one "
        "independent, pre-filled form per (file, sheet) below: column mappings, a guessed "
        "Source Key/Label/File Keyword from the filename, and Advanced cleaning rule candidates "
        "found by looking at real sample values (a common Brand placeholder, a Department "
        "placeholder, a UOM/type-looking column, a leading numeric code on Category/Subcategory). "
        "Review and correct everything in each form before adding it — especially UPC Strip "
        "Trailing Digits, which can't be guessed at all, and any Exclude Values, which are "
        "deliberately left for you to pick."),
    )

    # file_uploader has its own label row above the box; "Clear All" has
    # no matching label. The leftover vertical space lives on the plain
    # (unkeyed) stVerticalBlock Streamlit inserts as the column's direct
    # child, not on the column itself — see the matching comment above
    # the pagination-caption CSS for the full explanation.
    st.markdown(
        """
        <style>
        div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-clear_all_sources_btn) {
            justify-content: flex-end !important;
        }
        div.st-key-clear_all_sources_btn {
            margin-bottom: 14px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    uploader_version = st.session_state.get("detect_uploader_version", 0)
    acol1, acol2 = st.columns([4, 1])
    detect_files = acol1.file_uploader(
        "Sample file(s) to auto-detect from (optional)",
        type=["xlsx", "xls", "xlsb", "csv"], accept_multiple_files=True,
        key=f"detect_files_uploader_{uploader_version}",
    )
    if acol2.button("Clear All", type="secondary", key="clear_all_sources_btn"):
        st.session_state["detect_uploader_version"] = uploader_version + 1
        st.session_state["pending_sources"] = []
        st.rerun()

    sheet_selections = {}
    if detect_files:
        for f in detect_files:
            sheet_names, eligible_sheets = file_sheet_info(f)
            if sheet_names:
                default_sheets = [s for s in sheet_names if s in eligible_sheets] or sheet_names
                sheet_selections[f.name] = st.multiselect(
                    f"Sheet(s) to use from '{f.name}'", sheet_names, default=default_sheets,
                    key=f"sheets_for_{f.name}_{uploader_version}",
                )
                skipped = [s for s in sheet_names if s not in eligible_sheets]
                if skipped:
                    st.caption(
                        "Not pre-selected (couldn't find a plausible UPC + Description "
                        f"column): {', '.join(skipped)}. You can still check them manually."
                    )
            else:
                sheet_selections[f.name] = [None]  # CSV: the whole file is one "sheet"

        if st.button("Analyze Selected Sheets — Create A Form For Each", type="secondary"):
            base_priority = int(sources_df["priority_rank"].max()) + 1 if not sources_df.empty else 1
            next_id = st.session_state.get("pending_source_next_id", 0)
            # Disambiguates filenames that would otherwise guess the same
            # source_key (e.g. UNFI Natural's 3 separate per-warehouse
            # files all leading with "UNFI") using each file's own
            # distinguishing word (its warehouse city, etc.).
            batch_identities = guess_source_identities_batch([f.name for f in detect_files])
            new_entries = []
            analyze_errors = False
            for f in detect_files:
                for sheet in sheet_selections.get(f.name, [None]):
                    try:
                        entry = analyze_source(
                            f, sheet, next_priority_rank=base_priority + len(new_entries),
                            identity=batch_identities[f.name],
                        )
                        entry["id"] = next_id
                        new_entries.append(entry)
                        next_id += 1
                    except Exception as e:
                        st.error(f"Could not analyze '{f.name}'" + (f" sheet '{sheet}'" if sheet else "") + f": {e}")
                        analyze_errors = True

            # Safety net for any remaining collision the filename-level
            # disambiguation above couldn't catch (e.g. two DIFFERENT
            # sheets of the SAME file both guessing the same key) — the
            # sheet name is itself a natural, always-available disambiguator.
            seen_keys = {}
            for entry in new_entries:
                key = entry["source_key"]
                if key not in seen_keys:
                    seen_keys[key] = 1
                    continue
                suffix = re.sub(r"[^a-z0-9]+", "_", (entry["sheet_name"] or str(seen_keys[key])).lower()).strip("_")
                entry["source_key"] = f"{key}_{suffix}"
                entry["source_label"] = f"{entry['source_label']} ({entry['sheet_name'] or suffix})"
                entry["file_keyword"] = entry["source_label"]
                seen_keys[key] += 1

            st.session_state["pending_sources"] = st.session_state.get("pending_sources", []) + new_entries
            st.session_state["pending_source_next_id"] = next_id
            # Only rerun on a clean analyze — an immediate rerun right after
            # st.error() wipes the message off screen before it can be read.
            # Any successfully-analyzed sheets are already saved to session
            # state above and will show up as forms on the next interaction.
            if not analyze_errors:
                st.rerun()

    if st.button("+ Add a blank source manually (no file)", type="secondary"):
        next_id = st.session_state.get("pending_source_next_id", 0)
        base_priority = int(sources_df["priority_rank"].max()) + 1 if not sources_df.empty else 1
        blank_entry = {
            "id": next_id, "source_key": "", "source_label": "", "file_keyword": "",
            "priority_rank": base_priority, "sheet_name": "", "header_row": 1,
            "upc_column": "", "department_column": "", "category_column": "",
            "subcategory_column": "", "brand_column": "", "description_column": "",
            "pack_column": "", "size_column": "", "size_format": "plain", "uom_column": "",
            "exclude_column": "", "exclude_column_value_counts": None, "exclude_values": "",
            "blank_brand_when_equals": "", "blank_department_when_equals": "",
            "brand_suffix_match": "", "brand_suffix_result": "",
            "dedup_deprioritize_brand_value": "", "strip_leading_code_fields": "",
            "unmatched_fields": [], "source_filename": None,
        }
        st.session_state["pending_sources"] = st.session_state.get("pending_sources", []) + [blank_entry]
        st.session_state["pending_source_next_id"] = next_id + 1
        st.rerun()

    pending_sources = st.session_state.get("pending_sources", [])
    if pending_sources:
        st.info(f"{len(pending_sources)} source form(s) below — review each, then Stage New Source or Remove.")

    for idx, entry in enumerate(list(pending_sources)):
        eid = entry["id"]
        label = entry["source_filename"] or "(manual entry)"
        if entry.get("sheet_name"):
            label += f" — sheet '{entry['sheet_name']}'"
        summary = f"{label}  →  key: {entry['source_key'] or '(blank)'}, label: {entry['source_label'] or '(blank)'}"

        with st.expander(summary, expanded=(idx == 0)):
            if entry.get("unmatched_fields"):
                st.caption(f"Could not guess: {', '.join(entry['unmatched_fields'])} — fill in manually.")
            if entry.get("exclude_column"):
                st.caption(
                    f"Possible Exclude Column: **{entry['exclude_column']}** — sample value counts: "
                    f"{entry['exclude_column_value_counts']}. Pick which (if any) belong in Exclude Values."
                )

            with st.form(f"add_source_form_{eid}", clear_on_submit=False):
                c1, c2 = st.columns(2)
                source_key = c1.text_input("Source Key * (short, lowercase, no spaces)", value=entry["source_key"], key=f"src_{eid}_source_key")
                source_label = c2.text_input("Source Label *", value=entry["source_label"], key=f"src_{eid}_source_label")
                priority_rank = c1.number_input("Priority Rank * (lower = higher priority)", min_value=1, step=1, value=entry["priority_rank"], key=f"src_{eid}_priority_rank")
                file_keyword = c2.text_input("File Keyword (for your reference)", value=entry["file_keyword"], key=f"src_{eid}_file_keyword")
                sheet_name = c1.text_input("Sheet Name (blank = first sheet)", value=entry["sheet_name"], key=f"src_{eid}_sheet_name")
                header_row = c2.number_input("Header Row", min_value=1, step=1, value=entry["header_row"], key=f"src_{eid}_header_row")
                upc_column = c1.text_input("UPC Column *", value=entry["upc_column"], key=f"src_{eid}_upc_column")
                upc_suffix_column = c2.text_input("UPC Suffix Column (optional — e.g. a separate check-digit column to append)", key=f"src_{eid}_upc_suffix_column")
                strip_trailing_digits = c1.number_input("Strip Trailing Digits (e.g. 1 for a 13-digit EAN check digit)", min_value=0, step=1, value=0, key=f"src_{eid}_strip_trailing_digits")
                department_column = c1.text_input("Department Column", value=entry["department_column"], key=f"src_{eid}_department_column")
                category_column = c2.text_input("Category Column", value=entry["category_column"], key=f"src_{eid}_category_column")
                subcategory_column = c1.text_input("Subcategory Column", value=entry["subcategory_column"], key=f"src_{eid}_subcategory_column")
                brand_column = c2.text_input("Brand Column", value=entry["brand_column"], key=f"src_{eid}_brand_column")
                description_column = c1.text_input("Description Column", value=entry["description_column"], key=f"src_{eid}_description_column")

                st.markdown("**Pack & Size** (optional)")
                psc1, psc2 = st.columns(2)
                pack_column = psc1.text_input("Pack Column", value=entry.get("pack_column", ""), key=f"src_{eid}_pack_column")
                size_column = psc2.text_input("Size Column", value=entry.get("size_column", ""), key=f"src_{eid}_size_column")
                size_format_keys = list(SIZE_FORMATS.keys())
                size_format_labels = list(SIZE_FORMATS.values())
                default_format = entry.get("size_format") or "plain"
                size_format_label = psc1.selectbox(
                    "Size Column format",
                    size_format_labels,
                    index=size_format_keys.index(default_format) if default_format in size_format_keys else 0,
                    key=f"src_{eid}_size_format",
                )
                size_format = size_format_keys[size_format_labels.index(size_format_label)]
                uom_column = psc2.text_input(
                    "UOM Column (only with \"Plain value(s)\" — a separate column holding just the unit)",
                    value=entry.get("uom_column", ""), key=f"src_{eid}_uom_column",
                )
                uom_aliases = st.text_input(
                    "UOM Fixes (only with a combined Size Column format), comma-separated 'FROM=TO' pairs "
                    "(e.g. 'QRT=QT, GALLON=GAL') — 'Z'→'OZ' and '#'→'LB' are already fixed automatically",
                    value=entry.get("uom_aliases", ""), key=f"src_{eid}_uom_aliases",
                )

                st.markdown("**Advanced cleaning rules** (optional — matches script.py's per-source quirks)")
                ec1, ec2 = st.columns(2)
                exclude_column = ec1.text_input("Exclude Column (e.g. 'UOM')", value=entry["exclude_column"], key=f"src_{eid}_exclude_column")
                exclude_values = ec2.text_input("Exclude Values, comma-separated (e.g. 'DS,PL')", value=entry["exclude_values"], key=f"src_{eid}_exclude_values")
                blank_brand_when_equals = ec1.text_input("Blank Brand When Equals (e.g. '_')", value=entry["blank_brand_when_equals"], key=f"src_{eid}_blank_brand_when_equals")
                blank_department_when_equals = ec2.text_input("Blank Department/Category/Subcategory When Department Equals (e.g. 'OTHER')", value=entry["blank_department_when_equals"], key=f"src_{eid}_blank_department_when_equals")
                blank_department_default = ec1.text_input("Default For A Blank Department (e.g. 'GENERAL MERCHANDISE')", key=f"src_{eid}_blank_department_default")
                brand_suffix_match = ec2.text_input("Brand Ending With (e.g. 'PL')", value=entry["brand_suffix_match"], key=f"src_{eid}_brand_suffix_match")
                brand_suffix_result = ec1.text_input("...Becomes Exactly (e.g. 'PL')", value=entry["brand_suffix_result"], key=f"src_{eid}_brand_suffix_result")
                dedup_deprioritize_brand_value = ec2.text_input("Among Duplicate UPCs, Prefer Dropping Brand (e.g. 'PL')", value=entry["dedup_deprioritize_brand_value"], key=f"src_{eid}_dedup_deprioritize_brand_value")
                strip_leading_code_fields = ec1.text_input("Strip Leading Numeric Code From Fields, comma-separated (e.g. 'Category,Subcategory')", value=entry["strip_leading_code_fields"], key=f"src_{eid}_strip_leading_code_fields")

                notes = st.text_area("Notes", key=f"src_{eid}_notes")
                enabled = st.checkbox("Enabled", value=True, key=f"src_{eid}_enabled")
                bcol1, bcol2 = st.columns(2)
                submitted = bcol1.form_submit_button("Stage New Source", type="primary")
                removed = bcol2.form_submit_button("Remove This Form", type="secondary")

            if removed:
                st.session_state["pending_sources"] = [e for e in st.session_state.get("pending_sources", []) if e["id"] != eid]
                st.rerun()

            if submitted:
                source_key_clean = source_key.strip().lower()
                if not source_key or not source_label or not upc_column:
                    st.error("Source Key, Source Label, and UPC Column are required.")
                else:
                    with db_connect() as conn:
                        existing = conn.execute(
                            text("SELECT 1 FROM dbo.sources WHERE source_key = :k"),
                            {"k": source_key_clean},
                        ).first()
                    pending_now = dept_mapping.get_source_pending_changes(ENGINE)
                    if existing:
                        st.error(f"Source key '{source_key_clean}' already exists. Pick a different Source Key and try again.")
                    elif source_key_clean in pending_now:
                        st.error(f"Source key '{source_key_clean}' already has a pending change — undo it on Pending Changes first if you want to replace it.")
                    else:
                        config = {
                            "source_label": source_label, "enabled": enabled, "priority_rank": priority_rank,
                            "file_keyword": file_keyword or source_label, "sheet_name": sheet_name or None,
                            "header_row": header_row, "upc_column": upc_column,
                            "upc_suffix_column": upc_suffix_column or None,
                            "strip_trailing_digits": strip_trailing_digits,
                            "department_column": department_column or None,
                            "category_column": category_column or None,
                            "subcategory_column": subcategory_column or None,
                            "brand_column": brand_column or None,
                            "description_column": description_column or None,
                            "pack_column": pack_column or None,
                            "size_column": size_column or None,
                            "size_format": size_format,
                            "uom_column": uom_column or None,
                            "uom_aliases": uom_aliases or None,
                            "exclude_column": exclude_column or None,
                            "exclude_values": exclude_values or None,
                            "blank_brand_when_equals": blank_brand_when_equals or None,
                            "blank_department_when_equals": blank_department_when_equals or None,
                            "blank_department_default": blank_department_default or None,
                            "brand_suffix_match": brand_suffix_match or None,
                            "brand_suffix_result": brand_suffix_result or None,
                            "dedup_deprioritize_brand_value": dedup_deprioritize_brand_value or None,
                            "strip_leading_code_fields": strip_leading_code_fields or None,
                            "notes": notes or None,
                        }
                        dept_mapping.save_source_pending_change(
                            ENGINE, source_key_clean, "add", config, False, st.session_state["name"],
                        )
                        load_source_pending_changes.clear()
                        st.session_state["pending_sources"] = [e for e in st.session_state.get("pending_sources", []) if e["id"] != eid]
                        st.success(f"Staged adding source '{source_key_clean}' — see Pending Changes to review and push.")
                        st.rerun()


# -------------------------------------------------------------------
# Pending Changes
# -------------------------------------------------------------------
def render_pending_changes_tab() -> None:
    """The Pending Changes tab."""
    st.subheader("Pending Changes")
    if is_admin:
        st.caption(
            'Staged adds, deletes, item edits and source changes. Push makes them live.',
            help=("Every staged Add, Delete, and Edit from the Add Item, Delete Item, and UPC Overrides "
            "tabs, plus every staged Sources tab edit and new source, lands here in one place — "
            "durable and visible to every editor immediately. Push actually applies it to the database."),
        )
    else:
        st.caption(
            'Staged adds, deletes and item edits. Push makes them live.',
            help=("Every staged Add, Delete, and Edit from the Add Item, Delete Item, and UPC Overrides "
            "tabs lands here — durable and visible to every editor immediately. Push actually "
            "applies it to the database."),
        )
    st.markdown("#### Pending Item Master Changes")
    item_pending = render_item_master_pending_section()
    if not item_pending:
        st.info("Nothing staged right now.")

    if is_admin:
        st.markdown("#### Pending Source Changes")
        source_pending = render_source_pending_section()
        if not source_pending:
            st.info("Nothing staged right now.")


# -------------------------------------------------------------------
# Upload & Ingest
# -------------------------------------------------------------------
def render_upload_ingest_tab() -> None:
    """The Upload & Ingest tab."""
    st.subheader("Upload a distributor file")
    st.caption(
        "A new file replaces that source's rows. Every upload is logged below.",
        help=("Do this every month when a new file comes in for a source — the new file's rows "
        "REPLACE that source's previously staged rows. Every upload is logged permanently "
        "below, with an explanation for anything dropped, not just the first time."),
    )
    render_auto_merge_result(st.session_state.pop("_upload_auto_merge_result", None))
    render_monthly_refresh()
    sources_df = load_sources()
    if sources_df.empty:
        st.info("No sources configured yet — add one on the Sources tab first.")
    else:
        counts_df = load_raw_item_counts()
        total_staged = int(counts_df["row_count"].sum()) if not counts_df.empty else 0
        st.caption(f"{total_staged} total rows staged across {len(counts_df)} of {len(sources_df)} source(s).")
        with st.expander("View staged row counts per source"):
            st.dataframe(counts_df, width='stretch', hide_index=True)

        source_key_choice = st.selectbox("Source", sources_df["source_key"].tolist())
        source_series = sources_df[sources_df["source_key"] == source_key_choice].iloc[0]
        source_row = {col: sql_value(source_series[col]) for col in source_series.index}
        uploaded_file = st.file_uploader(
            f"Upload the file for '{source_row['source_label']}'",
            type=["xlsx", "xls", "xlsb", "csv"],
        )

        if uploaded_file is not None:
            try:
                raw_df = read_raw_file(uploaded_file, source_row)
                cleaned_df, stats, rejected_df = map_and_clean(raw_df, source_row)
                st.success(
                    f"Parsed {stats['rows_parsed']} raw rows → {stats['rows_staged']} valid, unique-UPC rows "
                    f"({stats['dropped_invalid_upc']} invalid UPC, {stats['dropped_duplicate_upc']} duplicate UPC dropped)."
                )
                st.dataframe(cleaned_df.head(20), width='stretch', hide_index=True)

                if st.button(f"Save {len(cleaned_df)} rows to raw_items for '{source_key_choice}'", type="primary"):
                    stage_source(
                        ENGINE, source_key_choice, cleaned_df, rejected_df, stats,
                        uploaded_by=st.session_state["name"],
                        original_filename=uploaded_file.name,
                        on_retry=_warn_retrying,
                    )
                    # Keeps the as-read file around so a later column-
                    # mapping fix on the Sources tab (e.g. adding a Pack/
                    # Size Column) can be re-run against it without
                    # asking for the same file again.
                    save_raw_upload(ENGINE, source_key_choice, raw_df, uploaded_file.name, st.session_state["name"])
                    load_raw_item_counts.clear()
                    load_ingestion_log.clear()
                    load_stale_sources.clear()
                    load_stale_sources_since_compute.clear()
                    st.success(f"Staged {len(cleaned_df)} rows for '{source_key_choice}'.")
                    # A fresh upload replaces raw_items right away, same
                    # as Apply Now — recompute a fresh merge draft so
                    # it's ready on the Merge tab instead of needing a
                    # separate trip there just to notice something
                    # changed. Stashed rather than shown directly: this
                    # run ends in st.rerun(), which would wipe anything
                    # rendered here before the frontend ever caught up.
                    with st.spinner(f"Recomputing the merge draft with {source_key_choice}'s new data..."):
                        st.session_state["_upload_auto_merge_result"] = auto_recompute_and_push_merge()
                    st.rerun()
            except Exception as e:
                st.error(f"Could not parse file: {e}")

        st.divider()
        st.subheader(f"Ingestion history for '{source_row['source_label']}'")
        log_df = load_ingestion_log(source_key_choice)
        if log_df.empty:
            st.caption("No uploads recorded yet for this source.")
        else:
            latest = log_df.iloc[0]
            st.caption(
                f"Last upload: {latest['uploaded_at']} by {latest['uploaded_by'] or 'unknown'} — "
                f"{latest['rows_staged']} staged, {latest['dropped_invalid_upc']} invalid UPC, "
                f"{latest['dropped_duplicate_upc']} duplicate UPC dropped."
            )
            with st.expander(f"View full history ({len(log_df)} upload(s)) and dropped rows"):
                st.dataframe(log_df, width='stretch', hide_index=True)

                log_choice_id = st.selectbox(
                    "View rejected rows for upload:",
                    log_df["id"].tolist(),
                    format_func=lambda i: (
                        f"{log_df.loc[log_df['id'] == i, 'uploaded_at'].iloc[0]} — "
                        f"{log_df.loc[log_df['id'] == i, 'original_filename'].iloc[0] or '(unknown file)'}"
                    ),
                )
                rejected_for_log = load_rejected_rows(log_choice_id)
                if rejected_for_log.empty:
                    st.caption("Nothing was dropped on this upload.")
                else:
                    for reason in rejected_for_log["reason"].unique():
                        st.markdown(f"**{reason}** — {REASON_EXPLANATIONS.get(reason, 'No explanation available.')}")
                        st.dataframe(
                            rejected_for_log[rejected_for_log["reason"] == reason],
                            width='stretch',
                            hide_index=True,
                        )


# -------------------------------------------------------------------
# Merge
# -------------------------------------------------------------------
def render_merge_tab() -> None:
    """The Merge tab."""
    st.subheader("Merge staged sources into the item master")
    with st.expander("How this works"):
        st.caption(
            "A Merge only ADDS items. Every source file is read, but for an item already in the item "
            "master the files are only used to confirm it isn't new — its description, brand, category, "
            "pack/size and Department Review group are never changed by a file, and it's kept even if no "
            "file lists it any more. A UPC no one has yet is built from the highest-priority source "
            "that has it (the first with every field filled in), cleaned by that source's rules, with "
            "manual overrides on top; manually deleted UPCs are never brought back. After a push the "
            "Department engine runs so every decision applies to the new items too: new items in a "
            "decided group get its Department, new groups wait in Crosswalk/Unmatched (or are "
            "auto-decided when the evidence is strong). Compute shows what would be added before anything "
            "changes."
        )
    with st.container(border=True):
        st.markdown("**Check the item master against every rule and decision**")
        st.caption(
            'Re-checks every item against every rule and decision (about 10 s) and stages any fix.',
            help=("Decisions, Settings, adds, deletes and UPC Overrides already update the item master the moment "
            "they're pushed — only the rows they affect — so this should find nothing. It re-checks every item "
            "(about 10 seconds) and stages a fix: every item that would change, what would change, and the rule "
            "or decision behind it. Nothing is written until you apply it, and an applied fix can be undone. "
            "Nothing is ever taken from source files."),
        )
        undo = st.session_state.get("_rules_undo")
        if undo:
            u1, u2 = st.columns([3, 1])
            u1.caption(f"Last fix: {len(undo['upcs']):,} item(s) changed by {undo['by']}.")
            if u2.button("Undo that fix", key="rules_undo_btn", width='stretch'):
                dept_mapping.undo_rules_plan(ENGINE, undo)
                st.session_state.pop("_rules_undo", None)
                load_items.clear()
                st.session_state["_toast"] = (f"Put {len(undo['upcs']):,} item(s) back exactly as they were.", "")
                st.rerun()
        if st.button("Check now", key="rules_check_btn"):
            with st.spinner("Checking every item..."):
                st.session_state["_rules_plan"] = dept_mapping.plan_rules(ENGINE)
        plan = st.session_state.get("_rules_plan")
        if plan is not None:
            if plan.empty:
                st.success("Everything matches — no item is out of line with the rules and decisions.")
            else:
                st.warning(f"{plan['UPC'].nunique():,} item(s) would change ({len(plan):,} field change(s)). "
                           "Review them below before applying.")
                st.dataframe(plan, hide_index=True, width='stretch', height=min(520, 38 + 35 * len(plan)))
                st.download_button("Download the list (CSV)", plan.to_csv(index=False).encode(),
                                   file_name="item_master_recheck.csv", mime="text/csv", key="rules_dl")
                a1, a2 = st.columns(2)
                if a1.button(f"Apply these {len(plan):,} change(s)", type="primary", key="rules_fix_btn", width='stretch'):
                    with st.spinner("Applying..."):
                        res = dept_mapping.apply_rules_plan(ENGINE, plan, st.session_state["name"])
                    st.session_state.pop("_rules_plan", None)
                    if res["ok"]:
                        st.session_state["_rules_undo"] = res["undo"]
                        load_items.clear()
                        st.session_state["_toast"] = (f"Applied {len(plan):,} change(s) — Undo that fix puts them back.", "")
                    else:
                        st.session_state["_toast"] = ("Something changed since the check — nothing was applied. Check again.", "")
                    st.rerun()
                if a2.button("Discard", key="rules_discard_btn", width='stretch'):
                    st.session_state.pop("_rules_plan", None)
                    st.rerun()

    # ---- items added by past Merges -------------------------------------
    with st.expander("Items added by past Merges — row data, and the decision each one got (or where it's waiting)"):
        merges = dept_mapping.list_merges_with_additions(ENGINE)
        recorded = merges[merges["recorded"] > 0]
        if recorded.empty:
            st.caption("No Merge has added items since this report started recording them.")
        else:
            mid = st.selectbox(
                "Merge", recorded["id"].tolist(), key="added_merge_pick",
                format_func=lambda i: (lambda r: f"{pd.to_datetime(r['merged_at']):%Y-%m-%d %H:%M} UTC by {r['merged_by']} — "
                                                 f"{int(r['recorded']):,} item(s) added")(recorded.set_index('id').loc[i]),
            )
            added = dept_mapping.merge_added_items(ENGINE, int(mid))
            render_item_decisions(added, f"added_{mid}", f"merge_{mid}_added_items")

    stale = load_stale_sources()
    if stale:
        label = "source" if len(stale) == 1 else "sources"
        st.warning(
            f"{len(stale)} {label} ({', '.join(stale)}) have raw data newer than the "
            "last Merge — Item Master and Department Review still show the old data until you "
            "compute and Push a new merge below."
        )
    else:
        st.caption("Item Master is up to date with every source's current raw data.")

    last_merge = load_last_merge_summary()
    if last_merge:
        with st.expander(
            f"Last Merge — {last_merge['upc_count']:,} item(s), "
            f"{last_merge['merged_at']:%Y-%m-%d %H:%M} by {last_merge['merged_by'] or 'unknown'}",
        ):
            if last_merge.get("added_count") is None:
                st.caption("No impact summary recorded for this merge (it ran before this feature existed).")
            else:
                lcol1, lcol2, lcol3 = st.columns(3)
                lcol1.metric("Added", f"{last_merge['added_count']:,}")
                lcol2.metric("Differed in files (ignored)", f"{last_merge['changed_count']:,}")
                lcol3.metric("Not in files (kept)", f"{last_merge['removed_count']:,}")
                st.caption(
                    f"{last_merge['overrides_applied'] or 0} manual override(s) applied, "
                    f"{last_merge['deleted_excluded'] or 0} manually-deleted UPC(s) excluded"
                )
                render_merge_change_breakdown(last_merge["changed_by_field"], last_merge["changed_by_source"])

    sources_df = load_sources()
    enabled_sources = sources_df[sources_df["enabled"] == True].sort_values("priority_rank")
    with st.expander("Source priority order (highest priority first)"):
        st.dataframe(enabled_sources[["priority_rank", "source_key", "source_label"]], width='stretch', hide_index=True)

    # Colored/primary only when there's actually fresher raw data to pull
    # in — otherwise the loud red reads as "something's waiting on you"
    # even when a click would just recompute the exact same result.
    if st.button("Compute Merge", type="primary" if stale else "secondary"):
        mark_own_progress()
        compute_progress = st.progress(0, text="Reading raw data, manual overrides, and deletions...")
        priority_order = enabled_sources["source_key"].tolist()
        final_df, overrides_applied, deleted_count = dept_mapping.compute_merge_final_df(ENGINE, priority_order)
        if final_df is None:
            compute_progress.empty()
            st.warning("No staged rows for any enabled source, and no manually-added items. "
                       "Upload files on the Upload & Ingest tab first.")
        else:
            compute_progress.progress(0.85, text="Comparing against the live item master and saving...")
            dept_mapping.save_merge_compute(
                ENGINE, final_df, st.session_state["name"], overrides_applied, deleted_count,
            )
            compute_progress.empty()
            clear_merge_compute_caches()
            st.success(
                f"Computed {len(final_df)} item(s) "
                f"({overrides_applied} manual override(s) applied, {deleted_count} manually-deleted UPC(s) excluded) "
                "— review below, then Push to apply it."
            )
            st.rerun()

    st.divider()
    if st.session_state.pop("_reset_confirm_push_merge", False):
        st.session_state["confirm_push_merge"] = False
    compute_meta = load_merge_compute_meta()
    if not compute_meta:
        st.caption("Nothing computed yet — click Compute Merge above.")
    else:
        st.success(
            f"\U0001f7e2 **Computed merge ready to push** — {compute_meta.get('added_count') or 0:,} new item(s) "
            "would be added. Existing items are never changed by a Merge. Review below, then push when ready."
        )
        with st.container(border=True):
            st.markdown(f"**{compute_meta['item_count']:,} item(s) total** after pushing")
            mcol1, mcol2, mcol3 = st.columns(3)
            mcol1.metric("New items to add", f"{compute_meta.get('added_count') or 0:,}", help="UPCs no one has yet — cleaned by every rule and decision, then added.")
            mcol2.metric("Existing, differ in files", f"{compute_meta.get('changed_count') or 0:,}", help="Existing items whose source data now reads differently. Ignored — a Merge never changes an existing item.")
            mcol3.metric("No longer in any file", f"{compute_meta.get('removed_count') or 0:,}", help="Existing items no file lists any more. Kept as they are.")
            st.caption(
                f"{compute_meta['overrides_applied']} manual override(s) applied, "
                f"{compute_meta['deleted_excluded']} manually-deleted UPC(s) excluded"
            )
            render_merge_change_breakdown(compute_meta.get("changed_by_field") or {}, compute_meta.get("changed_by_source") or {})
            if compute_meta.get("added_count"):
                with st.expander(f"The {compute_meta['added_count']:,} new item(s) — row data and the decision each will get"):
                    with st.spinner("Working out each item's decision..."):
                        preview = dept_mapping.draft_new_items(ENGINE)
                    render_item_decisions(preview, "draft_new", "merge_draft_new_items")
            st.caption(f"Computed {compute_meta['computed_at']} by {compute_meta['computed_by'] or 'unknown'}")
            if st.button("Discard this computed merge"):
                dept_mapping.discard_merge_compute(ENGINE)
                clear_merge_compute_caches()
                st.rerun()
        stale_since_compute = load_stale_sources_since_compute()
        if stale_since_compute:
            label = "source" if len(stale_since_compute) == 1 else "sources"
            st.error(
                f"{len(stale_since_compute)} {label} ({', '.join(stale_since_compute)}) have raw "
                "data newer than this computed merge — it's now out of date. Pushing will be "
                "blocked and this draft discarded; Compute Merge again first."
            )
        st.warning(
            "Pushing adds the new items, then re-runs Department Review's group engine so every decision "
            "applies to them too (new items in decided groups get their Department; new groups wait in "
            "Crosswalk/Unmatched). A safety snapshot is taken first, so this can be undone from Snapshots."
        )

        # Approval gate — a Merge push replaces the ENTIRE live item
        # master and now happens far more often (any Sources/Upload/
        # Dept Review push recomputes a fresh draft), so it needs
        # MERGE_PUSH_REQUIRED_APPROVALS distinct people to sign off
        # before it actually goes live. Admins bypass the count (their
        # own click is enough) but still get warned first if pushing
        # right now would risk someone else's in-progress work — that
        # warning only means anything shown BEFORE the push, which is
        # exactly why auto-recompute never auto-pushes anymore.
        approvals = compute_meta.get("approvals") or []
        distinct_approvers = sorted({a["approver"] for a in approvals})
        required = dept_mapping.MERGE_PUSH_REQUIRED_APPROVALS
        if distinct_approvers:
            st.caption(f"Approved by: {', '.join(distinct_approvers)} ({len(distinct_approvers)} of {required} needed)")
        else:
            st.caption(f"No approvals yet — {required} needed before this can be pushed (admins exempt).")
        if st.session_state["name"] not in distinct_approvers:
            if st.button("Approve this merge", key="approve_merge_compute"):
                dept_mapping.approve_merge_compute(ENGINE, st.session_state["name"])
                clear_merge_compute_caches()
                st.rerun()
        else:
            st.caption("You've already approved this draft.")

        has_pending_elsewhere = dept_mapping.has_pending_review_work(ENGINE)
        override_ack = True
        if is_admin and has_pending_elsewhere:
            st.error(
                "Someone has a staged Item Master change or Department Review decision "
                "in progress right now — pushing this merge could discard it (they'll see a notice "
                "either way, but it's better to know first)."
            )
            override_ack = st.checkbox(
                "I understand this may affect other users' in-progress work, and I want to push anyway.",
                key="confirm_override_pending_work",
            )

        confirm_push = st.checkbox(
            "I've reviewed this computed merge and I'm ready to update the database.",
            key="confirm_push_merge",
        )
        enough_approvals = is_admin or len(distinct_approvers) >= required
        if st.button("Push Items to Database", type="primary", disabled=not (confirm_push and enough_approvals and override_ack)):
            mark_own_progress()
            push_progress = st.progress(0, text="Starting push...")
            result = dept_mapping.push_merge_compute(
                ENGINE, st.session_state["name"], is_admin=is_admin,
                on_progress=lambda label, frac: push_progress.progress(frac, text=label),
            )
            push_progress.empty()
            if not (result.get("aborted_stale") or result.get("aborted_insufficient_approvals")):
                activity("Uploads & Merge", "Pushed a Merge", None, result.get("added_count") or result.get("item_count"),
                         details={k: v for k, v in result.items() if isinstance(v, (int, float, str, bool, type(None)))})
            clear_merge_compute_caches()
            load_last_merge_summary.clear()
            st.session_state["_reset_confirm_push_merge"] = True
            if result.get("aborted_insufficient_approvals"):
                # Shouldn't be reachable — the button above is disabled
                # until this is satisfied — but defensive in case
                # another approval was pulled back mid-review.
                st.error(
                    f"Push blocked — only {len(distinct_approvers)} of {result['required']} required "
                    "approval(s) are on this draft. Nothing was changed."
                )
                st.rerun()
                st.stop()
            if result["aborted_stale"]:
                label = "source" if len(result["stale_sources"]) == 1 else "sources"
                st.error(
                    f"Push blocked and this draft discarded — {len(result['stale_sources'])} "
                    f"{label} ({', '.join(result['stale_sources'])}) had raw data refreshed after "
                    "this merge was computed. Nothing was changed in the database. Click Compute "
                    "Merge again to build a fresh draft against the current data."
                )
                st.rerun()
                st.stop()
            load_items.clear()
            load_manual_items.clear()
            load_stale_sources.clear()
            load_dept_review_queue.clear()
            load_broken_out_combos.clear()
            load_decided_combos.clear()
            load_combo_member_items.clear()
            load_pending_upc_overrides.clear()
            load_dept_pending_changes.clear()
            load_dept_pending_upc_changes.clear()
            load_discard_notices.clear()
            clear_snapshot_caches()
            st.success(
                f"Pushed {result['item_count']:,} item(s) to the database "
                f"(safety snapshot #{result['safety_snapshot_id']} taken first"
                + (f"; this month's snapshot is now #{result['monthly_snapshot_id']}" if result.get("monthly_snapshot_id") is not None else "")
                + ")."
            )
            if result["engine_error"]:
                st.warning(
                    f"Item master updated, but recomputing Department Review's groups failed: "
                    f"{result['engine_error']} — Item Master reflects the new merge; Crosswalk/"
                    "Unmatched/Broken Out may be out of date until this is retried."
                )
            elif result["engine_summary"]:
                s = result["engine_summary"]
                st.info(
                    f"Department Review groups recomputed: {s['new_combos']} new, "
                    f"{s['auto_decided']} auto-decided, {s['needs_review']} need review, "
                    f"{s['unmatched']} unmatched, {s['demoted']} demoted back to review."
                )
            if result["discarded_pending_combo_ids"]:
                n = len(result["discarded_pending_combo_ids"])
                label = "decision" if n == 1 else "decisions"
                st.warning(
                    f"{n} pending Department Review {label} (combo ID(s): "
                    f"{', '.join(str(c) for c in result['discarded_pending_combo_ids'])}) "
                    "were discarded because that combo's evidence changed during this Merge — "
                    "please re-review them fresh in Crosswalk/Unmatched/Broken Out."
                )
            if result["discarded_item_master_upcs"]:
                n = len(result["discarded_item_master_upcs"])
                label = "change" if n == 1 else "changes"
                st.warning(
                    f"{n} pending Item Master {label} (UPC(s): "
                    f"{', '.join(result['discarded_item_master_upcs'])}) were discarded because "
                    "this Merge changed that item's data — please re-review and re-stage them "
                    "against the current item."
                )
                load_item_master_pending.clear()
            st.rerun()

    st.divider()
    with st.expander("Spot-check against a reference UPC list"):
        st.caption(
            "Compare this merge's UPCs with a known-good list — a very low match means a UPC cleaning rule is off.",
            help=("Upload a file with a known-good UPC list (e.g. a prior month's export) to sanity-check "
            "that this merge's UPC cleaning still lines up with it. A very low match percentage usually "
            "means a source's UPC cleaning rule (check digit, leading zeros) is now wrong — a match near "
            "0% almost always means a cleaning mistake, not that the data genuinely changed that much."),
        )
        reference_file = st.file_uploader(
            "Reference file (must have a column literally named 'UPC')", type=["xlsx", "xls", "csv"], key="reference_upload"
        )
        if reference_file is not None:
            try:
                with st.spinner(f"Reading '{reference_file.name}' and comparing against the item master — this can take a moment for a large file..."):
                    if reference_file.name.lower().endswith(".csv"):
                        ref_df = pd.read_csv(reference_file, dtype=str)
                    else:
                        ref_df = pd.read_excel(reference_file, dtype=str)
                    if "UPC" not in ref_df.columns:
                        columns_found = list(ref_df.columns)
                        overlap = None
                    else:
                        ref_upcs = set(
                            ref_df["UPC"].dropna().astype(str).str.strip().apply(lambda u: u.lstrip("0") or "0")
                        )
                        with db_connect() as conn:
                            current_upcs = set(pd.read_sql(text("SELECT upc FROM dbo.items"), conn)["upc"])
                        overlap = ref_upcs & current_upcs
                        pct = 100 * len(overlap) / len(ref_upcs) if ref_upcs else 0

                if overlap is None:
                    st.error(f"No 'UPC' column found. Columns present: {columns_found}")
                elif pct < 50:
                    st.error(
                        f"Only {len(overlap)} of {len(ref_upcs)} reference UPCs matched ({pct:.1f}%). "
                        "This low a match usually means a UPC cleaning rule is wrong somewhere — "
                        "check Strip Trailing Digits / UPC Suffix Column on the Sources tab."
                    )
                else:
                    st.success(f"{len(overlap)} of {len(ref_upcs)} reference UPCs matched ({pct:.1f}%).")
            except Exception as e:
                st.error(f"Could not read reference file: {e}")


# -------------------------------------------------------------------
# Snapshots
# -------------------------------------------------------------------
def render_snapshots_tab() -> None:
    """The Snapshots tab."""
    st.subheader("Snapshots")
    st.caption(
        "Full copies of the item master, decisions, staged work and settings. Restoring saves today's data first.",
        help=("A snapshot is a full, point-in-time copy of the item master, every Department Review decision and "
        "staged change, Settings, and each source's settings (not the raw distributor files). Restoring one "
        "always saves today's data first, so a restore can be undone."),
    )
    st.caption(
        f"Kept: **manual** snapshots until someone deletes them · **monthly** ones for the last "
        f"{dept_mapping.KEEP_MONTHLY_SNAPSHOTS} months (refreshed after each Merge push) · **automatic** safety "
        f"copies, the newest {dept_mapping.KEEP_SAFETY_SNAPSHOTS}."
    )

    def _after_restore(msg):
        clear_snapshot_caches()
        st.cache_data.clear()
        st.session_state["_toast"] = (msg, "")
        st.rerun()

    # ---- Undo the latest restore ----------------------------------------
    last = dept_mapping.get_last_restore(ENGINE)
    if last and last["restored_from"] is not None:
        when = pd.to_datetime(last["taken_at"]).strftime("%m/%d %H:%M")
        was_undo = last["restored_kind"] == "safety_restore"
        with st.container(border=True):
            st.markdown(
                f"**Last restore:** {last['taken_by']} restored snapshot #{last['restored_from']}"
                + (f" — *{last['restored_label']}*" if last["restored_label"] and not was_undo else
                   " (that was undoing an earlier restore)" if was_undo else "")
                + f" on {when} UTC. The data from just before it is saved as snapshot #{last['safety_id']}."
            )
            if last["merges_since"]:
                st.warning(f"{last['merges_since']} Merge push(es) have happened since — undoing the restore "
                           "also takes those back.")
            if st.session_state.get("_confirm_undo_restore"):
                u1, u2 = st.columns(2)
                if u1.button(f"Confirm — put back the data from before that restore (#{last['safety_id']})",
                             type="primary", width='stretch', key="undo_restore_confirm"):
                    st.session_state.pop("_confirm_undo_restore", None)
                    with st.spinner("Undoing the restore..."):
                        dept_mapping.restore_snapshot(ENGINE, int(last["safety_id"]), st.session_state["name"])
                    _after_restore(f"Restore undone — back to the data from before {when} UTC. "
                                   "(Changed your mind? Undo this one the same way.)")
                if u2.button("Cancel", width='stretch', key="undo_restore_cancel"):
                    st.session_state.pop("_confirm_undo_restore", None)
                    st.rerun()
            elif st.button("Undo this restore", key="undo_restore_btn"):
                st.session_state["_confirm_undo_restore"] = True
                st.rerun()

    with st.form("take_snapshot_form"):
        label = st.text_input(
            "Name it so others know why it exists", placeholder="e.g. Before September price changes — all Broken Out done",
        )
        if st.form_submit_button("Take Snapshot Now", type="primary"):
            with st.spinner("Taking snapshot..."):
                snapshot_id = dept_mapping.take_snapshot(ENGINE, st.session_state["name"], label or None)
            clear_snapshot_caches()
            st.session_state["_toast"] = (f"Snapshot #{snapshot_id} taken.", "")
            st.rerun()

    st.divider()
    snapshots_df = load_snapshots()
    if snapshots_df.empty:
        st.caption("No snapshots yet.")
    else:
        KIND_BADGE = {"manual": "Manual", "monthly": "Monthly", "safety_merge": "Before a Merge push",
                      "safety_restore": "Before a restore"}
        fc1, fc2 = st.columns([2, 3])
        show = fc1.segmented_control(
            "Show", ["All", "Manual", "Monthly", "Automatic"], default="All", key="snap_filter",
            label_visibility="collapsed",
        ) or "All"
        q = fc2.text_input("Search snapshots", key="snap_search", label_visibility="collapsed",
                           placeholder="Search name, person, or #id").strip().lower()
        want = {"Manual": {"manual"}, "Monthly": {"monthly"}, "Automatic": {"safety_merge", "safety_restore"}}.get(show)
        rows = snapshots_df
        if want:
            rows = rows[rows["kind"].isin(want)]
        if q:
            rows = rows[rows.apply(lambda r: q in f"#{r['snapshot_id']} {r['label'] or ''} {r['taken_by'] or ''}".lower(), axis=1)]
        st.caption(f"{len(rows)} of {len(snapshots_df)} snapshot(s), newest first.")

        def _summary(d):
            g = d.get("groups", {})
            waiting = g.get("crosswalk", 0) + g.get("unmatched", 0)
            staged = d.get("staged", {})
            n_staged = sum(v or 0 for k, v in staged.items() if k != "by")
            bits = [f"{d['items']['total']:,} items",
                    f"{g.get('auto', 0) + g.get('decided_by_person', 0) + g.get('decided_by_item', 0):,} groups decided",
                    f"{g.get('broken_out', 0):,} in Broken Out", f"{waiting:,} waiting in Crosswalk/Unmatched",
                    f"{n_staged:,} staged change(s)" + (f" ({', '.join(staged['by'])})" if staged.get("by") else "")]
            return " · ".join(bits)

        for _, row in rows.iterrows():
            snapshot_id = int(row["snapshot_id"])
            kind = row["kind"] or "manual"
            details = json.loads(row["details_json"]) if isinstance(row["details_json"], str) and row["details_json"] else None
            with st.container(border=True):
                c1, c2, c3 = st.columns([5, 1, 1])
                title = row["label"] if pd.notna(row["label"]) and row["label"] else f"Snapshot of {row['snapshot_month']}"
                c1.markdown(f"**#{snapshot_id} · {title}**")
                when = pd.to_datetime(row["taken_at"]).strftime("%Y-%m-%d %H:%M")
                c1.caption(f"{KIND_BADGE.get(kind, kind)} · taken {when} UTC by {row['taken_by'] or 'unknown'}"
                           + (f" · restoring #{int(row['restored_from'])}" if pd.notna(row["restored_from"]) else ""))
                c1.caption(_summary(details) if details else f"{row['item_count']:,} items · {row['combo_count']:,} groups")
                restore_key, delete_key = f"confirm_restore_{snapshot_id}", f"confirm_delete_{snapshot_id}"
                if c2.button("Restore", key=f"restore_btn_{snapshot_id}", width='stretch'):
                    st.session_state[restore_key] = True
                if c3.button("Delete", key=f"delete_btn_{snapshot_id}", width='stretch'):
                    st.session_state[delete_key] = True

                with st.expander("Details", key=f"snap_details_{snapshot_id}"):
                    if details is None:
                        if st.button("Show details", key=f"snap_load_details_{snapshot_id}"):
                            with st.spinner("Reading the snapshot..."):
                                dept_mapping.get_snapshot_details(ENGINE, snapshot_id)
                            clear_snapshot_caches()
                            st.rerun()
                        st.caption("Worked out from the snapshot itself the first time you open it.")
                    else:
                        d1, d2 = st.columns(2)
                        with d1:
                            st.markdown("**Item master**")
                            src = details["items"]["by_source"]
                            st.caption(" · ".join(f"{k}: {v:,}" for k, v in sorted(src.items(), key=lambda kv: -kv[1])))
                            st.caption(f"{details['items']['no_department']:,} item(s) with no Department · "
                                       f"{details['manual'].get('overrides') or 0:,} manual edit(s) · "
                                       f"{details['manual'].get('deleted') or 0:,} deleted item(s)")
                            g = details["groups"]
                            st.markdown("**Department Review groups**")
                            st.caption(
                                f"Auto-decided {g.get('auto', 0):,} · decided by a person {g.get('decided_by_person', 0):,} · "
                                f"decided item by item {g.get('decided_by_item', 0):,} · in Broken Out {g.get('broken_out', 0):,} · "
                                f"Crosswalk {g.get('crosswalk', 0):,} · Unmatched {g.get('unmatched', 0):,}"
                            )
                        with d2:
                            s_ = details["staged"]
                            st.markdown("**Staged, not yet pushed**")
                            st.caption(
                                f"{s_.get('group_decisions') or 0} group decision(s) · {s_.get('item_decisions') or 0} item decision(s) · "
                                f"{s_.get('votes') or 0} vote(s) · {s_.get('item_master_changes') or 0} item add/delete/edit(s) · "
                                f"{s_.get('source_changes') or 0} source change(s)"
                                + (f" — by {', '.join(s_['by'])}" if s_.get("by") else "")
                            )
                            se = details["settings"]
                            st.markdown("**Settings**")
                            st.caption(
                                "Not saved in this older snapshot." if se.get("departments") is None else
                                f"{se['departments']} Departments · {se['strict']} Strict · {se['unmatched_defaults']} Unmatched Defaults"
                            )
                        if details.get("sources"):
                            files = (details.get("live") or {}).get("files", {})
                            st.markdown("**Sources**")
                            st.dataframe(pd.DataFrame([
                                {"Priority": s["priority"], "Source": s["key"], "Name": s["label"],
                                 "Enabled": s["enabled"], "Items won": src.get(s["key"], 0),
                                 "Latest file": (files.get(s["key"]) or {}).get("file"),
                                 "Uploaded": (files.get(s["key"]) or {}).get("uploaded_at")}
                                for s in details["sources"]
                            ]), hide_index=True, width='stretch')
                        lm = (details.get("live") or {}).get("last_merge")
                        if lm:
                            st.caption(f"Last Merge before this snapshot: {lm['at']} UTC by {lm['by']}.")
                    ck = f"_snap_compare_{snapshot_id}"
                    if st.button("Compare with what's live now", key=f"snap_compare_btn_{snapshot_id}"):
                        with st.spinner("Comparing..."):
                            st.session_state[ck] = dept_mapping.compare_snapshot_to_live(ENGINE, snapshot_id)
                    cmp_ = st.session_state.get(ck)
                    if cmp_:
                        if not (cmp_["added"] or cmp_["removed"] or cmp_["changed"] or cmp_["groups_different"]):
                            st.success("Live data is the same as this snapshot — restoring it would change nothing.")
                        else:
                            st.info(
                                f"Restoring this would: remove {cmp_['added']:,} item(s) added since · bring back "
                                f"{cmp_['removed']:,} removed item(s) · change {cmp_['changed']:,} item(s)"
                                + (" (" + ", ".join(f"{k} {v:,}" for k, v in cmp_["changed_by_field"].items()) + ")"
                                   if cmp_["changed_by_field"] else "")
                                + f" · put {cmp_['groups_different']:,} Department Review group(s) back as they were."
                            )

                if st.session_state.get(restore_key):
                    st.warning(
                        f"This REPLACES the live item master, every Department Review decision and staged change, "
                        f"Settings and source settings with snapshot #{snapshot_id}. Today's data is saved first, "
                        "and **Undo this restore** at the top of this tab puts it back."
                    )
                    rc1, rc2 = st.columns(2)
                    if rc1.button(f"Confirm restore #{snapshot_id}", key=f"confirm_restore_btn_{snapshot_id}",
                                  type="primary", width='stretch'):
                        st.session_state.pop(restore_key, None)
                        with st.spinner(f"Restoring snapshot #{snapshot_id}..."):
                            safety_id = dept_mapping.restore_snapshot(ENGINE, snapshot_id, st.session_state["name"])
                        _after_restore(f"Restored snapshot #{snapshot_id}. Today's prior data is saved as #{safety_id} — "
                                       "use Undo this restore to go back.")
                    if rc2.button("Cancel", key=f"cancel_restore_btn_{snapshot_id}", width='stretch'):
                        st.session_state.pop(restore_key, None)
                        st.rerun()
                if st.session_state.get(delete_key):
                    st.warning(f"Delete snapshot #{snapshot_id} for good? This can't be undone.")
                    dc1, dc2 = st.columns(2)
                    if dc1.button(f"Delete #{snapshot_id}", key=f"confirm_delete_btn_{snapshot_id}", width='stretch'):
                        st.session_state.pop(delete_key, None)
                        dept_mapping.delete_snapshot(ENGINE, snapshot_id)
                        activity("Snapshots", f"Deleted snapshot #{snapshot_id}", title)
                        clear_snapshot_caches()
                        st.rerun()
                    if dc2.button("Cancel", key=f"cancel_delete_btn_{snapshot_id}", width='stretch'):
                        st.session_state.pop(delete_key, None)
                        st.rerun()


# -------------------------------------------------------------------
# Activity (admins): who changed what
# -------------------------------------------------------------------
@st.cache_data(ttl=30, show_spinner=False)
def load_activity(since) -> pd.DataFrame:
    df = dept_mapping.list_activity(ENGINE, since=since)
    if not df.empty:
        df["when"] = pd.to_datetime(df["at"]).dt.tz_localize("UTC").dt.tz_convert("America/Los_Angeles")
    return df


@st.cache_data(ttl=30, show_spinner=False)
def load_staged_by_person() -> pd.DataFrame:
    return dept_mapping.staged_work_by_person(ENGINE)


ACT_ICONS = {}
ACT_LOG_PAGE = 200


def _act_kind(action: str) -> str:
    """An action without its numbers / who-staged-it, so repeats group together:
    "Staged 12 item decision(s)" → "Staged item decisions"."""
    a = re.sub(r"\s*\((?:staged by|re-ran|today's)[^)]*\)", "", action or "")
    a = re.sub(r"#?\b\d[\d,]*\s*", "", a)
    a = re.sub(r"\s*\(from [^)]*\)", "", a)
    return a.replace("(s)", "s").replace("  ", " ").strip(" :—-") or (action or "")


@st.cache_data(ttl=600, show_spinner=False)
def load_upc_groups(upcs: tuple) -> dict:
    return dept_mapping.groups_for_upcs(ENGINE, list(upcs)) if upcs else {}


def _act_upcs(details, target) -> list:
    try:
        d = json.loads(details) if isinstance(details, str) else None
        if isinstance(d, dict) and d.get("upcs"):
            return [str(u) for u in d["upcs"]]
    except ValueError:
        pass
    return re.findall(r"\b\d{8,14}\b", target or "")


def _act_group_label(cid, facts) -> str:
    f = facts.get(cid) or {}
    bits = [b for b in (f.get("raw_department"), f.get("raw_category"), f.get("raw_subcategory")) if b]
    return f"{(f.get('source_key') or '').upper()} — {' / '.join(bits) or '(blank Department/Category/Subcategory)'}"


def _act_expand(v: pd.DataFrame) -> pd.DataFrame:
    """One row per (change, group it touched): Department Review work is on
    its group already; an item change counts on the group each of its UPCs
    sits in; a source change is on the source. Adds subject / where / n."""
    v = v.drop(columns=["subject", "where"], errors="ignore")
    facts = load_group_facts()
    item_rows = v[(v["area"] == "Items") | ((v["area"] == "Pushed live") & v["combo_id"].isna())]
    all_upcs = sorted({u for r in item_rows.itertuples() for u in _act_upcs(r.details, r.target)})
    upc_group = load_upc_groups(tuple(all_upcs))
    out = []
    for r in v.to_dict("records"):
        area = r["area"]
        if area in ("Department Review", "Pushed live", "Undo / Redo") and pd.notna(r["combo_id"]):
            cid = int(r["combo_id"])
            out.append({**r, "subject": _act_group_label(cid, facts) if cid in facts else r["target"],
                        "where": (facts.get(cid) or {}).get("where", "—")})
        elif area == "Items" or (area == "Pushed live" and pd.isna(r["combo_id"])):
            upcs = _act_upcs(r["details"], r["target"])
            if not upcs:
                if area == "Pushed live" and isinstance(r["target"], str):  # a source push
                    out.append({**r, "subject": f"Source: {r['target'].upper()}", "where": "Source"})
                continue
            for cid, n in Counter(upc_group.get(u) for u in upcs).items():
                out.append({**r, "n": n,
                            "subject": _act_group_label(int(cid), facts) if cid is not None and int(cid) in facts
                            else "Items not in any group (added by hand)",
                            "where": (facts.get(int(cid)) or {}).get("where", "—") if cid is not None else "—"})
        elif area == "Sources" and isinstance(r["target"], str):
            out.append({**r, "subject": f"Source: {r['target'].upper()}", "where": "Source"})
    return pd.DataFrame(out, columns=list(v.columns) + ["subject", "where"]) if out else v.assign(subject=None, where=None).iloc[0:0]


def _act_subject(area: str, target) -> str | None:
    if area in ("Department Review", "Pushed live", "Undo / Redo") and isinstance(target, str) and " — " in target:
        return target
    if area == "Items" or (area == "Pushed live" and isinstance(target, str) and target[:1].isdigit()):
        return "Item changes (adds, deletes, UPC overrides)"
    if area == "Sources" and isinstance(target, str):
        return f"Source: {target.upper()}"
    return None


def _act_prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["kind"] = df["action"].map(_act_kind)
    df["subject"] = [_act_subject(a, t) for a, t in zip(df["area"], df["target"])]
    df["day"] = df["when"].dt.date
    df["n"] = df["n_items"].fillna(0).astype(int)
    return df


def _act_summary_text(g: pd.DataFrame) -> str:
    """ "Staged item decisions ×4 · Pushed ×1" for one slice of the log."""
    counts = g["kind"].value_counts()
    parts = [k + (f" ×{n}" if n > 1 else "") for k, n in counts.items()]
    return " · ".join(parts[:2]) + (f" · +{len(parts) - 2} more" if len(parts) > 2 else "")


def render_activity_tab() -> None:
    """Editors and admins: what everyone has been changing — by person, by
    group, and day by day — plus what each has staged right now."""
    st.subheader("Activity")
    periods = {"Today": 1, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90, "All time": None}
    now_local = pd.Timestamp.now(tz="America/Los_Angeles")
    f1, f2, f3, f4 = st.columns([1.2, 1.8, 1.8, 2])
    period = f1.selectbox("Period", list(periods), index=1, key="act_period", label_visibility="collapsed")
    days = periods[period]
    since = None
    if days:
        since = (now_local.normalize() - pd.Timedelta(days=days - 1)).tz_convert("UTC").tz_localize(None).to_pydatetime()
    log = load_activity(since)
    people_all = sorted(set(log["actor"])) if not log.empty else []
    people = f2.multiselect("People", people_all, key="act_people", placeholder="Everyone", label_visibility="collapsed")
    areas = f3.multiselect("Areas", dept_mapping.ACTIVITY_AREAS, key="act_areas", placeholder="Every area",
                           label_visibility="collapsed")
    q = f4.text_input("Search", key="act_search", placeholder="Group, UPC, source, what happened…",
                      label_visibility="collapsed").strip().lower()
    vias = st.multiselect("How", dept_mapping.ACTIVITY_VIAS, key="act_via", placeholder="Made any way (in the app, a workbook, a file…)",
                          label_visibility="collapsed")
    view = log
    if vias:
        view = view[view["via"].isin(vias)]
    if people:
        view = view[view["actor"].isin(people)]
    if areas:
        view = view[view["area"].isin(areas)]
    if q and not view.empty:
        blob = (view["action"].fillna("") + " " + view["target"].fillna("") + " " + view["actor"]).str.lower()
        view = view[blob.str.contains(q, regex=False)]

    staged = load_staged_by_person()
    if people:
        staged = staged[staged["person"].isin(people)]

    if view.empty:
        m1, m2 = st.columns(2)
        m1.metric("Changes", 0)
        m2.metric("Staged now, not pushed", f"{int(staged['n_changes'].sum()) if not staged.empty else 0:,}")
        st.caption(f"Nothing recorded for {period.lower()}" + (" with these filters." if (people or areas or q) else "."))
        _render_staged_now(staged)
        return
    v = _act_prepare(view)
    pushed = v[v["area"] == "Pushed live"]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Changes", f"{len(v):,}")
    m2.metric("People", f"{v['actor'].nunique():,}")
    m3.metric("Groups touched", f"{v['target'].nunique():,}", help="Groups, items or sources changed")
    m4.metric("Items pushed live", f"{int(pushed['n'].sum()):,}")
    m5.metric("Staged now", f"{int(staged['n_changes'].sum()) if not staged.empty else 0:,}", help="Staged, not pushed yet")

    st.download_button(f"Download these {len(v):,} change(s) as CSV", _activity_frame(v).to_csv(index=False).encode("utf-8"),
                       file_name=f"Activity {period} {now_local:%Y-%m-%d}.csv", mime="text/csv", key="act_csv_top",
                       help="Every line matching the filters above — opens in Excel")
    t_people, t_groups, t_days, t_staged = st.tabs(["By person", "By group", "By day", "Staged now"])

    with t_people:
        area_cols = [x for x in dept_mapping.ACTIVITY_AREAS if x in set(v["area"])]
        tbl = v.pivot_table(index="actor", columns="area", values="activity_id", aggfunc="count", fill_value=0)
        tbl = tbl.reindex(columns=area_cols, fill_value=0)
        tbl.columns = [f"{ACT_ICONS.get(c, '')} {c}" for c in tbl.columns]
        how = v.pivot_table(index="actor", columns="via", values="activity_id", aggfunc="count", fill_value=0)
        how = how.reindex(columns=[x for x in dept_mapping.ACTIVITY_VIAS if x in how.columns], fill_value=0)
        how.columns = [f"via {c}" for c in how.columns]
        tbl = tbl.join(how)
        tbl.insert(0, "Staged now", staged.groupby("person")["n_changes"].sum().reindex(tbl.index).fillna(0).astype(int)
                   if not staged.empty else 0)
        tbl.insert(0, "Items pushed", pushed.groupby("actor")["n"].sum().reindex(tbl.index).fillna(0).astype(int))
        tbl.insert(0, "Groups", v.groupby("actor")["target"].nunique())
        tbl.insert(0, "Changes", v.groupby("actor").size())
        tbl.insert(0, "Last active", v.groupby("actor")["when"].max().dt.strftime("%b %d %I:%M %p"))
        tbl = tbl.sort_values("Changes", ascending=False)
        tbl.index.name = "Person"
        st.dataframe(tbl, width="stretch", height=min(400, 40 + 35 * len(tbl)),
                     column_config={c: st.column_config.NumberColumn(format="%d") for c in tbl.columns if c != "Last active"})
        who = st.selectbox("Show one person's work", list(tbl.index), index=None, key="act_person",
                           placeholder="Pick a person to see their work, day by day…")
        if who:
            _render_days(v[v["actor"] == who], key=f"act_pd_{who}", show_person=False)

    with t_groups:
        _render_by_group(v, key="act_groups")

    with t_days:
        _render_days(v, key="act_days")
        with st.expander("Every line (full log)"):
            n_pages = max(1, -(-len(v) // ACT_LOG_PAGE))
            pg = st.number_input(f"Page (of {n_pages:,}, newest first)", 1, n_pages, 1, key="act_log_page") if n_pages > 1 else 1
            _activity_table(v.iloc[(pg - 1) * ACT_LOG_PAGE: pg * ACT_LOG_PAGE], "act_log_tbl")

    with t_staged:
        _render_staged_now(staged)


def _day_table(g: pd.DataFrame, key: str) -> None:
    e = _act_expand(g)
    rest = g[~g["activity_id"].isin(e["activity_id"])].drop(columns=["subject", "where"], errors="ignore").assign(subject=None, where=None)
    e = pd.concat([e, rest], ignore_index=True) if not rest.empty else e
    rows = (e.groupby(["area", "kind", "via"], sort=False, dropna=False)
            .agg(times=("activity_id", "nunique"), subjects=("subject", "nunique"), items=("n", "sum"),
                 first=("when", "min"), last=("when", "max"), example=("subject", "first"))
            .reset_index().sort_values("last", ascending=False))
    out = pd.DataFrame({
        "Time": [f"{f:%I:%M %p}" + (f" – {l:%I:%M %p}" if t > 1 else "") for f, l, t in zip(rows["first"], rows["last"], rows["times"])],
        "What": [f"{k}" + (f"  ×{t}" if t > 1 else "") for a, k, t in zip(rows["area"], rows["kind"], rows["times"])],
        "On": [e_ if n <= 1 else f"{n} groups" for e_, n in zip(rows["example"].fillna(""), rows["subjects"])],
        "How": rows["via"], "Items": rows["items"],
    })
    st.dataframe(out, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(out)), key=key,
                 column_config={"Items": st.column_config.NumberColumn(format="%d"),
                                "Time": st.column_config.TextColumn(width="medium"),
                                "On": st.column_config.TextColumn(width="large")})


def _render_days(v: pd.DataFrame, key: str, show_person: bool = True) -> None:
    """Each day, then each person in it, folded to one line per kind of work."""
    newest = v["day"].max()
    for day, g in sorted(v.groupby("day"), key=lambda kv: kv[0], reverse=True):
        who = f" by {g['actor'].nunique()} person(s)" if show_person else ""
        with st.expander(f"**{pd.Timestamp(day):%A, %b %d}** — {len(g):,} change(s){who}", expanded=day == newest,
                         key=f"{key}_{day}"):
            if not show_person:
                _day_table(g, f"{key}_{day}_t")
                continue
            for person, gp in sorted(g.groupby("actor"), key=lambda kv: -len(kv[1])):
                with st.expander(f"**{person}** — {len(gp):,} change(s), {gp['when'].min():%I:%M %p} – {gp['when'].max():%I:%M %p}",
                                 key=f"{key}_{day}_{person}"):
                    _day_table(gp, f"{key}_{day}_{person}_t")


def _render_by_group(v: pd.DataFrame, key: str) -> None:
    """One row per group — item changes counted on the groups their items
    sit in — with the tab each group is on now."""
    g = _act_expand(v)
    if g.empty:
        st.caption("Nothing tied to a particular group or source.")
        return
    agg = g.groupby("subject", sort=False).agg(
        where=("where", "first"), changes=("activity_id", "nunique"), last=("when", "max"),
        people=("actor", lambda x: ", ".join(sorted(set(x)))),
        pushed=("area", lambda x: (x == "Pushed live").any()),
    )
    items = g[g["area"].isin(["Pushed live", "Items"])].groupby("subject")["n"].sum()
    most = g.groupby("subject")["kind"].agg(lambda x: x.value_counts().index[0])
    how = g.groupby("subject")["via"].agg(lambda x: " + ".join(x.value_counts().index[:2]))
    rows = agg.assign(items=items.reindex(agg.index).fillna(0).astype(int), mostly=most.reindex(agg.index),
                      how=how.reindex(agg.index))
    rows = rows.sort_values("last", ascending=False).reset_index()
    out = pd.DataFrame({
        "Group": rows["subject"], "Now in": rows["where"], "Mostly": rows["mostly"], "How": rows["how"],
        "Changes": rows["changes"],
        "Items": rows["items"], "Live": rows["pushed"].map({True: "Yes", False: ""}),
        "People": rows["people"], "Last change": rows["last"].dt.strftime("%b %d %I:%M %p"),
    })
    c1, c2 = st.columns([3, 2])
    q = c1.text_input("Find a group", key=f"{key}_q", placeholder="Find a group…", label_visibility="collapsed").strip().lower()
    where_opts = [w for w in ("Crosswalk", "Unmatched", "Broken Out", "Decided", "Source") if w in set(out["Now in"])]
    where = c2.multiselect("Now in", where_opts, key=f"{key}_where", placeholder="Every tab", label_visibility="collapsed")
    if q:
        out = out[out["Group"].str.lower().str.contains(q, regex=False)]
    if where:
        out = out[out["Now in"].isin(where)]
    st.caption(f"{len(out):,} group(s) · newest first")
    st.dataframe(out, hide_index=True, width="stretch", key=key, height=min(560, 40 + 35 * len(out)),
                 column_config={"Group": st.column_config.TextColumn(width="large"),
                                "Now in": st.column_config.TextColumn(help="The tab the group is on right now"),
                                "Mostly": st.column_config.TextColumn(help="The kind of change made most on it"),
                                "Items": st.column_config.NumberColumn(format="%d", help="Items changed or pushed in it"),
                                "Live": st.column_config.TextColumn(width="small", help="= something here was pushed live")})


def _render_staged_now(staged: pd.DataFrame) -> None:
    if staged.empty:
        st.caption("Nobody has anything staged right now.")
        return
    pv = staged.pivot_table(index="person", columns="what", values="n_changes", aggfunc="sum", fill_value=0)
    pv.insert(0, "Items affected", staged.groupby("person")["n_items"].sum().fillna(0).astype(int))
    pv.index.name = "Person"
    st.caption("Staged and waiting on Pending Changes — not live yet.")
    st.dataframe(pv, width="stretch")


def _activity_frame(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "When": df["when"].dt.strftime("%Y-%m-%d %I:%M %p"), "Person": df["actor"], "Area": df["area"],
        "What": df["action"], "How": df["via"], "Group / item": df["target"], "Items": df["n_items"],
    })


def _activity_table(df: pd.DataFrame, key: str) -> None:
    st.dataframe(_activity_frame(df), hide_index=True, width="stretch", height=min(600, 40 + 35 * len(df)), key=key,
                 column_config={"Items": st.column_config.NumberColumn(format="%d")})


# ---------------------------------------------------------------------------
# Router: only the chosen tab's code runs on each rerun.
# Everything from here through Snapshots is only ever reachable when
# active_tab actually equals one of these names — and tab_names above
# only ever includes the admin-only ones (Sources, Pending Changes,
# Upload & Ingest, Merge, Snapshots) for is_admin. A plain "editor"
# here but can never make active_tab become one of the admin-only
# values in the first place, so no extra is_admin check is needed
# inside those specific tab bodies below.
# ---------------------------------------------------------------------------
REVIEWER_TABS = {
    "Department Review": render_department_review_tab,
    "Add Item": render_add_item_tab,
    "Delete Item": render_delete_item_tab,
    "UPC Overrides": render_upc_overrides_tab,
    "Sources": render_sources_tab,
    "Pending Changes": render_pending_changes_tab,
    "Upload & Ingest": render_upload_ingest_tab,
    "Merge": render_merge_tab,
    "Snapshots": render_snapshots_tab,
    "Activity": render_activity_tab,
}
if active_tab == "Item Master":
    render_item_master_tab()
elif is_reviewer and active_tab in REVIEWER_TABS:
    REVIEWER_TABS[active_tab]()


def sync_url() -> None:
    """Keep the address matching what's on screen (only non-default values)."""
    want = {"tab": st.session_state.get("active_tab")}
    if want["tab"] == "Department Review":
        sub = st.session_state.get("dept_review_subtab")
        want["sub"] = sub
        f = get_shared_dept_filter()
        if f.get("search"):
            want["q"] = f["search"]
        if f.get("sort_label"):
            want["sort"] = f["sort_label"]
        if f.get("sort_desc") is False:
            want["desc"] = "0"
        if f.get("page_size") not in (None, DEFAULT_GROUP_PAGE_SIZE):
            want["size"] = str(f["page_size"])
        page = st.session_state.get(DEPT_PAGE_KEYS[sub], 1) if sub in DEPT_PAGE_KEYS else 1
        if page and page > 1:
            want["page"] = str(page)
        if sub == "Broken Out" and st.session_state.get("_url_group_now"):
            want["group"] = str(st.session_state["_url_group_now"])
    elif want["tab"] == "Item Master":
        for param, key in IM_URL.items():
            v = st.session_state.get(key)
            if v not in (None, "", "All") and not (param == "im_page" and v == 1) and not (param == "im_size" and v == IM_DEFAULT_PAGE_SIZE):
                want[param] = str(v)
        if st.session_state.get("im_manual_only"):
            want["im_manual"] = "1"
    want = {k: v for k, v in want.items() if v is not None}
    if dict(st.query_params) != want:
        st.query_params.clear()
        st.query_params.update(want)


sync_url()
save_workspace()
