"""
NWG Item Master App
--------------------
Self-service replacement for the Excel-based ingestion pipeline in
script.py: configure distributor sources, upload their files, merge by
source priority, and browse/edit the resulting item master — all backed by
Azure SQL, no Excel involved.
"""

import hashlib
import html
from collections import Counter
from datetime import datetime, timezone
import json
import io
import re
import threading
import time
import uuid
from contextlib import contextmanager

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
        _error_card("🔒", "Can't reach the database from this network", [
            f"The database only lets in approved internet addresses, and this one{where} isn't approved yet — "
            "this usually happens after switching networks (home, office, hotspot).",
            "**To fix it:** an admin adds this address in the Azure portal → the SQL server → **Networking**. "
            "It can take up to 5 minutes to start working.",
        ], ex)
        return True
    if isinstance(ex, DBAPIError) and is_transient_connection_error(ex):
        _error_card("⏳", "Still connecting to the database…", [
            "It's taking longer than usual to wake up. This page tries again by itself in a few seconds — "
            "nothing you were working on is lost.",
        ], ex, retry_label="Try again now")
        import streamlit.components.v1 as _components
        _components.html("<script>setTimeout(() => window.parent.location.reload(), 10000);</script>", height=0)
        return True
    # Anything else is a real bug: record it for the admins (🔔 → App errors).
    where = " / ".join(str(v) for v in (st.session_state.get("active_tab"), st.session_state.get("dept_review_subtab")
                                        if st.session_state.get("active_tab") == "Department Review" else None) if v)
    ref = None
    if globals().get("ENGINE") is not None:
        ref = dept_mapping.log_app_error(ENGINE, st.session_state.get("name"), where, ex)
    if ref is None:
        import logging
        logging.getLogger("item_master_app").error("Unexpected error (%s)", where, exc_info=ex)
    _error_card("⚠️", "Something went wrong", [
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
st.markdown(
    """
    <div class="app-busy-lock"></div>
    <div class="app-loading-overlay">
        <div class="app-loading-spinner"></div>
        <div class="app-loading-text">Loading…</div>
    </div>
    <style>
    /* Small floating corner chip, NOT a full-page overlay — replaces an
       earlier version of this that covered the whole viewport with a
       fixed, blurred inset:0 div. That version caused real, repeated
       scroll-blocking bugs (reported live twice): pointer-events on a
       full-screen fixed element is a fragile thing to get right across
       browsers/timing, and the failure mode is the page looking normal but
       refusing to scroll, with no visible cause. A small corner badge that
       never covers the scrollable content can't have that failure mode at
       all — there's nothing there to block, regardless of pointer-events
       or timing. Delayed-show, instant-hide: a rerun finishing within
       ~350ms (switching tabs, toggling a filter) never shows this at all,
       since the tag reverts to the base (opacity:0, no delay) rule before
       the delayed show-transition below ever completes. */
    /* While the app is working (anything past ~0.4s: a popup calculating,
       a push, a page change), a see-through layer over everything —
       popups included — takes the clicks, so nobody clicks something
       else mid-way. It's hidden (so it can't block anything) the instant
       the work finishes; visibility, unlike pointer-events, can be
       delayed, so quick clicks never see it at all. */
    /* One loading screen only: Streamlit's own "Running load_x(...)"
       lines, its running/Stop corner and Deploy button stay hidden, and the
       page doesn't fade while it redraws — the centered box says it all.
       The lock and box below key off Streamlit marking the app as running. */
    [data-testid="stStatusWidget"] { visibility: hidden !important; }
    [data-testid="stAppDeployButton"] { display: none !important; }
    [data-stale="true"], .stale-element { opacity: 1 !important; filter: none !important; transition: none !important; }
    div[data-testid="stSpinner"]:has(code) { display: none !important; }
    /* The app's own "what I'm doing" messages (st.spinner) move into the
       loading card instead of sitting behind the veil. */
    [data-test-script-state="running"] div[data-testid="stSpinner"] {
        position: fixed !important;
        top: calc(42% + 3.6rem);
        left: 50%;
        transform: translateX(-50%);
        z-index: 1000300;
        width: min(30rem, 86vw);
        text-align: center;
        justify-content: center;
        color: rgba(255, 255, 255, 0.72);
        font-size: 0.85rem;
        animation: app-fade-in 0.2s ease-out 0.3s both;
    }
    [data-test-script-state="running"] div[data-testid="stSpinner"] i,
    [data-test-script-state="running"] div[data-testid="stSpinner"] svg { display: none !important; }
    /* Content arriving while a page builds (group cards load in one after
       another) fades in instead of popping into place. Only on first
       appearance — anything already on screen isn't re-animated. Cards
       (bordered boxes) also rise a few pixels; plain elements only fade,
       since a moving parent would drag the fixed top bar along with it. */
    @keyframes app-fade-in { from { opacity: 0; } to { opacity: 1; } }
    @keyframes app-card-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
    section.main div[data-testid="stElementContainer"],
    div[data-testid="stMain"] div[data-testid="stElementContainer"] {
        animation: app-fade-in 0.22s ease-out both;
    }
    div[data-testid="stMain"] div[data-testid="stVerticalBlockBorderWrapper"],
    div[data-testid="stMain"] div[data-testid="stVerticalBlock"][class*="st-key-"]:not(.st-key-topbar):not(:has(.st-key-topbar)) {
        animation: app-card-in 0.3s cubic-bezier(0.2, 0.7, 0.3, 1) both;
    }
    @media (prefers-reduced-motion: reduce) {
        div[data-testid="stMain"] * { animation: none !important; }
    }
    .app-busy-lock {
        position: fixed;
        inset: 0;
        z-index: 1000100;
        cursor: progress;
        background: rgba(0, 0, 0, 0);
        visibility: hidden;
        transition: visibility 0s linear 0s, background 0.1s;
    }
    [data-test-script-state="running"] .app-busy-lock {
        visibility: visible;
        background: rgba(14, 17, 23, 0.6);
        backdrop-filter: blur(3px);
        -webkit-backdrop-filter: blur(3px);
        transition: visibility 0s linear 0.3s, background 0.25s ease-in 0.3s;
    }
    /* Centered "Working…" box, shown together with the click lock so the
       grey screen always says why. */
    .app-loading-overlay {
        position: fixed;
        top: 42%;
        left: 50%;
        transform: translate(-50%, -50%);
        z-index: 1000200;
        background: rgba(22, 26, 34, 0.96);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 14px;
        padding: 1.4rem 2.2rem 1.2rem;
        box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45);
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0.85rem;
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.12s ease-out, transform 0.12s ease-out;
    }
    [data-test-script-state="running"] .app-loading-overlay {
        opacity: 1;
        transform: translate(-50%, -50%) scale(1);
        transition: opacity 0.2s ease-in 0.3s;
    }
    /* An operation with its own real st.progress() bar (Compute/Push Merge)
       renders this marker for its whole duration — the corner chip would
       otherwise still kick in after ~1s (it reacts to ANY running script,
       not to how long-running it turns out to be) and sit there redundant
       with the actual progress bar's own status text. */
    body:has(.own-progress-marker) .app-loading-overlay {
        opacity: 0 !important;
    }
    .app-loading-spinner {
        width: 2.1rem;
        height: 2.1rem;
        border-radius: 50%;
        border: 3px solid rgba(255, 255, 255, 0.12);
        border-top-color: #ff4b4b;
        animation: app-loading-spin 0.8s linear infinite;
    }
    @keyframes app-loading-spin {
        to { transform: rotate(360deg); }
    }
    .app-loading-text {
        color: rgba(255, 255, 255, 0.85);
        font-size: 0.9rem;
        font-weight: 500;
        letter-spacing: 0.01em;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with open("config.yaml") as f:
    auth_config = yaml.safe_load(f)

authenticator = stauth.Authenticate(
    auth_config["credentials"],
    auth_config["cookie"]["name"],
    auth_config["cookie"]["key"],
    auth_config["cookie"]["expiry_days"],
    auto_hash=False,
)

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
                or (k.startswith("wb_") and isinstance(v, dict) and "draft" in v)):
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


# One id per script run = one per click. Every Department Review write made
# during this run is recorded under it, so a click that writes several
# times ("Accept all" over 12 items) is ONE thing for the top-bar Undo.
CLICK_ID = uuid.uuid4().hex


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


def _warn_retrying(attempt, delay, exc):
    # A toast (not st.warning) because this is purely transient status — it
    # almost always resolves itself within a few seconds as an idle Azure
    # SQL database wakes up, and should fade on its own rather than sitting
    # inline on the page forever like an actual, actionable error would.
    st.toast(
        f"Database connection timed out — it's likely an idle Azure SQL database waking back "
        f"up. Retrying in {delay:.0f}s (attempt {attempt + 1})...",
        icon="⏳",
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
        f"⚠️ Raw data has changed for {len(stale)} {label} ({', '.join(stale)}) since the last "
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
def load_snapshots() -> pd.DataFrame:
    return dept_mapping.list_snapshots(ENGINE)


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
        bcol1.caption("Changed by field")
        bcol1.dataframe(pd.DataFrame(rows, columns=["Field", "# Changed"]), hide_index=True, width='stretch')
    if changed_by_source:
        rows = sorted(changed_by_source.items(), key=lambda kv: -kv[1])
        bcol2.caption("Changed by source")
        bcol2.dataframe(pd.DataFrame(rows, columns=["Source", "# Changed"]), hide_index=True, width='stretch')


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
                "It will apply on the next Merge push, or save again to retry.", "⚠️",
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
    msg = (f"Saved {what} and re-ran the Department engine — {moved:,} group(s) changed tab or department, "
           f"{synced['items']:,} item department(s) updated in Item Master.")
    if discarded:
        msg += f" {len(discarded)} staged decision(s) were discarded because their evidence changed."
    st.session_state["_toast"] = (msg, "⚙️")
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
        title = f"⚠️ {len(shown):,} staged change(s) didn't survive" + (f" — {len(mine):,} of them yours" if len(mine) else "")
    else:
        title = f"⚠️ {len(shown):,} of your staged change(s) didn't survive"
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
        st.error(f"⚠️ {len(blocked)} {label} already had a pending edit staged by someone else — yours wasn't saved for those.")
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
    """One plain-English sentence explaining why a Crosswalk/Unmatched
    group isn't auto-decided — the same underlying evidence
    dept_mapping.confidence_label reports, phrased as prose instead of a
    column of jargon (purity/tier/resolved_via never appear as raw
    labels here)."""
    suggested = row["suggested_department"]
    if pd.isna(suggested):
        suggested = None
    resolved_via = row["resolved_via"]
    if pd.isna(resolved_via):
        resolved_via = None
    if resolved_via == "Default from Key":
        return f"No matching evidence anywhere, but “{suggested}” is a saved default for this exact text."
    n_evidence = row["n_evidence"]
    if not n_evidence or not suggested:
        return "No matching evidence anywhere — you'll need to pick a Department yourself."
    if resolved_via and "single sibling" in resolved_via:
        return f"Only one similar group elsewhere suggests “{suggested}” — not enough on its own to trust automatically."
    purity = row["purity"]
    pct = f"{purity:.0%}" if not pd.isna(purity) else "0%"
    sentence = f"{pct} of {int(n_evidence):,} matching item(s) point to “{suggested}”"
    runner_up = row["runner_up_department"]
    runner_share = row["runner_up_share"]
    if runner_up and not pd.isna(runner_up):
        sentence += f", vs. {runner_share:.0%} for “{runner_up}”"
    if resolved_via == "Partially Chained":
        sentence += " (partly inferred through another distributor's own data)"
    elif resolved_via == "Chained - Insufficient":
        sentence += " (only reached through another distributor's data, and still not enough)"
    return sentence + " — not quite confident enough to decide on its own."


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


def render_filter_bar(tab_key: str, sort_options: dict, search_label: str, search_placeholder: str) -> tuple:
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
    Returns (search_text, sort_column_name, sort_desc)."""
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
        if not is_locked:
            shared.update(defaults)
        st.rerun()

    scol1, scol2 = st.columns([4, 1])
    sort_label = scol1.selectbox("Sort by", list(sort_options.keys()), key=sort_key, on_change=_touch)
    with scol2.container(key="dept_sort_desc_wrap"):
        sort_desc = st.checkbox("Descending", key=desc_key, on_change=_touch)
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
    pcol1, pcol2, pcol3 = st.columns([1, 1, 3])
    page_size = pcol1.selectbox("Groups per page", GROUP_PAGE_SIZES, key=size_key, on_change=_touch)
    total_pages = max(1, (df_len - 1) // page_size + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages
    page_num = pcol2.number_input("Page", min_value=1, max_value=total_pages, step=1, key=page_num_key)
    with pcol3.container(key=f"{tab_key}_page_caption"):
        st.caption(f"{matching_label} — page {page_num} of {total_pages}")
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


def render_affected_items_expander(combo_id: int, n_upcs_total: int, key_prefix: str, container=st) -> None:
    """A per-combo "see the actual rows this affects" drill-down — the
    aggregate count alone ("1,359 item(s)") doesn't let a person confirm
    they're really the items they think they are before approving a
    whole group at once. Collapsed by default everywhere it's used, so
    the normal review flow stays a quick scan; open it only when you want
    to check the actual data. `container` lets a caller place this inside
    a column (e.g. sharing a row with Admin override) instead of always
    taking a full row of its own."""
    # Lazy: a page of 25 cards used to fetch and draw 25 item lists nobody
    # had opened. Now the list is only fetched once someone opens it.
    exp = container.expander(f"Show affected items ({n_upcs_total:,})", key=f"{key_prefix}_{combo_id}", on_change="rerun")
    with exp:
        if not exp.open:
            return
        items_df = load_combo_member_items(combo_id)
        if items_df.empty:
            st.caption("No items currently in the item master for this group yet.")
        else:
            manual_count = items_df["manually_edited_by"].notna().sum()
            if manual_count:
                label = "item has" if manual_count == 1 else "items have"
                st.caption(f"✏️ {manual_count} {label} a manual correction on file for this group.")
            display_df = items_df.rename(columns={
                "upc": "UPC", "description": "Description", "brand": "Brand",
                "pack": "Pack", "size": "Size", "uom": "UOM",
            }).drop(columns=["manually_edited_by"])
            st.dataframe(
                display_df,
                hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items_df)),
            )


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
        with st.container(border=True):
            hc0, hc1, *hc_admin = st.columns([0.4, 8, 0.6] if is_admin else [0.4, 8])
            hc0.checkbox(
                "Save for later", value=False, key=f"dept_pending_snooze_combo_{combo_id}",
                label_visibility="collapsed",
                help="Move this to Saved for later — it's still unresolved, just out of the main Needs "
                     "agreement list until you check it again.",
            )
            hc1.markdown(f"**{(first['source_key'] or '').upper()} — {first['label']}** — {first['n_upcs_total']:,} item(s)")
            if hc_admin:
                admin_override_button(hc_admin[0], {
                    "kind": "combo", "combo_id": combo_id, "tier": first.get("tier"),
                    "source_key": first["source_key"], "raw_label": first["label"], "n_upcs_total": first["n_upcs_total"],
                    "label": f"{(first['source_key'] or '').upper()} — {first['label']}",
                }, key=f"admin_override_btn_dispute_{combo_id}")
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
                            st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {result['departments'][0]}.", "✅")
                        elif not result["departments"]:
                            st.session_state["_toast"] = (f"Withdrew your vote on **{first['label']}** — no suggestions left.", "↩️")
                        else:
                            st.session_state["_toast"] = (f"Withdrew your vote on **{first['label']}**.", "↩️")
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
                            st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {department}.", "✅")
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
                            "🚫",
                        )
                    elif not result["disputed"]:
                        st.session_state["_toast"] = (f"Resolved: **{first['label']}** → {new_dept}.", "✅")
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
    st.caption(f"⚖️ {len(suggestions_by_upc)} item(s) have a suggested change awaiting their owner's decision.")

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
                    st.session_state["_toast"] = (f"Accepted: {len(items)} item(s) → {department}.", "✅")
                    st.rerun()
                if gc3.button(deny_label, key=f"deny_upc_group_{key_base}", width='stretch'):
                    for it in items:
                        _deny(it["upc"], suggested_by)
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Denied: {len(items)} suggestion(s).", "🚫")
                    st.rerun()
            elif can_withdraw:
                if gc2.button(withdraw_label, key=f"withdraw_upc_group_{key_base}", width='stretch'):
                    for it in items:
                        _deny(it["upc"], suggested_by)
                    clear_dept_suggestion_caches()
                    st.session_state["_toast"] = (f"Withdrew {len(items)} suggestion(s).", "↩️")
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
                            st.session_state["_toast"] = (f"Accepted: {it['upc']} → {department}.", "✅")
                            st.rerun()
                        if ic3.button("Deny", key=f"deny_upc_item_{key_base}_{it['upc']}", width='stretch'):
                            _deny(it["upc"], suggested_by)
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Denied suggestion for {it['upc']}.", "🚫")
                            st.rerun()
                    elif can_withdraw:
                        if ic2.button("Withdraw", key=f"withdraw_upc_item_{key_base}_{it['upc']}", width='stretch'):
                            _deny(it["upc"], suggested_by)
                            clear_dept_suggestion_caches()
                            st.session_state["_toast"] = (f"Withdrew suggestion for {it['upc']}.", "↩️")
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
                                st.session_state["_toast"] = (f"Accepted: {len(picked)} item(s) → {department}.", "✅")
                                st.rerun()
                            if pc2.button(f"Deny picked ({len(picked)})", key=f"deny_upc_picked_{key_base}", width='stretch', disabled=not picked):
                                for it in picked:
                                    _deny(it["upc"], suggested_by)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Denied: {len(picked)} suggestion(s).", "🚫")
                                st.rerun()
                        elif can_withdraw:
                            if pc1.button(f"Withdraw picked ({len(picked)})", key=f"withdraw_upc_picked_{key_base}", width='stretch', disabled=not picked):
                                for it in picked:
                                    _deny(it["upc"], suggested_by)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Withdrew {len(picked)} suggestion(s).", "↩️")
                                st.rerun()
                    else:
                        st.dataframe(
                            pd.DataFrame([
                                {"UPC": it["upc"], "Description": it.get("description"), "Currently": pending_upc_changes.get(it["upc"], {}).get("department")}
                                for it in items
                            ]),
                            hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items)),
                        )


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


def clear_dept_review_caches() -> None:
    load_dept_review_queue.clear()
    load_broken_out_combos.clear()
    load_pending_upc_overrides.clear()
    load_combo_upc_decisions.clear()
    load_decided_combos.clear()


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
    with st.expander("📦 Monthly refresh — upload all of this month's files at once",
                     expanded=bool(st.session_state.get("_mr_reports"))):
        st.caption(
            "Drop every distributor file for the month. Each is matched to its source by the source's File Keyword "
            "(Sources tab), read with that source's cleaning rules, and compared with its last upload — a file much "
            "smaller than last time is held back until you tick it. Then one Merge draft is computed with every "
            "Department Review decision applied; review and push it on the Merge tab. For hosting, "
            "`scripts/monthly_refresh.py` does the same from an inbox folder on a schedule."
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
        label = {"ready": "✅ Ready", "suspicious": "⚠️ Much smaller than last time", "error": "❌ Can't read",
                 "skipped": "⏭️ No matching source"}
        st.dataframe(pd.DataFrame([{
            "File": r["file"], "Source": r.get("source_key") or "—",
            "Rows": r.get("rows"), "Rows last time": r.get("previous_rows"),
            "Status": ("❌ Two files for this source" if r.get("source_key") in dup else label.get(r["status"], r["status"])),
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
            st.session_state["_toast"] = (f"Ingested {len(take)} file(s). The Merge draft is ready on the Merge tab.", "📦")
            st.rerun()


@st.cache_data(show_spinner=False)
def bulk_template(kind: str, departments: tuple) -> bytes:
    return item_bulk.template_bytes(kind, list(departments))


def render_bulk_upload(kind: str, title: str) -> None:
    """Many items at once: download a blank template, fill it in (paste
    from any spreadsheet), upload it, check the preview, stage it all in
    one click. Staged changes go to Pending Changes like any single one."""
    ver_key = f"_bulk_{kind}_ver"
    with st.expander(f"📄 {title} — upload a spreadsheet", expanded=st.session_state.get(f"_bulk_{kind}_open", False)):
        st.caption(item_bulk.INSTRUCTIONS[kind] + " Everything uploaded is staged to Pending Changes, not applied straight away.")
        departments = tuple(load_departments()["department"].tolist())
        st.download_button(
            "⬇ Download the blank template", bulk_template(kind, departments),
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
                "📋",
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
                      placeholder="🔍 Search UPC, description, brand, group…").strip().lower()
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
    c1.download_button("⬇ Download (CSV)", view.drop(columns=["Tab"]).to_csv(index=False).encode(), file_name=f"{file_stem}.csv",
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
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("To stage", f"{count(owi.STAGE):,}")
    c2.metric("Moves to make", f"{count(owi.MOVE):,}")
    c3.metric("Already done", f"{count(owi.DONE):,}")
    c4.metric("Skipped", f"{count(owi.SKIP):,}")
    items, groups = p["items"], p["groups"]
    areas = ["Crosswalk", "Unmatched", "Broken Out", "Decided", "Item master"]
    tabs = st.tabs(["Summary", "Moves", "Group decisions"] + [f"{a} items" for a in areas])
    with tabs[0]:
        st.dataframe(summ, hide_index=True, width="stretch")
        st.caption("Items: a Broken Out group's item gets an item decision there; any other item gets a UPC override "
                   "of just its Department. “Already done” means the app already has it that way (live or staged).")
    with tabs[1]:
        _owi_table(p["moves"], f"{key}_moves")
    with tabs[2]:
        _owi_table(groups, f"{key}_groups")
    for tab, area in zip(tabs[3:], areas):
        with tab:
            _owi_table(items[items["Area"] == area], f"{key}_{area}")


def render_old_workbook_import() -> None:
    """Admin only: load the decisions people made in an old department
    workbook into Pending Changes — see old_workbook_import."""
    owi = old_workbook_import
    ver = st.session_state.get("_owi_ver", 0)
    done = st.session_state.get("_owi_done")
    with st.expander("📥 Import decisions from an old department workbook (admins)",
                     expanded=bool(done or st.session_state.get("_owi_open"))):
        st.caption(
            "Upload an old “…department_workbook.xlsx” from the Excel process. Only what a person decided is taken — "
            "approved Crosswalk/Unmatched groups, manually overridden Decided groups, manually reviewed or approved "
            "items, Final UPC Overrides, and the moves someone asked for (Break Out, Return to Crosswalk/Unmatched, "
            "Send to Broken Out). Anything the app already has that way is skipped. You get the full report before "
            "anything happens; then it's all staged to Pending Changes as you (moves happen straight away, as in the app)."
        )
        if done:
            r = done["result"]
            st.success(
                f"Imported **{done['name']}**: {r['moved']:,} move(s) made, {r['groups']:,} group decision(s) and "
                f"{r['item_decisions']:,} Broken Out item decision(s) staged in **Department Review → Pending Changes**, "
                f"{r['overrides']:,} UPC override(s) staged in the **Pending Changes** tab"
                + (f", {r['suggested']:,} went as suggestions (someone else owns those items)" if r["suggested"] else "")
                + (f", {r['blocked']:,} skipped (someone else staged them first)" if r["blocked"] else "")
                + f". A snapshot (#{r['snapshot']}) was taken first, so it can all be put back.", icon="✅")
            d1, d2 = st.columns(2)
            d1.download_button("⬇ Download the full report", done["report"], file_name=done["report_name"],
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
        up = st.file_uploader("Old department workbook (.xlsx)", type=["xlsx"], key=f"owi_file_{ver}")
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
        b1, b2 = st.columns(2)
        b1.download_button("⬇ Download this report", owi.report_excel(p, f"Import preview — {up.name}"),
                           file_name=report_name, key="owi_plan_report", width="stretch",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if b2.button(f"Stage {n_stage:,} change(s)" + (f" and make {n_move:,} move(s)" if n_move else ""),
                     type="primary", key="owi_apply", width="stretch", disabled=not (n_stage or n_move)):
            with st.spinner("Importing — staging everything as you…"):
                result = owi.apply(ENGINE, p, st.session_state["name"], is_admin, up.name)
            st.cache_data.clear()
            load_notifications.clear()
            st.session_state["_owi_done"] = {"result": result, "plan": p, "name": up.name, "report_name": report_name,
                                             "report": owi.report_excel(p, f"Imported {up.name} (snapshot #{result['snapshot']} taken first)")}
            st.session_state.pop("_owi", None)
            st.session_state["_owi_ver"] = ver + 1
            st.rerun()


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
                load_item_master_pending.clear()
                st.rerun()

    included_upcs = [upc for upc in pending if st.session_state.get(f"im_pending_include_{upc}", True)]
    st.warning(
        f"Pushing applies the {len(included_upcs):,} included change(s) to the live database "
        f"immediately — this affects the real item master, not a preview. The other "
        f"{len(pending) - len(included_upcs):,} unchecked change(s) stay pending, untouched."
    )
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
                load_source_pending_changes.clear()
                st.rerun()
    st.warning(
        f"Pushing applies all {len(pending):,} staged source change(s) to the live database immediately."
    )
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
        st.toast(toast[0], icon=toast[1])
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

    t1, t2, t3, t4, t6, t5 = st.columns([1.5, 1.0, 1.1, 1.0, 0.9, 1.8])
    pick = t1.selectbox(f"Set {value_label}", [""] + options, key=f"wb_pick_{key}_{v}",
                        label_visibility="collapsed", placeholder=f"Pick a {value_label}…")
    set_checked = t2.button("Set ✓ rows", width='stretch', key=f"wb_setchk_{key}_{v}",
                            help=f"Give every checked row the {value_label} picked on the left.")
    set_shown = t3.button(f"Set all {len(shown):,} shown", width='stretch', key=f"wb_setall_{key}_{v}",
                          help=f"Give every row currently shown (after the filter) the {value_label} picked on the left.")
    clear_checked = t4.button("Clear ✓ rows", width='stretch', key=f"wb_clear_{key}_{v}", help="Blank out the checked rows.")
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
            "✓": st.column_config.CheckboxColumn(width="small", help="Check rows, then use Set ✓ rows / Clear ✓ rows."),
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
        + (f" · :red[{n_bad} not a Department — pick a real one or clear them]" if n_bad else "")
        + " · Tips: click a column header to sort (e.g. Brand) so similar items sit together · select a "
          "cell and **drag its corner handle** to fill down · **Ctrl+D** fills a selection down from its top "
          "cell · **Ctrl+V** pastes a column copied from Excel · the top-bar **Undo** takes back each step."
    )

    checked = [u for u, c in zip(view["UPC"], edited["✓"]) if c]
    if not any([set_checked, set_shown, clear_checked, clear_all, fill_sugg, stage]):
        return
    before = dict(draft)
    label = None
    if (set_checked or set_shown) and not pick:
        st.session_state["_toast"] = (f"Pick a {value_label} on the left first.", "👈")
    elif set_checked and not checked:
        st.session_state["_toast"] = ("Check some rows first (the ✓ column).", "☑️")
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
    with st.expander(f"Review items ({len(review)} left)", key=f"broken_out_expander_{combo_id}"):
        if review.empty:
            st.caption("Nothing left to decide here.")
        else:
            manual_count = review["Manually Edited By"].notna().sum()
            if manual_count:
                st.caption(f"✏️ {manual_count} item(s) have a manual correction on file — see the column below.")
            _workbench_body(bo, review, options, info_cols=["Description", "Brand", "Pack", "Size", "UOM", "Manually Edited By", "UPC"],
                            suggestion_col="Suggested", title=title, show_stage=False)
    if not auto.empty:
        with st.expander(f"Auto-decided items, not yet confirmed ({len(auto)})", key=f"broken_out_auto_expander_{combo_id}"):
            st.caption("Filled in automatically (Brand / UPC Root / Description Match). Keep a row's auto department with "
                       "**Fill … with auto**, or pick a different one — only rows you fill in are staged, and staging "
                       "sends them to Pending Changes like any other decision.")
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
    c2.caption(f"{n_rev:,} review item(s) + {len(ready) - n_rev:,} auto-decided item(s) filled in. Staging sends them to "
               "Pending Changes; the top-bar Undo takes it back.")
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
        st.caption(f"One sheet with every item in the group, already holding what's in the grids above. The Department "
                   f"column is a dropdown: pick a Department, or **{USE_AUTO}** to keep that row's own auto (or "
                   "suggested) department — drag it down every row you agree with. Upload the file and either put it "
                   "into the grids to keep working, or stage it straight to Pending Changes. Undo takes either back.")
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
        st.download_button("⬇ Download as Excel", _excel_with_dropdown(out, "Department", [USE_AUTO] + options),
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
        fc1.markdown(f"📄 **{pending['name']}** — uploaded, not used yet")
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
            st.session_state["_toast"] = (f"Put {n_diff} Department(s) from {up.name} into the grids — Undo takes it back.", "📥")
            st.rerun()


def open_dept_dialog(key: str, info: dict) -> None:
    """Streamlit allows one open dialog per run, and closing one with X/Esc
    doesn't clear its session key on its own — so opening any Department
    Review popup clears every other one first (see each dialog's
    on_dismiss for the X/Esc side)."""
    for k in DEPT_DIALOG_KEYS:
        st.session_state.pop(k, None)
    st.session_state[key] = info
    st.rerun()


def _dismiss(key: str):
    return lambda: st.session_state.pop(key, None)


def admin_override_button(container, info: dict, key: str) -> None:
    """The one entry point to every admin override — a small button only
    admins ever see, so a non-admin's card has no empty space where admin
    controls would otherwise sit. `info` carries what the popup needs:
    kind ("combo" or "upc_group"), combo_id, label, and for a combo its
    tier/source_key/n_upcs_total."""
    if is_admin and container.button("🛡️", key=key, help="Admin override", width='stretch'):
        open_dept_dialog("dept_admin_override", info)


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
            _done((f"Admin override: **{info['raw_label']}** → {dept}.", "🛡️"))
        if locked_by and c2.button("Remove override (unlock)", width='stretch'):
            with track(combo_id, info["label"], "Removed admin override"):
                dept_mapping.remove_combo_override(ENGINE, combo_id, actor)
            _done((f"Unlocked **{info['raw_label']}** — open to normal editing again.", "🔓"))
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
            _done((f"Admin: {len(to_override)} item(s) overridden, {len(to_unlock)} unlocked in **{info['raw_label']}**.", "🛡️"))

        with st.expander(f"Whole group at once ({len(items):,} item(s))"):
            dept = st.selectbox("Force every item to", departments, key=f"admin_override_dialog_group_dept_{combo_id}")
            c1, c2 = st.columns(2)
            if c1.button("Override whole group", width='stretch'):
                with track(combo_id, info["label"], f"Admin override: whole group → {dept}"):
                    n = dept_mapping.admin_override_upc_group(ENGINE, combo_id, dept, actor)
                _done((f"Admin override: {n} item(s) in **{info['raw_label']}** → {dept}.", "🛡️"))
            if locked_n and c2.button(f"Unlock all {locked_n}", width='stretch'):
                with track(combo_id, info["label"], "Admin: unlocked whole group"):
                    n = dept_mapping.remove_upc_override_group(ENGINE, combo_id, actor)
                _done((f"Unlocked {n} item(s) in **{info['raw_label']}**.", "🔓"))


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
                    "⚠️",
                )
                clear_dept_suggestion_caches()
                st.rerun()
            load_dept_recent_moves.clear()
            load_broken_out_claims.clear()
            clear_dept_suggestion_caches()
            clear_dept_review_caches()
            st.session_state["_toast"] = (f"Undone: **{info['label']}** — now {target.removeprefix('Stay in ')}.", "↩️")
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
            st.session_state["_toast"] = (f"Asked **{path['authorized']}** to undo **{info['label']}**.", "⏳")
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
    st.session_state["_toast"] = (f"{send_back_label}: **{label}**", "↩️")
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
    st.session_state["_toast"] = (f"Moved to Broken Out: **{label}**", "🧩")
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
    return _BackgroundRefreshed(lambda username, since: dept_mapping.get_notifications(ENGINE, username, since), ttl=60)


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
    st.markdown(f"**⚠️ App errors ({len(errors)})**")
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


def render_notifications_sidebar() -> None:
    """What's waiting on you, and what changed on your work since your last
    visit — in the sidebar, out of the way of the page itself. Search and a
    type filter narrow it; each list shows its newest few with the rest one
    click away. Admins can also see everyone else's, one person at a time."""
    name = st.session_state["name"]
    since = _notif_since()
    notes = load_notifications(name, since)
    n_new = sum(1 for n in notes["action"] + notes["updates"] if n["new"])
    SHOW = 6
    kinds = dept_mapping.NOTIFICATION_KINDS

    def _card(n, key):
        with st.container(border=True):
            badge = "🆕 " if n["new"] else ""
            st.markdown(f"{badge}**{n['title']}**")
            st.caption(f"{kinds.get(n.get('kind'), '')} · {n['detail']}" if n.get("kind") else n["detail"])
            st.button(
                f"View on {n['tab']}", key=key, width='stretch',
                on_click=_open_notification, args=(n["tab"], n["search"]),
            )

    def _section(title, items, empty, key):
        st.markdown(f"**{title}**" + (f" ({len(items)})" if items else ""))
        if not items:
            st.caption(empty)
            return
        for i, n in enumerate(items[:SHOW]):
            _card(n, f"notif_{key}_{i}")
        if len(items) > SHOW:
            with st.expander(f"{len(items) - SHOW} more"):
                for i, n in enumerate(items[SHOW:], start=SHOW):
                    _card(n, f"notif_{key}_{i}")

    def _filter(items, q, kind):
        if kind != "All types":
            items = [n for n in items if kinds.get(n.get("kind")) == kind]
        if q:
            items = [n for n in items if q in n["title"].lower() or q in n["detail"].lower()]
        return items

    with st.sidebar:
        st.markdown("### 🔔 Notifications" + (f" · {n_new} new" if n_new else ""))
        if is_admin:
            render_app_errors()
        view = "Mine"
        if is_admin:
            view = st.segmented_control(
                "Whose", ["Mine", "Team"], default="Mine", key="notif_view", label_visibility="collapsed",
                help="Team: what's waiting on each other reviewer, one person at a time.",
            ) or "Mine"
        q = st.text_input(
            "Search notifications", key="notif_search", placeholder="🔍 Search — group, person, or what happened",
            label_visibility="collapsed",
        ).strip().lower()
        kind = st.selectbox("Type", ["All types"] + list(kinds.values()), key="notif_kind", label_visibility="collapsed")

        if view == "Mine":
            st.caption(f"New since your last visit ({pd.Timestamp(since).strftime('%m/%d %H:%M')} UTC) are marked 🆕.")
            action, updates = _filter(notes["action"], q, kind), _filter(notes["updates"], q, kind)
            filtered = (q or kind != "All types")
            _section("Waiting on you", action, "Nothing matches." if filtered else "Nothing is waiting on you.", "action")
            st.divider()
            _section("Since your last visit", updates, "Nothing matches." if filtered else "Nothing new on your work.", "updates")
            if notes["action"] or notes["updates"]:
                st.button("Mark all as read", key="notif_mark_read", width='stretch', on_click=_mark_all_read)
            return

        st.caption("Everyone else's notifications, one person at a time — 🆕 means new since *their* last visit.")
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
                                          "↩️" if undo else "↪️")
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
                                          "↩️" if undo else "↪️")
        else:
            st.session_state["_toast"] = (
                f"Can't {'undo' if undo else 'redo'} \"{e['description']}\" on **{e['label']}** — "
                f"{result.get('why') or 'that group has changed since'}, so {'undoing' if undo else 'redoing'} it would overwrite newer work. "
                f"Nothing was changed; that step is skipped{' and your next Undo goes to the one before it' if undo else ''}.",
                "⚠️",
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
            notes = load_notifications(name, _notif_since())
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
                "↶ Undo", key="topbar_undo", on_click=_topbar_step, args=("undo",),
                help=(f"Undo: {describe(u)}" if u else "Nothing of yours to undo")
                + ". Department Review actions and grid work — not Merge pushes or anything already pushed live.",
            )
            st.button(
                "↷ Redo", key="topbar_redo", on_click=_topbar_step, args=("redo",),
                help=(f"Redo: {describe(r)}" if r else "Nothing to redo")
                + ". Only if nobody has changed that group since your undo.",
            )


def render_account_box() -> None:
    """Who's signed in, and Logout — top of the sidebar, apart from the
    working buttons so it isn't clicked by accident."""
    with st.sidebar:
        with st.container(border=True, key="account_box"):
            c1, c2 = st.columns([1.35, 1], vertical_alignment="center")
            c1.markdown(f"👤 **{st.session_state['name']}** · :gray[{user_role}]")
            with c2:
                authenticator.logout("Logout", "main", key="logout_btn", callback=_on_logout)
    if st.session_state.pop("_logged_out", False):
        st.rerun()  # straight to the login screen, not the rest of this page


load_workspace()
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
st.markdown(
    """
    <style>
    /* Tab look for the section selectors only — not radios inside a popup. */
    div[role="radiogroup"]:not([role="dialog"] *) {
        gap: 0;
        border-bottom: 1px solid rgba(250, 250, 250, 0.2);
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"] {
        margin: 0;
        cursor: pointer;
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"] > div {
        padding: 8px 16px 10px 16px;
        margin-bottom: -1px;
        border-bottom: 2px solid transparent;
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"] > div > div > div:first-child {
        display: none;
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"] p {
        margin: 0;
        font-size: 14px;
        color: rgba(250, 250, 250, 0.6);
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"]:hover p {
        color: rgba(250, 250, 250, 0.9);
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"][data-selected="true"] > div {
        border-bottom: 2px solid #ff4b4b;
    }
    div[role="radiogroup"]:not([role="dialog"] *) label[data-testid="stRadioOption"][data-selected="true"] p {
        color: #ff4b4b;
        font-weight: 600;
    }
    /* The "N item(s) — page X of Y" caption sits beside a labeled
       selectbox/number_input pair (Rows per page / Page) with no label of
       its own. The column stretches to match that control's full height,
       but the leftover space actually lives on the plain (unkeyed)
       stVerticalBlock Streamlit inserts as the column's direct child, not
       on the column itself — that's the level that needs justify-content:
       flex-end. Bottom-anchoring (rather than centering in the full
       column) lands the caption against the input box itself regardless
       of the label's height, then the small lift centers it on that box. */
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-im_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-manual_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-deleted_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-broken_out_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-decided_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-dept_review_review_page_caption),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-dept_review_unmatched_page_caption) {
        justify-content: flex-end !important;
    }
    div.st-key-im_page_caption,
    div.st-key-manual_page_caption,
    div.st-key-deleted_page_caption,
    div.st-key-broken_out_page_caption,
    div.st-key-decided_page_caption,
    div.st-key-dept_review_review_page_caption,
    div.st-key-dept_review_unmatched_page_caption {
        margin-bottom: 9px;
    }
    /* Same bottom-anchor-then-lift trick as the page-caption rule above,
       applied to "Descending"/"Lock filters" — each sits beside a widget
       taller than its own checkbox+label row (a labeled "Sort by"
       selectbox, or a label-collapsed search box that still reserves its
       label's space), so a fixed top-padding guess would need a
       different value for each row. Bottom-anchoring self-adjusts to
       whatever the sibling's real height is instead. */
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div.st-key-dept_sort_desc_wrap),
    div[data-testid="stColumn"] > div[data-testid="stVerticalBlock"]:has(div[class*="_lock_wrap"]) {
        justify-content: flex-end !important;
    }
    div.st-key-dept_sort_desc_wrap,
    div[class*="_lock_wrap"] {
        margin-bottom: 9px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
nav_request = st.session_state.pop("_nav_to_tab", None)
if nav_request and nav_request in tab_names:
    st.session_state["active_tab"] = nav_request
# The page you're on lives in the address (?tab=…&sub=…, plus its filters
# and page, and the Broken Out group you're working in), so a reload or a
# shared link opens the same place. Still one page, so switching stays instant.
DEPT_PAGE_KEYS = {"Crosswalk": "dept_review_page_num_review", "Unmatched": "dept_review_page_num_unmatched",
                  "Broken Out": "broken_out_page_num", "Decided": "decided_page_num"}
IM_PAGE_SIZES = [100, 250, 500, 1000, 2500, 5000]
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
    st.toast(_toast[0], icon=_toast[1])

# ---------------------------------------------------------------------------
# Item Master (Browse & Edit)
# ---------------------------------------------------------------------------
if active_tab == "Item Master":
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
        "✏️ " + df.loc[has_manual_edit, "ManuallyEditedBy"].astype(str) + " (" + when_str.loc[has_manual_edit] + ")"
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
            out = out[out["Department"] == current_dept]
        if exclude_col != "Brand" and current_brand != "All":
            out = out[out["Brand"] == current_brand]
        if exclude_col != "SourceKey" and current_source != "All":
            out = out[out["SourceKey"] == current_source]
        return out

    departments = ["All"] + sorted(_narrow(df, "Department")["Department"].dropna().unique().tolist())
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
    search = col4.text_input("Search description or UPC", key=search_key)
    manual_only = st.checkbox("✏️ Show only manually-edited items", key="im_manual_only")

    filtered = df.copy()
    if dept_filter != "All":
        filtered = filtered[filtered["Department"] == dept_filter]
    if brand_filter != "All":
        filtered = filtered[filtered["Brand"] == brand_filter]
    if source_filter != "All":
        filtered = filtered[filtered["SourceKey"] == source_filter]
    if manual_only:
        filtered = filtered[filtered["ManuallyEditedBy"].notna()]
    if search:
        s = search.lower()
        filtered = filtered[
            filtered["Description"].str.lower().str.contains(s, na=False)
            | filtered["UPC"].str.contains(s, na=False)
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
        st.caption(f"{matched_count} matching items ({len(df)} total) — page {page_num} of {total_pages}")

    start = (page_num - 1) * page_size
    page_df = filtered.iloc[start:start + page_size]

    if is_admin:
        st.caption(
            "Edit cells directly, then click Stage Changes — they'll show up on the Pending "
            "Changes tab for every editor immediately; Push there actually applies them. Only this "
            "page's rows are staged."
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
                f"⚠️ {len(pending_conflicts)} of your edited row(s) already have a manual "
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

if is_reviewer:
    # Everything from here through Snapshots is only ever reachable when
    # active_tab actually equals one of these names — and tab_names above
    # only ever includes the admin-only ones (Sources, Pending Changes,
    # Upload & Ingest, Merge, Snapshots) for is_admin. A plain "editor"
    # role can select Department Review/Add Item/Delete Item/UPC Overrides
    # here but can never make active_tab become one of the admin-only
    # values in the first place, so no extra is_admin check is needed
    # inside those specific tab bodies below.
    # -------------------------------------------------------------------
    # Department Review — Crosswalk / Unmatched
    # -------------------------------------------------------------------
    if active_tab == "Department Review":
        st.subheader("Department Review")
        render_merge_staleness_banner()
        st.caption(
            "Every month's new raw data gets auto-decided as much as possible. What's left here "
            "genuinely needs a person: Crosswalk has some evidence but not enough to trust "
            "automatically; Unmatched has none at all; Broken Out is a group being decided item by "
            "item; Decided shows everything already resolved, automatically or by hand."
        )
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
        review_subtabs = ["Crosswalk", "Unmatched", "Broken Out", "Pending Changes", "Decided", "Settings"]
        if "dept_review_subtab" not in st.session_state and st.query_params.get("sub") in review_subtabs:
            st.session_state["dept_review_subtab"] = st.query_params["sub"]
        review_subtab = st.radio(
            "Department Review section", review_subtabs,
            horizontal=True, label_visibility="collapsed", key="dept_review_subtab",
        )

        if total_pending_upcs and review_subtab != "Pending Changes":
            st.caption(f"\U0001f4cb {total_pending_upcs:,} item(s) staged, not yet pushed to the database — see Pending Changes.")

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

        if review_subtab == "Settings":
            if is_admin:
                render_old_workbook_import()
            if not is_admin:
                st.info("Only admins can change these settings — shown here read-only so you can see how groups get decided.")
            st.markdown("#### Strict Departments")
            st.caption(
                "A Strict Department NEVER auto-decides, no matter how strong its evidence looks — it "
                "always shows up in Crosswalk or Unmatched for a person to confirm, every single run. "
                "Use this for a raw Department text you know is too broad or unreliable to trust blindly "
                "(e.g. a distributor's generic \"Bulk\" or \"Specialty\" bucket that quietly mixes very "
                "different real Departments). \"Trust Direct Evidence?\" is the one exception: if checked, "
                "a UPC winning DIRECT evidence (this exact item also sold under Scan Advantage with a real "
                "Department) can still auto-decide — only Chained/Category Match/Brand Match evidence stays "
                "blocked. Source = \"any\" applies to that Department text regardless of which distributor "
                "carries it; an exact-source row always wins over an \"any\" row for the same text."
            )
            strict_df = load_strict_departments()
            sources_for_strict = ["any"] + sorted(load_sources()["source_key"].tolist())
            edited_strict = st.data_editor(
                strict_df,
                key="strict_departments_editor",
                width='stretch',
                hide_index=True,
                num_rows="dynamic" if is_admin else "fixed",
                disabled=not is_admin,
                column_config={
                    "source_key": st.column_config.SelectboxColumn("Source", options=sources_for_strict, required=True),
                    "old_department": st.column_config.TextColumn("Old Department (exact text)", required=True),
                    "trust_direct_evidence": st.column_config.CheckboxColumn("Trust Direct Evidence?", default=False),
                },
            )
            if is_admin and st.button("Save Strict Departments", type="primary"):
                rows = edited_strict.dropna(subset=["source_key", "old_department"])
                rows = rows[rows["old_department"].astype(str).str.strip() != ""]
                with db_begin() as conn:
                    conn.execute(text("DELETE FROM dbo.dept_mapping_strict_departments"))
                    if not rows.empty:
                        conn.execute(
                            text(
                                "INSERT INTO dbo.dept_mapping_strict_departments "
                                "(source_key, old_department, trust_direct_evidence, updated_by) "
                                "VALUES (:source_key, :old_department, :trust_direct_evidence, :updated_by)"
                            ),
                            [
                                {
                                    "source_key": r["source_key"],
                                    "old_department": str(r["old_department"]).strip(),
                                    "trust_direct_evidence": bool(r["trust_direct_evidence"]),
                                    "updated_by": st.session_state["name"],
                                }
                                for _, r in rows.iterrows()
                            ],
                        )
                load_strict_departments.clear()
                apply_settings_change("Strict Departments")

            st.divider()
            st.markdown("#### Departments")
            st.caption(
                "The Department choices offered everywhere in Department Review. Scan Advantage's own "
                "current Departments are added automatically every time the engine runs (\"auto\" below) "
                "and can't be removed here — add a Department below only if you need a genuinely new one "
                "Scan Advantage doesn't carry yet (e.g. a new catch-all like BULK)."
            )
            departments_df = load_departments()
            auto_count = int((departments_df["source_type"] == "auto").sum())
            manual_count = len(departments_df) - auto_count
            st.caption(f"{auto_count} from Scan Advantage's own data, {manual_count} added manually.")

            add_col1, add_col2 = st.columns([3, 1]) if is_admin else (None, None)
            new_dept = add_col1 and add_col1.text_input(
                "Add a new Department", key="new_department_input", label_visibility="collapsed",
                placeholder="Add a new Department (e.g. BULK)",
            )
            if add_col2 and add_col2.button("Add", key="add_department_btn", width='stretch'):
                if new_dept.strip():
                    dept_mapping.add_department(ENGINE, new_dept)
                    load_departments.clear()
                    st.rerun()

            st.dataframe(
                departments_df.rename(columns={"department": "Department", "source_type": "Source"}),
                hide_index=True, width='stretch',
            )

            manual_depts = departments_df[departments_df["source_type"] == "manual"]["department"].tolist()
            if manual_depts and is_admin:
                rem_col1, rem_col2 = st.columns([3, 1])
                to_remove = rem_col1.selectbox(
                    "Remove a manually-added Department", manual_depts,
                    key="remove_department_select", label_visibility="collapsed",
                )
                if rem_col2.button("Remove", key="remove_department_btn", width='stretch'):
                    dept_mapping.remove_department(ENGINE, to_remove)
                    load_departments.clear()
                    st.rerun()

            st.divider()
            st.markdown("#### Unmatched Department Defaults")
            st.caption(
                "A starting suggestion for an Unmatched group with NO evidence at all to go on — keyed "
                "off the exact raw Department text a distributor uses. Source = \"any\" applies "
                "regardless of distributor; an exact-source row wins over an \"any\" row for the same text."
            )
            defaults_df = load_unmatched_defaults()
            sources_for_defaults = ["any"] + sorted(load_sources()["source_key"].tolist())
            department_choices = sorted(load_departments()["department"].tolist())
            edited_defaults = st.data_editor(
                defaults_df,
                key="unmatched_defaults_editor",
                width='stretch',
                hide_index=True,
                num_rows="dynamic" if is_admin else "fixed",
                disabled=not is_admin,
                column_config={
                    "source_key": st.column_config.SelectboxColumn("Source", options=sources_for_defaults, required=True),
                    "old_department": st.column_config.TextColumn("Old Department (exact text)", required=True),
                    "new_department": st.column_config.SelectboxColumn("Default Department", options=department_choices, required=True),
                },
            )
            if is_admin and st.button("Save Unmatched Defaults", type="primary"):
                rows = edited_defaults.dropna(subset=["source_key", "old_department", "new_department"])
                rows = rows[rows["old_department"].astype(str).str.strip() != ""]
                dept_mapping.save_unmatched_defaults(
                    ENGINE,
                    [
                        {
                            "source_key": r["source_key"],
                            "old_department": str(r["old_department"]).strip(),
                            "new_department": r["new_department"],
                        }
                        for _, r in rows.iterrows()
                    ],
                    st.session_state["name"],
                )
                load_unmatched_defaults.clear()
                apply_settings_change("Unmatched Defaults")

        elif review_subtab == "Pending Changes":
            # Must run BEFORE the "confirm_push_dept_changes" checkbox widget
            # is instantiated below — st.session_state can't reassign an
            # already-instantiated widget's own key (StreamlitWidgetAlready
            # InstantiatedError), which is exactly what a direct
            # `st.session_state["confirm_push_dept_changes"] = False` inside
            # the button handler below used to crash on, every single push,
            # right after the database write had already gone through (the
            # push itself always succeeded — only this reset crashed after).
            if st.session_state.pop("_reset_confirm_push", False):
                st.session_state["confirm_push_dept_changes"] = False
            render_discard_notices("dept_review")
            st.caption(
                "A department decision — Approve on Crosswalk/Unmatched, or setting an item's "
                "Department on Broken Out — lands here immediately and is visible to every other "
                "editor right away; Push actually applies it to the database. Break Out and Send Back "
                "aren't decisions (nothing gets decided by clicking them), so they apply right away too "
                "— but they're still one-click undoable below under Recent moves, no push needed either way."
            )
            pc_search = render_search_bar("pending_changes", "Search Pending Changes", "Filter by source or group label…")

            def _pc_matches(source_key, label):
                if not pc_search:
                    return True
                s = pc_search.lower()
                return s in (source_key or "").lower() or s in (label or "").lower()

            actor = st.session_state["name"]
            pc_department_options = load_departments()["department"].tolist()
            visible_recent_moves = [m for m in recent_moves if _pc_matches(m["source_key"], m["label"])]
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
                st.info("Nothing staged yet — Approve a Crosswalk/Unmatched group or decide a Broken Out item to see changes here.")
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
                    with st.container(border=True):
                        c0, c1, c2, c3, *c_admin = st.columns([0.4, 3.6, 2, 1, 0.5] if is_admin else [0.4, 3.6, 2, 1])
                        c0.checkbox(
                            "Include", value=True, key=f"dept_pending_include_combo_{combo_id}",
                            label_visibility="collapsed",
                            help="Included in the next push — uncheck to save this one for later.",
                        )
                        c1.markdown(f"**{change['source_key'].upper()}** — {change['label']}")
                        c2.markdown(f"Approve as **{change['department']}**")
                        is_primary_stager = actor == change.get("staged_by")
                        if c3.button("Undo…", key=f"undo_pending_{combo_id}", width='stretch'):
                            open_undo_picker(combo_id, f"{change['source_key'].upper()} — {change['label']}")
                        if c_admin:
                            admin_override_button(c_admin[0], {
                                "kind": "combo", "combo_id": combo_id, "tier": change.get("tier"),
                                "source_key": change["source_key"], "raw_label": change["label"],
                                "n_upcs_total": change["n_upcs_total"],
                                "label": f"{change['source_key'].upper()} — {change['label']}",
                            }, key=f"admin_override_btn_ready_{combo_id}")
                        requested = combo_undo_requests.get(combo_id, set())
                        if requested and not is_primary_stager:
                            c1.caption(f"↩️ Undo requested by {', '.join(sorted(requested))} — waiting on **{change.get('staged_by') or 'the stager'}**.")
                        when_str = pd.to_datetime(change.get("staged_at"), errors="coerce")
                        when_str = when_str.strftime("%Y-%m-%d %H:%M") if pd.notna(when_str) else ""
                        if change.get("overridden_by"):
                            agreed_bit = f" — **admin override by {change['overridden_by']}**"
                        elif change.get("agreed_by"):
                            agreed_bit = f" — agreed by **{change['agreed_by']}**"
                        else:
                            agreed_bit = ""
                        c1.caption(f"Staged by **{change.get('staged_by') or 'unknown'}**" + (f" on {when_str}" if when_str else "") + agreed_bit)

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
                                    "🚫",
                                )
                            elif result["disputed"]:
                                st.session_state["_toast"] = (
                                    f"**{change['label']}** now has more than one suggested department "
                                    f"({', '.join(result['departments'])}) — see the Needs Agreement section below.",
                                    "🗳️",
                                )
                            else:
                                st.session_state["_toast"] = (f"{toast_verb} **{change['label']}** → {new_dept}.", "✅")
                            st.session_state[f"_reset_suggest_{combo_id}"] = True
                            st.rerun()

                        is_locked = bool(change.get("overridden_by"))
                        if is_locked and not is_admin:
                            st.caption(
                                f"🔒 Locked by **{change['overridden_by']}**'s admin override — only an admin "
                                "can change this until they remove it."
                            )
                        elif not is_locked:
                            # "I also agree" + the suggest/update dropdown share
                            # one row (instead of a row each) — with hundreds of
                            # these on screen, every row saved matters.
                            show_agree = actor not in combo_backers.get(combo_id, set())
                            row_cols = st.columns([1.3, 2.4, 1]) if show_agree else st.columns([3, 1])
                            agree_col, scol1, scol2 = row_cols if show_agree else (None, row_cols[0], row_cols[1])
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
                                    st.session_state["_toast"] = (f"Recorded your agreement on **{change['label']}**.", "✅")
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
                    with st.container(border=True):
                        c0, c1, c2, c3, *c_admin = st.columns([0.4, 3.6, 2, 1, 0.5] if is_admin else [0.4, 3.6, 2, 1])
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
                        c1.markdown(f"**{first.get('source_key', '').upper()}** — {first['label']}")
                        c2.markdown(f"{len(items)} item(s) staged" + (f", {len(group_suggestions)} awaiting agreement" if group_suggestions else ""))
                        primary = upc_group_primaries.get(combo_id)
                        is_primary_editor = actor == primary
                        if c3.button("Undo…", key=f"undo_pending_upc_combo_{combo_id}", width='stretch'):
                            open_undo_picker(combo_id, f"{first.get('source_key', '').upper()} — {first['label']}")
                        if c_admin:
                            admin_override_button(c_admin[0], {
                                "kind": "upc_group", "combo_id": combo_id, "raw_label": first["label"],
                                "label": f"{first.get('source_key', '').upper()} — {first['label']}",
                            }, key=f"admin_override_btn_group_{mode}_{combo_id}")
                        requested = upc_group_undo_requests.get(combo_id, set())
                        if requested and not is_primary_editor:
                            c1.caption(f"↩️ Undo requested by {', '.join(sorted(requested))} — waiting on **{primary or 'the first editor'}**.")

                        if group_suggestions:
                            render_upc_change_suggestions(group_suggestions, pending_upc_changes, combo_id)

                        with st.expander(f"Show staged items ({len(items)})", key=f"pending_upc_expander_{combo_id}"):
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
                                st.session_state["_toast"] = ((", ".join(msg_bits) or "Nothing changed") + ".", "🗳️")
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
                st.warning(
                    f"Pushing applies the {included_item_count:,} included item(s) to the live database immediately "
                    f"— this affects the real Merge output, not a preview. The other "
                    f"{total_pending_upcs - included_item_count:,} unincluded item(s) stay staged, untouched. "
                    "Review everything below first."
                )
                dept_push_approvals = dept_mapping.get_dept_push_approvals(ENGINE)
                dept_distinct_approvers = sorted({a["approver"] for a in dept_push_approvals})
                dept_required = dept_mapping.DEPT_PUSH_REQUIRED_APPROVALS
                if is_admin:
                    st.caption("You're an admin, so you can push this batch on your own.")
                elif dept_distinct_approvers:
                    st.caption(f"✓ Approved by: {', '.join(dept_distinct_approvers)} ({len(dept_distinct_approvers)} of {dept_required} needed)")
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
                confirm_push = st.checkbox(
                    "I've reviewed these changes and I'm ready to update the database.", key="confirm_push_dept_changes",
                )
                if st.button(
                    f"Push {included_item_count:,} Included Item(s) to the Database", type="primary",
                    key="push_pending_changes", disabled=not (confirm_push and dept_enough_approvals) or not included_item_count,
                ):
                    included_upcs = [upc for cid in included_upc_combo_ids for upc, _ in visible_by_combo[cid]]
                    included_pending_changes = {cid: pending_changes[cid] for cid in included_combo_ids}
                    included_pending_upc_changes = {upc: pending_upc_changes[upc] for upc in included_upcs}
                    touched_combo_ids = set(included_pending_changes.keys())
                    for combo_id, change in included_pending_changes.items():
                        dept_mapping.approve_combo(ENGINE, combo_id, change["department"], change.get("staged_by") or actor, pushed_by=actor)
                        dept_mapping.delete_pending_change(ENGINE, combo_id)
                        st.session_state.pop(f"dept_pending_include_combo_{combo_id}", None)
                    if included_pending_upc_changes:
                        touched_combo_ids.update(c["combo_id"] for c in included_pending_upc_changes.values())
                        dept_mapping.apply_upc_decisions(
                            ENGINE,
                            {upc: {"department": c["department"], "staged_by": c.get("staged_by")} for upc, c in included_pending_upc_changes.items()},
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
                        "department(s) changed.", "✅",
                    )
                    st.rerun()

                st.divider()
                st.markdown("#### Ready to push")
                if not (included_combo_ids or included_upc_combo_ids):
                    st.caption("Nothing currently included — check \"Include\" on a Saved for Later item below, or resolve a Needs Agreement one.")
                else:
                    st.caption(
                        "Uncheck \"Include\" to save one for later. Agree or suggest a different department "
                        "below each — a different suggestion sends it back to Needs Agreement. Admins can override."
                    )
                    for combo_id in included_combo_ids:
                        _render_group_row(combo_id, visible_pending_changes[combo_id])
                    for combo_id in included_upc_combo_ids:
                        _render_upc_group_row(combo_id, visible_by_combo[combo_id])

                if excluded_combo_ids or excluded_upc_combo_ids or snoozed_combo_disputes or snoozed_needs_agreement_upc_combo_ids:
                    st.divider()
                    st.markdown("#### Saved for later")
                    st.caption("Not part of the push above — check \"Include\" to move one back up when it's ready.")
                    for combo_id in excluded_combo_ids:
                        _render_group_row(combo_id, visible_pending_changes[combo_id])
                    for combo_id in excluded_upc_combo_ids:
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
                st.caption(
                    "Only resolves once the people who actually disagree change their own vote to match — "
                    "agreeing just adds your name, it never forces a decision. No vote yet? Suggest your own "
                    "below. Admins can override. A Broken Out group with any pending suggestion sits here "
                    "in full — it can't be pushed until every suggestion in it is accepted or denied. Not "
                    "ready to deal with one right now? Check \"Save for later\" to move it down without "
                    "resolving it."
                )
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
                st.caption(
                    "Someone asked for one of these to be undone, but only the person who staged it (or, for "
                    "a Broken Out group, whoever first worked on it) — or an admin — can actually undo it."
                )
                for combo_id in undo_requested_combos:
                    change = visible_pending_changes[combo_id]
                    requested = combo_undo_requests.get(combo_id, set())
                    with st.container(border=True):
                        rc1, rc2 = st.columns([4, 1.4])
                        rc1.markdown(f"**{change['source_key'].upper()}** — {change['label']}")
                        rc1.caption(f"Requested by {', '.join(sorted(requested))} — waiting on **{change.get('staged_by') or 'the stager'}**.")
                        if actor == change.get("staged_by") or is_admin:
                            if rc2.button("Undo…", key=f"undo_requested_combo_{combo_id}", width='stretch', type="primary"):
                                open_undo_picker(combo_id, f"{change['source_key'].upper()} — {change['label']}")
                for combo_id in undo_requested_upc_groups:
                    items = visible_by_combo[combo_id]
                    first = items[0][1]
                    requested = upc_group_undo_requests.get(combo_id, set())
                    primary = upc_group_primaries.get(combo_id)
                    with st.container(border=True):
                        rc1, rc2 = st.columns([4, 1.4])
                        rc1.markdown(f"**{first.get('source_key', '').upper()}** — {first['label']}")
                        rc1.caption(f"Requested by {', '.join(sorted(requested))} — waiting on **{primary or 'the first editor'}**.")
                        if actor == primary or is_admin:
                            if rc2.button("Undo…", key=f"undo_requested_upc_group_{combo_id}", width='stretch', type="primary"):
                                open_undo_picker(combo_id, f"{first.get('source_key', '').upper()} — {first['label']}")

            # ---- Recent moves (structural Break Out/Send Back undo — kept
            # last, since it's the least time-critical, most historical
            # section). ----
            if visible_recent_moves:
                st.divider()
                st.markdown("#### Recent moves")
                st.caption(
                    "Break Out / Send Back actions from the other tabs — applied immediately. Undo… lets you "
                    "pick how far back to go when a group has moved more than once."
                )
                moves_by_combo = {}
                for move in visible_recent_moves:
                    moves_by_combo.setdefault(move["combo_id"], []).append(move)
                for combo_id, moves in moves_by_combo.items():
                    latest = moves[0]
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([4, 2, 1])
                        c1.markdown(f"**{latest['source_key'].upper()}** — {latest['label']}")
                        c2.markdown(
                            " → ".join(m["description"] for m in reversed(moves))
                            + f" — {latest['n_upcs_total']:,} item(s)"
                        )
                        if c3.button("Undo…", key=f"undo_recent_{combo_id}", width='stretch'):
                            open_undo_picker(combo_id, f"{latest['source_key'].upper()} — {latest['label']}")

        elif review_subtab == "Broken Out":
            st.caption(
                "A group here couldn't be trusted as a whole — its items get decided one at a time "
                "instead. Pick a Department for as many as you're ready to and stage them; a group "
                "moves to Decided automatically once every one of its items has a real decision. Not "
                "sure about the whole group anymore? Send it back to combo-level review instead — "
                "that discards any item decisions made here so far."
            )
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
                st.caption(
                    f"{len(broken_df)} group(s) broken out, {int(broken_df['pending_count'].sum()):,} "
                    "item(s) still need a decision."
                )

                search, sort_column, sort_desc = render_filter_bar(
                    "broken_out",
                    {
                        "# Items": "n_upcs_total", "Source": "source_key", "Old Department": "raw_department",
                        "Category": "raw_category", "Subcategory": "raw_subcategory",
                        "Items Left": "pending_count",
                    },
                    "Search Broken Out Groups", "Filter by source, department, category, or subcategory…",
                )
                filtered = broken_df
                if search:
                    s = search.lower()
                    mask = (
                        filtered["source_key"].str.lower().str.contains(s, na=False)
                        | filtered["raw_department"].str.lower().str.contains(s, na=False)
                        | filtered["raw_category"].str.lower().str.contains(s, na=False)
                        | filtered["raw_subcategory"].str.lower().str.contains(s, na=False)
                        | combo_label_series(filtered).str.lower().str.contains(s, regex=False)
                    )
                    filtered = filtered[mask]
                filtered = sort_full_df(filtered, sort_column, sort_desc)

                page_num_key = "broken_out_page_num"
                page_size, page_num, total_pages = render_page_controls(
                    "broken_out", page_num_key, len(filtered), f"{len(filtered)} matching group(s)",
                )

                start = (page_num - 1) * page_size
                page_df = filtered.iloc[start:start + page_size]
                department_options = load_departments()["department"].tolist()
                actor = st.session_state["name"]
                page_combo_ids = [int(r["combo_id"]) for _, r in page_df.iterrows()]
                claims = dept_mapping.get_broken_out_claims(ENGINE, page_combo_ids)
                mine = [c for c in page_combo_ids if (claims.get(c) or {}).get("claimed_by") == actor]
                st.session_state["_url_group_now"] = mine[0] if mine else None
                upc_change_suggestions = load_upc_change_suggestions()

                for _, row in page_df.iterrows():
                    combo_id = int(row["combo_id"])
                    label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
                    label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
                    staged_here = sum(1 for c in pending_upc_changes.values() if c["combo_id"] == combo_id)
                    pending_sugg_here = sum(1 for s in upc_change_suggestions.values() if s[0]["combo_id"] == combo_id)
                    send_back_label = "Send Back to Crosswalk" if row["tier"] == "review" else "Send Back to Unmatched"
                    claim = claims.get(combo_id)
                    with st.container(border=True):
                        top1, top2, top3 = st.columns([3, 1, 1.4])
                        top1.markdown(f"**{row['source_key'].upper()}** — {label}")
                        top2.markdown(
                            f"<div style='text-align:right;color:gray;padding-top:4px;'>"
                            f"{int(row['decided_count'])} of {int(row['override_count'])} decided</div>",
                            unsafe_allow_html=True,
                        )
                        if top3.button(send_back_label, key=f"revert_broken_out_{combo_id}", width='stretch'):
                            # Not a department decision — discards any
                            # item-level work and reopens the combo for a
                            # fresh combo-level decision, so it applies
                            # immediately rather than through Pending
                            # Changes (same reasoning as Break Out above).
                            # Still undoable from "Recent moves" — and if
                            # this combo already has real decisions on it
                            # (auto, manual, or staged), request_send_back
                            # stops for a confirmation first instead of
                            # silently discarding them.
                            request_send_back(combo_id, row["source_key"], label, int(row["n_upcs_total"]), send_back_label, is_whole=False)
                        caption_bits = []
                        if staged_here:
                            caption_bits.append(f"{staged_here} item(s) staged here, not yet pushed")
                        if pending_sugg_here:
                            caption_bits.append(f"{pending_sugg_here} pending suggestion(s) — see Pending Changes")
                        if caption_bits:
                            st.caption(" · ".join(caption_bits) + ".")

                        # Claiming gates who may bulk-decide this combo's still-
                        # UNDECIDED items — a temporary mutex, never where a
                        # decision lives (see dept_mapping.get_broken_out_claim).
                        # It does NOT grant authority over items someone else
                        # already owns; those still only take suggestions,
                        # from Pending Changes.
                        if claim and claim["claimed_by"] != actor:
                            claimed_when = pd.to_datetime(claim["claimed_at"], errors="coerce")
                            claimed_str = claimed_when.strftime("%Y-%m-%d %H:%M") if pd.notna(claimed_when) else ""
                            cc1, cc2 = st.columns([3, 1])
                            cc1.info(f"🔒 Claimed by **{claim['claimed_by']}**" + (f" since {claimed_str}" if claimed_str else "") + " — read-only until they release it.")
                            if is_admin and cc2.button("Force release", key=f"force_release_claim_{combo_id}", width='stretch'):
                                dept_mapping.release_broken_out_claim(ENGINE, combo_id, actor, is_admin=True)
                                load_broken_out_claims.clear()
                                st.session_state["_toast"] = (f"Force-released the claim on **{label}**.", "🔓")
                                st.rerun()
                            continue

                        if not claim:
                            if st.button("Claim this group to review its items", key=f"claim_broken_out_{combo_id}"):
                                with st.spinner("Claiming the group and loading its items…"):
                                    result = dept_mapping.claim_broken_out_group(ENGINE, combo_id, actor)
                                load_broken_out_claims.clear()
                                if not result["claimed"]:
                                    st.session_state["_toast"] = (f"{result['claimed_by']} claimed this just before you did — try again in a bit.", "🔒")
                                st.rerun()
                            continue

                        # claim["claimed_by"] == actor from here on.
                        dept_mapping.touch_broken_out_claim(ENGINE, combo_id, actor)
                        rc1, rc2 = st.columns([3, 1])
                        rc1.caption("🔓 You've claimed this group — only you can bulk-decide its remaining items until you release it.")
                        if rc2.button("Release claim", key=f"release_claim_{combo_id}", width='stretch'):
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
                            with st.spinner(f"Staging {len(decisions):,} item(s)..."):
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
                            st.session_state["_toast"] = (msg.strip(), "🗳️" if (suggested_n or blocked_n) else "📋")
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

        elif review_subtab == "Decided":
            st.caption(
                "Every group that's actually FINISHED — a group still being decided item by item lives "
                "on the Broken Out tab instead, even if it's mostly done, until its very last item is "
                "decided. Status shows exactly how: Whole Group (Auto/Manual), or graduated from Broken "
                "Out (Fully Auto, Partially Auto, or Manually Decided). Any row can be sent back to where "
                "it came from — a Whole Group row to the Crosswalk/Unmatched queue it was decided out of; "
                "a Broken Out row back to Broken Out to decide its items again (with or without "
                "auto-matching). To change something without sending it back, edit it here — that goes "
                "through Pending Changes."
            )
            decided_df = load_decided_combos()
            decided_df = decided_df[~decided_df["combo_id"].isin(pending_changes.keys())]
            if decided_df.empty:
                st.info("Nothing decided yet.")
            else:
                whole_count = int(decided_df["status"].str.startswith("Whole Group").sum())
                broken_count = len(decided_df) - whole_count
                st.caption(
                    f"{len(decided_df)} group(s) total — {whole_count} decided as a whole, "
                    f"{broken_count} Broken Out — {int(decided_df['n_upcs_total'].sum()):,} item(s) total."
                )

                search, sort_column, sort_desc = render_filter_bar(
                    "decided",
                    {
                        "# Items": "n_upcs_total", "Source": "source_key", "Old Department": "raw_department",
                        "Category": "raw_category", "Subcategory": "raw_subcategory", "Status": "status",
                        "Decided Department": "decided_department",
                    },
                    "Search Decided Groups",
                    "Filter by source, department, category, subcategory, decided department, or status…",
                )
                filtered = decided_df
                if search:
                    s = search.lower()
                    mask = (
                        filtered["source_key"].str.lower().str.contains(s, na=False)
                        | filtered["raw_department"].str.lower().str.contains(s, na=False)
                        | filtered["raw_category"].str.lower().str.contains(s, na=False)
                        | filtered["raw_subcategory"].str.lower().str.contains(s, na=False)
                        | combo_label_series(filtered).str.lower().str.contains(s, regex=False)
                        | filtered["decided_department"].fillna("").str.lower().str.contains(s, na=False)
                        | filtered["status"].str.lower().str.contains(s, na=False)
                    )
                    filtered = filtered[mask]
                filtered = sort_full_df(filtered, sort_column, sort_desc)

                page_num_key = "decided_page_num"
                page_size, page_num, total_pages = render_page_controls(
                    "decided", page_num_key, len(filtered), f"{len(filtered)} matching group(s)",
                )

                start = (page_num - 1) * page_size
                page_df = filtered.iloc[start:start + page_size]
                actor = st.session_state["name"]
                department_options = load_departments()["department"].tolist()

                for _, row in page_df.iterrows():
                    combo_id = int(row["combo_id"])
                    label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
                    label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
                    is_whole = row["status"].startswith("Whole Group")
                    with st.container(border=True):
                        if is_whole:
                            top1, top2, top3 = st.columns([3, 2.4, 1.4])
                        else:
                            top1, top2, top3, top4 = st.columns([2.4, 1.9, 1.35, 1.35])
                        top1.markdown(f"**{row['source_key'].upper()}** — {label}")
                        if is_whole:
                            status_line = f"{row['status']} — Decided as <b>{html.escape(str(row['decided_department']))}</b>, {int(row['n_upcs_total']):,} item(s)"
                            if pd.notna(row.get("last_decided_by")):
                                when_str = pd.to_datetime(row["last_decided_at"], errors="coerce")
                                when_str = when_str.strftime("%Y-%m-%d") if pd.notna(when_str) else ""
                                status_line += f" — approved by {row['last_decided_by']}" + (f" on {when_str}" if when_str else "")
                            pushed_by = row.get("pushed_by")
                            if pd.notna(pushed_by) and pushed_by != row.get("last_decided_by"):
                                status_line += f", pushed by {pushed_by}"
                        else:
                            status_line = f"{row['status']} — {int(row['n_upcs_total']):,} item(s), decided individually"
                        top2.markdown(
                            f"<div style='text-align:right;color:gray;padding-top:4px;'>{status_line}</div>",
                            unsafe_allow_html=True,
                        )
                        n_ov = load_override_counts().get(combo_id, 0)
                        if n_ov and is_whole:
                            st.caption(f"🔒 **{n_ov:,} of {int(row['n_upcs_total']):,}** item(s) have a UPC override setting "
                                       "their Department instead of this group's decision.")
                        # Goes back to wherever it came from: a whole group to the
                        # queue it was decided out of (an auto-decided one to the
                        # queue the engine promoted it from — see origin_tier). A
                        # Broken Out group can go back to Broken Out (redo its
                        # items) or all the way to the queue it was broken out of.
                        queue_name = {"review": "Crosswalk", "unmatched": "Unmatched"}.get(
                            dept_mapping.origin_tier(row["tier"], row["n_evidence"]), "Review"
                        )
                        if is_whole:
                            if top3.button(f"Send Back to {queue_name}", key=f"revert_decided_{combo_id}", width='stretch'):
                                request_send_back(
                                    combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                    f"Send Back to {queue_name}", is_whole,
                                )
                        else:
                            if top3.button("↩ Broken Out", key=f"revert_decided_{combo_id}", width='stretch',
                                           help="Send back to Broken Out to decide its items again"):
                                request_break_out(
                                    combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                    reopen=True, fully_auto=row["status"] == "Broken Out — Fully Auto",
                                )
                            if top4.button(f"↩ {queue_name}", key=f"revert_decided_queue_{combo_id}", width='stretch',
                                           help=f"Send back to {queue_name} for a fresh whole-group decision"):
                                request_send_back(
                                    combo_id, row["source_key"], label, int(row["n_upcs_total"]),
                                    f"Send Back to {queue_name}", is_whole,
                                )
                        if is_whole:
                            # Auto and Manual are the only two whole-group decided_via
                            # states (get_decided_combos' own status logic treats
                            # anything that isn't literally "Auto" as Manual) — so
                            # confirming here just needs to flip that one value; the
                            # status label above updates to "— Manual" on its own.
                            if row["status"] == "Whole Group — Auto":
                                if st.button("✓ Mark as reviewed", key=f"confirm_whole_{combo_id}"):
                                    with track(combo_id, f"{row['source_key'].upper()} — {label}", "Marked as reviewed"):
                                        dept_mapping.confirm_combo_decision(ENGINE, combo_id, st.session_state["name"])
                                    load_decided_combos.clear()
                                    st.rerun()
                            with st.expander("Change department", key=f"decided_change_dept_expander_{combo_id}"):
                                st.caption(
                                    "A real correction, not a sign-off — stages a new decision here just "
                                    "like Crosswalk/Unmatched Approve, so it shows up on Pending Changes and "
                                    "needs a Push (undoable there) instead of changing the live data "
                                    "immediately. Leaves this combo's current department in place until "
                                    "then."
                                )
                                dc1, dc2 = st.columns([3, 1.4])
                                new_dept = dc1.selectbox(
                                    "New department", [d for d in department_options if d != row["decided_department"]],
                                    key=f"decided_change_dept_{combo_id}", label_visibility="collapsed",
                                )
                                if dc2.button("Stage change", key=f"decided_change_dept_btn_{combo_id}", width='stretch'):
                                    with track(combo_id, f"{row['source_key'].upper()} — {label}", f"Staged a change to {new_dept}"):
                                        result = dept_mapping.upsert_combo_suggestion(
                                            ENGINE, combo_id, row.get("tier"), new_dept, row["source_key"],
                                            label, int(row["n_upcs_total"]), actor, is_admin=is_admin,
                                        )
                                    clear_dept_suggestion_caches()
                                    if result.get("locked"):
                                        st.session_state["_toast"] = (f"**{label}** is locked by an admin override — ask them to remove it first.", "🔒")
                                    elif not result["disputed"]:
                                        st.session_state["_toast"] = (f"Staged: **{label}** → {new_dept}. See Pending Changes to push.", "📋")
                                    else:
                                        st.session_state["_toast"] = (f"Staged as a suggestion for **{label}** — see Pending Changes.", "📋")
                                    st.rerun()
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
                                            f"✓ Confirm all {len(auto_upcs)} auto-decided item(s) as reviewed",
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
                                    st.caption(
                                        "Fill in **New Department** for any item you want to change — it's staged "
                                        "on Pending Changes to push (and undoable there), not applied immediately."
                                    )
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
                                            "📋",
                                        )
                                        st.rerun()

                render_bottom_pagination("decided_page_size", page_num_key, "decided", total_pages)

        elif review_subtab in ("Crosswalk", "Unmatched"):
            tier = "review" if review_subtab == "Crosswalk" else "unmatched"
            warm_break_out_cache()

            queue_df = load_dept_review_queue(tier)
            combo_suggestions = load_combo_suggestions()
            queue_df = queue_df[~queue_df["combo_id"].isin(set(pending_changes.keys()) | set(combo_suggestions.keys()))]
            department_options = load_departments()["department"].tolist()

            if queue_df.empty:
                st.success(f"Nothing waiting in {review_subtab} — every {review_subtab.lower()} group has already been decided.")
            else:
                total_items = int(queue_df["n_upcs_total"].sum())
                st.caption(f"{len(queue_df)} group(s) waiting on a decision, {total_items:,} item(s) total.")

                search, sort_column, sort_desc = render_filter_bar(
                    f"dept_review_{tier}",
                    {
                        "# Items": "n_upcs_total", "Source": "source_key", "Old Department": "raw_department",
                        "Category": "raw_category", "Subcategory": "raw_subcategory",
                        "New Department": "suggested_department",
                    },
                    f"Search {review_subtab}", "Filter by source, department, category, or subcategory…",
                )
                filtered = queue_df
                if search:
                    s = search.lower()
                    mask = (
                        filtered["source_key"].str.lower().str.contains(s, na=False)
                        | filtered["raw_department"].str.lower().str.contains(s, na=False)
                        | filtered["raw_category"].str.lower().str.contains(s, na=False)
                        | filtered["raw_subcategory"].str.lower().str.contains(s, na=False)
                        | combo_label_series(filtered).str.lower().str.contains(s, regex=False)
                    )
                    filtered = filtered[mask]
                filtered = sort_full_df(filtered, sort_column, sort_desc)

                page_num_key = f"dept_review_page_num_{tier}"
                page_size, page_num, total_pages = render_page_controls(
                    f"dept_review_{tier}", page_num_key, len(filtered), f"{len(filtered)} matching group(s)",
                )

                start = (page_num - 1) * page_size
                page_df = filtered.iloc[start:start + page_size]

                for _, row in page_df.iterrows():
                    combo_id = int(row["combo_id"])
                    label_bits = [b for b in [row["raw_department"], row["raw_category"], row["raw_subcategory"]] if b]
                    label = " / ".join(label_bits) if label_bits else "(blank Department/Category/Subcategory)"
                    with st.container(border=True):
                        top1, top2 = st.columns([4, 1])
                        top1.markdown(f"**{row['source_key'].upper()}** — {label}")
                        top2.markdown(
                            f"<div style='text-align:right;color:gray;padding-top:4px;'>{int(row['n_upcs_total']):,} item(s)</div>",
                            unsafe_allow_html=True,
                        )
                        st.caption(evidence_sentence(row))
                        n_ov = load_override_counts().get(combo_id, 0)
                        if n_ov:
                            every = " (that's every item here)" if n_ov >= int(row["n_upcs_total"]) else ""
                            st.caption(f"🔒 **{n_ov:,} of {int(row['n_upcs_total']):,}** item(s) have a UPC override setting "
                                       f"their Department, so this group's decision won't change those{every}.")

                        dept_col, approve_col, break_col = st.columns([3, 1, 1.6])
                        options_with_blank = [""] + department_options
                        suggested = row["suggested_department"]
                        default_index = options_with_blank.index(suggested) if suggested in options_with_blank else 0
                        chosen_dept = dept_col.selectbox(
                            "Department", options_with_blank, index=default_index,
                            key=f"dept_choice_{tier}_{combo_id}", label_visibility="collapsed",
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
                                        "🗳️",
                                    )
                                else:
                                    st.session_state["_toast"] = (
                                        f"Staged: **{row['source_key'].upper()} — {label}** → {chosen_dept} "
                                        f"({int(row['n_upcs_total']):,} item(s)). See Pending Changes to push it.",
                                        "📋",
                                    )
                                st.rerun()
                        if break_col.button("Break Out to UPC-Level", key=f"breakout_{tier}_{combo_id}", width='stretch'):
                            # Not a department decision — just routes this
                            # group to the item-level queue instead of
                            # combo-level review — so it applies immediately
                            # rather than sitting in Pending Changes. Staging
                            # it (as this used to) meant a group could vanish
                            # from Crosswalk/Unmatched the moment it was
                            # clicked but not actually reach Broken Out until
                            # a separate, easy-to-miss push — exactly the
                            # "can't find where I moved it" confusion this
                            # was rebuilt to avoid. Still undoable with one
                            # click from Pending Changes' "Recent moves", via
                            # the exact-state snapshot taken right before.
                            # request_break_out checks whether any per-UPC
                            # auto-matching could decide something for this
                            # combo right now and, if so, asks which
                            # starting point the human wants.
                            request_break_out(combo_id, row["source_key"], label, int(row["n_upcs_total"]))
                        render_affected_items_expander(combo_id, int(row["n_upcs_total"]), "review_items")

                render_bottom_pagination(f"dept_review_{tier}_page_size", page_num_key, f"dept_review_{tier}", total_pages)

    # -------------------------------------------------------------------
    # Add Item
    # -------------------------------------------------------------------
    if active_tab == "Add Item":
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
            "Items added here by hand (not pulled in from any distributor file) — these always "
            "survive a Merge. Remove one to delete it from the item master entirely; it's kept "
            "under Deleted Items on the Delete Item tab so it can be restored later if needed."
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
                    with db_begin() as conn:
                        conn.execute(text("DELETE FROM dbo.items WHERE upc = :upc"), {"upc": upc_to_remove})
                        conn.execute(text("DELETE FROM dbo.manual_overrides WHERE upc = :upc"), {"upc": upc_to_remove})
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
                                "upc": upc_to_remove,
                                "description": sql_value(remove_row["Description"]),
                                "department": sql_value(remove_row["Department"]),
                                "category": sql_value(remove_row["Category"]),
                                "subcategory": sql_value(remove_row["Subcategory"]),
                                "brand": sql_value(remove_row["Brand"]),
                                "pack": sql_value(remove_row["Pack"]),
                                "size": sql_value(remove_row["Size"]),
                                "uom": sql_value(remove_row["UOM"]),
                                "deleted_by": st.session_state["name"],
                            },
                        )
                    load_items.clear()
                    load_manual_items.clear()
                    load_deleted_items.clear()
                    st.success(f"Removed {upc_to_remove}. It's kept under Deleted Items if you need to restore it.")
                    st.rerun()

    # -------------------------------------------------------------------
    # Delete Item
    # -------------------------------------------------------------------
    if active_tab == "Delete Item":
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
                st.caption(f"Type part of a description or UPC to find it among {len(df)} items.")
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
                    st.warning(
                        f"This stages deleting UPC {upc_to_delete} — visible to every other editor "
                        "immediately, but Push (on the Pending Changes tab) actually removes it from "
                        "dbo.items and remembers it as deliberately deleted so the next Merge doesn't bring "
                        "it back just because a distributor source still lists it. Its data is kept under "
                        "Deleted Items once pushed, so it can be restored later if needed."
                    )
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
    if active_tab == "UPC Overrides":
        st.subheader("Edit one item's attributes")
        st.caption(
            "Search for a single item and edit its fields directly — Department is a dropdown here, "
            "so there's no risk of a typo creating a department that doesn't really exist (unlike the "
            "free-text grid on Item Master). Submitting stages the change; it shows up on Pending "
            "Changes for every editor immediately, and Push there actually applies it."
        )
        item_master_pending = item_master_pending_cross_link()
        render_bulk_upload("edit", "Changing many items (Departments or any other fields)")
        df = load_items()
        df = df[~df["UPC"].isin(item_master_pending.keys())]
        search = st.text_input("Search by description or UPC", key="upc_override_search")
        if not search:
            st.caption(f"Type part of a description or UPC to find it among {len(df)} items.")
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
    if active_tab == "Sources":
        st.subheader("Configured distributor sources")
        st.caption(
            "Replaces script.py's Data Source Definitions.xlsx — add a new distributor here (no code "
            "needed), then upload its file on the Upload & Ingest tab. Edits and new sources are "
            "staged, not applied immediately — see Pending Changes to review, undo, and push them."
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
            "Upload one or more sample files below — for each file, pick which sheet(s) should "
            "become sources (different sheets can be completely different layouts, e.g. a "
            "distributor's own catalog vs. its Food Service items). Analyzing creates one "
            "independent, pre-filled form per (file, sheet) below: column mappings, a guessed "
            "Source Key/Label/File Keyword from the filename, and Advanced cleaning rule candidates "
            "found by looking at real sample values (a common Brand placeholder, a Department "
            "placeholder, a UOM/type-looking column, a leading numeric code on Category/Subcategory). "
            "Review and correct everything in each form before adding it — especially UPC Strip "
            "Trailing Digits, which can't be guessed at all, and any Exclude Values, which are "
            "deliberately left for you to pick."
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
    if active_tab == "Pending Changes":
        st.subheader("Pending Changes")
        if is_admin:
            st.caption(
                "Every staged Add, Delete, and Edit from the Add Item, Delete Item, and UPC Overrides "
                "tabs, plus every staged Sources tab edit and new source, lands here in one place — "
                "durable and visible to every editor immediately. Push actually applies it to the database."
            )
        else:
            st.caption(
                "Every staged Add, Delete, and Edit from the Add Item, Delete Item, and UPC Overrides "
                "tabs lands here — durable and visible to every editor immediately. Push actually "
                "applies it to the database."
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
    if active_tab == "Upload & Ingest":
        st.subheader("Upload a distributor file")
        st.caption(
            "Do this every month when a new file comes in for a source — the new file's rows "
            "REPLACE that source's previously staged rows. Every upload is logged permanently "
            "below, with an explanation for anything dropped, not just the first time."
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
    if active_tab == "Merge":
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
                "Decisions, Settings, adds, deletes and UPC Overrides already update the item master the moment "
                "they're pushed — only the rows they affect — so this should find nothing. It re-checks every item "
                "(about 10 seconds) and stages a fix: every item that would change, what would change, and the rule "
                "or decision behind it. Nothing is written until you apply it, and an applied fix can be undone. "
                "Nothing is ever taken from source files."
            )
            undo = st.session_state.get("_rules_undo")
            if undo:
                u1, u2 = st.columns([3, 1])
                u1.caption(f"Last fix: {len(undo['upcs']):,} item(s) changed by {undo['by']}.")
                if u2.button("Undo that fix", key="rules_undo_btn", width='stretch'):
                    dept_mapping.undo_rules_plan(ENGINE, undo)
                    st.session_state.pop("_rules_undo", None)
                    load_items.clear()
                    st.session_state["_toast"] = (f"Put {len(undo['upcs']):,} item(s) back exactly as they were.", "↩️")
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
                    st.download_button("⬇ Download the list (CSV)", plan.to_csv(index=False).encode(),
                                       file_name="item_master_recheck.csv", mime="text/csv", key="rules_dl")
                    a1, a2 = st.columns(2)
                    if a1.button(f"Apply these {len(plan):,} change(s)", type="primary", key="rules_fix_btn", width='stretch'):
                        with st.spinner("Applying..."):
                            res = dept_mapping.apply_rules_plan(ENGINE, plan, st.session_state["name"])
                        st.session_state.pop("_rules_plan", None)
                        if res["ok"]:
                            st.session_state["_rules_undo"] = res["undo"]
                            load_items.clear()
                            st.session_state["_toast"] = (f"Applied {len(plan):,} change(s) — Undo that fix puts them back.", "✅")
                        else:
                            st.session_state["_toast"] = ("Something changed since the check — nothing was applied. Check again.", "⚠️")
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
                with st.spinner("Loading..."):
                    added = dept_mapping.merge_added_items(ENGINE, int(mid))
                render_item_decisions(added, f"added_{mid}", f"merge_{mid}_added_items")

        stale = load_stale_sources()
        if stale:
            label = "source" if len(stale) == 1 else "sources"
            st.warning(
                f"⚠️ {len(stale)} {label} ({', '.join(stale)}) have raw data newer than the "
                "last Merge — Item Master and Department Review still show the old data until you "
                "compute and Push a new merge below."
            )
        else:
            st.caption("✅ Item Master is up to date with every source's current raw data.")

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
                    f"⚠️ {len(stale_since_compute)} {label} ({', '.join(stale_since_compute)}) have raw "
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
                st.caption(f"✓ Approved by: {', '.join(distinct_approvers)} ({len(distinct_approvers)} of {required} needed)")
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
                    "⚠️ Someone has a staged Item Master change or Department Review decision "
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
                        f"⚠️ Push blocked and this draft discarded — {len(result['stale_sources'])} "
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
                    + (f"; this month's snapshot is now #{result['monthly_snapshot_id']}" if result.get("monthly_snapshot_id") else "")
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
                        f"⚠️ {n} pending Department Review {label} (combo ID(s): "
                        f"{', '.join(str(c) for c in result['discarded_pending_combo_ids'])}) "
                        "were discarded because that combo's evidence changed during this Merge — "
                        "please re-review them fresh in Crosswalk/Unmatched/Broken Out."
                    )
                if result["discarded_item_master_upcs"]:
                    n = len(result["discarded_item_master_upcs"])
                    label = "change" if n == 1 else "changes"
                    st.warning(
                        f"⚠️ {n} pending Item Master {label} (UPC(s): "
                        f"{', '.join(result['discarded_item_master_upcs'])}) were discarded because "
                        "this Merge changed that item's data — please re-review and re-stage them "
                        "against the current item."
                    )
                    load_item_master_pending.clear()
                st.rerun()

        st.divider()
        with st.expander("Spot-check against a reference UPC list"):
            st.caption(
                "Upload a file with a known-good UPC list (e.g. a prior month's export) to sanity-check "
                "that this merge's UPC cleaning still lines up with it. A very low match percentage usually "
                "means a source's UPC cleaning rule (check digit, leading zeros) is now wrong — a match near "
                "0% almost always means a cleaning mistake, not that the data genuinely changed that much."
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
    if active_tab == "Snapshots":
        st.subheader("Snapshots")
        st.caption(
            "A snapshot is a full, point-in-time copy of the item master, every Department Review decision and "
            "staged change, Settings, and each source's settings (not the raw distributor files). Restoring one "
            "always saves today's data first, so a restore can be undone."
        )
        st.caption(
            f"Kept: **manual** snapshots until someone deletes them · **monthly** ones for the last "
            f"{dept_mapping.KEEP_MONTHLY_SNAPSHOTS} months (refreshed after each Merge push) · **automatic** safety "
            f"copies, the newest {dept_mapping.KEEP_SAFETY_SNAPSHOTS}."
        )

        def _after_restore(msg):
            clear_snapshot_caches()
            st.cache_data.clear()
            st.session_state["_toast"] = (msg, "↩️")
            st.rerun()

        # ---- Undo the latest restore ----------------------------------------
        last = dept_mapping.get_last_restore(ENGINE)
        if last and last["restored_from"] is not None:
            when = pd.to_datetime(last["taken_at"]).strftime("%m/%d %H:%M")
            was_undo = last["restored_kind"] == "safety_restore"
            with st.container(border=True):
                st.markdown(
                    f"↩️ **Last restore:** {last['taken_by']} restored snapshot #{last['restored_from']}"
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
                st.session_state["_toast"] = (f"Snapshot #{snapshot_id} taken.", "✅")
                st.rerun()

        st.divider()
        snapshots_df = load_snapshots()
        if snapshots_df.empty:
            st.caption("No snapshots yet.")
        else:
            KIND_BADGE = {"manual": "📌 Manual", "monthly": "📅 Monthly", "safety_merge": "🛟 Before a Merge push",
                          "safety_restore": "🛟 Before a restore"}
            fc1, fc2 = st.columns([2, 3])
            show = fc1.segmented_control(
                "Show", ["All", "Manual", "Monthly", "Automatic"], default="All", key="snap_filter",
                label_visibility="collapsed",
            ) or "All"
            q = fc2.text_input("Search snapshots", key="snap_search", label_visibility="collapsed",
                               placeholder="🔍 Search name, person, or #id").strip().lower()
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
                            clear_snapshot_caches()
                            st.rerun()
                        if dc2.button("Cancel", key=f"cancel_delete_btn_{snapshot_id}", width='stretch'):
                            st.session_state.pop(delete_key, None)
                            st.rerun()


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
