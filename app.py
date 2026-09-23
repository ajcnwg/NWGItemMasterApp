"""
NWG Item Master App
--------------------
Self-service replacement for the Excel-based ingestion pipeline in
script.py: configure distributor sources, upload their files, merge by
source priority, and browse/edit the resulting item master — all backed by
Azure SQL, no Excel involved.
"""

import re

import pandas as pd
import streamlit as st
import streamlit_authenticator as stauth
import yaml
from sqlalchemy import text

from autodetect import (
    analyze_source,
    guess_source_identities_batch,
    list_sheet_names,
    sheet_has_minimum_info,
)
from db import get_engine, robust_begin, robust_connect
from ingest import (
    REASON_EXPLANATIONS, SIZE_FORMATS, load_raw_upload, map_and_clean,
    read_raw_file, save_raw_upload, stage_source,
)
import dept_mapping

st.set_page_config(page_title="NWG Item Master App", layout="wide")

# Custom full-page loading overlay, replacing Streamlit's plain top-left
# "Running load_items()." text. Streamlit mounts an EMPTY
# [data-testid="stStatusWidget"] div for the exact duration of any script
# rerun (present with no children while running, removed entirely once
# idle — its own spinner icon is CSS-only, not a DOM child) and removes it
# the instant the rerun finishes. `:has()` is live/reactive to DOM changes,
# so this pure-CSS rule needs no JS polling to track that state.
st.markdown(
    """
    <div class="app-loading-overlay">
        <div class="app-loading-spinner"></div>
        <div class="app-loading-text">Working…</div>
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
    .app-loading-overlay {
        position: fixed;
        top: 4.5rem;
        right: 1rem;
        z-index: 1000000;
        background: rgba(14, 17, 23, 0.92);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 8px;
        padding: 0.6rem 1rem;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
        display: flex;
        flex-direction: row;
        align-items: center;
        gap: 0.75rem;
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.15s ease-out;
    }
    body:has([data-testid="stStatusWidget"]) .app-loading-overlay {
        opacity: 1;
        transition: opacity 0.15s ease-in 1s;
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
        width: 1.1rem;
        height: 1.1rem;
        border-radius: 50%;
        border: 3px solid rgba(255, 255, 255, 0.2);
        border-top-color: #ff4b4b;
        animation: app-loading-spin 0.8s linear infinite;
    }
    @keyframes app-loading-spin {
        to { transform: rotate(360deg); }
    }
    .app-loading-text {
        color: rgba(255, 255, 255, 0.9);
        font-size: 0.85rem;
        font-weight: 500;
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

authenticator.login(location="main")

auth_status = st.session_state.get("authentication_status")
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

# Floats this row up into Streamlit's own top chrome (where "Deploy" and the
# "⋮" menu live) instead of taking its own row above the title — st.container's
# `key` gives it a stable "st-key-topbar" class we can target with :has() to
# position the whole block without touching Streamlit's native header markup.
# The <style> tag is injected as the FIRST element inside that same fixed
# container (rather than before it) so its own dead flow-space is absorbed
# by the container's own already-collapsed placeholder — a <style> tag has
# no rendered box of its own, and applies to the whole document regardless
# of where in the DOM it sits, so nesting it here changes nothing except
# which element pays for Streamlit's per-element layout overhead.
with st.container(key="topbar"):
    st.markdown(
        """
        <style>
        /* streamlit-authenticator's login() call leaves an empty placeholder
           behind after a successful login (class st-key-init) that still
           reserves a full line of height — with "Signed in as / Logout" now
           floated away from this spot instead of filling it, that leftover
           reads as a stray gap above the title. Safe to collapse: it has no
           content once authenticated. */
        div.st-key-init {
            display: none !important;
        }
        div[data-testid="stLayoutWrapper"]:has(> div.st-key-topbar) {
            margin-bottom: -2rem !important;
        }
        div.st-key-topbar {
            position: fixed !important;
            top: 0;
            left: 4.5rem;
            width: fit-content !important;
            height: 60px;
            z-index: 999995;
            display: flex !important;
            flex-direction: row !important;
            align-items: center !important;
            gap: 1rem;
        }
        /* The <style> tag's own wrapping element would otherwise be an
           empty, but still visible, flex item sitting before the caption —
           hide it; the <style> rules still apply regardless. */
        div.st-key-topbar > div[data-testid="stElementContainer"]:has(style) {
            display: none;
        }
        /* Streamlit's own rules give each stElementContainer flex:1 1 0%,
           splitting the row evenly by count instead of sizing to content —
           fine for a column of widgets, wrong for two items meant to sit
           at their natural width side by side. */
        div.st-key-topbar > div[data-testid="stElementContainer"] {
            flex: none !important;
            width: fit-content !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.caption(f"Signed in as **{st.session_state['name']}** ({user_role})")
    authenticator.logout("Logout", "main")

ENGINE = get_engine()


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


@st.cache_data
def load_items() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT i.upc AS UPC, i.description AS Description, i.department AS Department, "
                "i.category AS Category, i.subcategory AS Subcategory, i.brand AS Brand, "
                "i.pack AS Pack, i.size AS Size, i.uom AS UOM, "
                "i.source_key AS SourceKey, i.created_at AS CreatedAt, i.updated_at AS UpdatedAt, "
                # A manual_overrides row exists for BOTH a manually-added item
                # and a manually-corrected existing one (push_item_master_add/
                # _edit both write there so the correction survives the next
                # Merge) — this is the one reliable signal for "a human's
                # correction is behind this row," since an edited row keeps
                # whatever source_key it already had, not "manual".
                "mo.updated_by AS ManuallyEditedBy, mo.updated_at AS ManuallyEditedAt "
                "FROM dbo.items i LEFT JOIN dbo.manual_overrides mo ON mo.upc = i.upc "
                "ORDER BY Department, Category, Description"
            ),
            conn,
        )


@st.cache_data
def load_sources() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(text("SELECT * FROM dbo.sources ORDER BY priority_rank"), conn)


@st.cache_data
def load_raw_item_counts() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, COUNT(*) AS row_count, MAX(loaded_at) AS last_loaded "
                "FROM dbo.raw_items GROUP BY source_key"
            ),
            conn,
        )


@st.cache_data
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


@st.cache_data
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


@st.cache_data
def load_snapshots() -> pd.DataFrame:
    return dept_mapping.list_snapshots(ENGINE)


@st.cache_data
def load_has_snapshot_this_month() -> bool:
    return dept_mapping.has_snapshot_this_month(ENGINE)


def clear_snapshot_caches() -> None:
    load_snapshots.clear()
    load_has_snapshot_this_month.clear()


@st.cache_data
def load_merge_compute_meta() -> dict | None:
    return dept_mapping.get_merge_compute_meta(ENGINE)


@st.cache_data
def load_stale_sources_since_compute() -> list[str]:
    return dept_mapping.get_stale_sources_since_compute(ENGINE)


@st.cache_data
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


@st.cache_data
def load_discard_notices() -> pd.DataFrame:
    return dept_mapping.get_discard_notices(ENGINE)


def render_discard_notices(entity_type: str) -> None:
    """Durable, visible-to-everyone notice that someone's staged-but-not-
    yet-pushed work didn't survive — either a Merge discarded it (see
    push_merge_compute's own notes on why this can no longer just be a
    one-shot warning to whoever clicked Push) or a different person staged
    a conflicting decision for the same combo/UPC and silently overwrote
    it (see save_pending_changes_bulk/admin_override_upc). entity_type
    filters to "item_master" or "dept_review" so each Pending Changes
    section only shows notices relevant to it.

    Grouped by (who triggered it, who originally staged it) rather than
    one card per row — a single admin override across a 200-item Broken
    Out group fires one discard notice per UPC, and without grouping that
    used to mean 200 full-width cards for one actual event."""
    notices = load_discard_notices()
    notices = notices[notices["entity_type"] == entity_type]
    if notices.empty:
        return
    groups = {}
    for _, n in notices.iterrows():
        groups.setdefault((n["triggered_by"], n["originally_staged_by"]), []).append(n)
    with st.container(border=True):
        hc1, hc2 = st.columns([5, 1.5])
        hc1.error(f"⚠️ {len(notices)} staged change(s) didn't survive — please re-review.")
        if len(groups) > 1 and hc2.button("Dismiss all", key=f"dismiss_discard_all_{entity_type}", width='stretch'):
            dept_mapping.dismiss_discard_notices(ENGINE, notices["id"].tolist())
            load_discard_notices.clear()
            st.rerun()
        for (triggered_by, originally_staged_by), items in groups.items():
            with st.container(border=True):
                gc1, gc2 = st.columns([5, 1.5])
                who = f"staged by **{originally_staged_by}**" if originally_staged_by else "staged"
                gc1.markdown(f"**{len(items)} item(s)** {who} — discarded by **{triggered_by or 'a merge'}**")
                if len(items) > 1:
                    if gc2.button("Dismiss all", key=f"dismiss_discard_group_{entity_type}_{hash((triggered_by, originally_staged_by))}", width='stretch'):
                        dept_mapping.dismiss_discard_notices(ENGINE, [int(n["id"]) for n in items])
                        load_discard_notices.clear()
                        st.rerun()
                    with st.expander(f"Show item(s) ({len(items)})", key=f"discard_items_{entity_type}_{hash((triggered_by, originally_staged_by))}"):
                        # Sub-grouped by which combo/Broken Out group each
                        # item came from — an admin override across a big
                        # Broken Out group otherwise dumps dozens of UPCs
                        # from one combo into an undifferentiated list.
                        subgroups = {}
                        for n in items:
                            subgroups.setdefault(n["group_label"] or n["entity_label"], []).append(n)
                        for i, (group_label, sub_items) in enumerate(subgroups.items()):
                            if i:
                                st.divider()
                            if len(subgroups) > 1:
                                st.markdown(f"**{group_label}** — {len(sub_items)} item(s)")
                            for n in sub_items:
                                ic1, ic2 = st.columns([5, 1])
                                ic1.markdown(f"{n['entity_label']}" if len(subgroups) > 1 else f"**{n['entity_label']}**")
                                ic1.caption(f"{n['reason']} — at {n['triggered_at']}")
                                if ic2.button("Got it", key=f"dismiss_discard_{n['id']}", width='stretch'):
                                    dept_mapping.dismiss_discard_notice(ENGINE, int(n["id"]))
                                    load_discard_notices.clear()
                                    st.rerun()
                else:
                    n = items[0]
                    gc1.caption(f"{n['reason']} — at {n['triggered_at']}")
                    if gc2.button("Got it", key=f"dismiss_discard_{n['id']}", width='stretch'):
                        dept_mapping.dismiss_discard_notice(ENGINE, int(n["id"]))
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
        f"\U0001f504 A fresh merge draft is ready — {result['item_count']:,} item(s) would replace the "
        f"live item master. {where}"
    )


@st.cache_data
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


@st.cache_data
def load_deleted_items() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT upc, description, department, category, subcategory, brand, pack, size, uom, "
                "deleted_by, deleted_at FROM dbo.deleted_upcs ORDER BY deleted_at DESC"
            ),
            conn,
        )


@st.cache_data
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


@st.cache_data
def load_dept_review_queue(tier: str) -> pd.DataFrame:
    return dept_mapping.get_review_queue(ENGINE, tier)


@st.cache_data
def load_combo_member_items(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_combo_member_items(ENGINE, combo_id)


@st.cache_data
def load_broken_out_combos() -> pd.DataFrame:
    return dept_mapping.get_broken_out_combos(ENGINE)


@st.cache_data
def load_pending_upc_overrides(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_pending_upc_overrides(ENGINE, combo_id)


@st.cache_data
def load_auto_decided_upc_overrides(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_auto_decided_upc_overrides(ENGINE, combo_id)


@st.cache_data
def load_decided_combos() -> pd.DataFrame:
    return dept_mapping.get_decided_combos(ENGINE)


@st.cache_data
def load_combo_upc_decisions(combo_id: int) -> pd.DataFrame:
    return dept_mapping.get_combo_upc_decisions(ENGINE, combo_id)


@st.cache_data
def load_strict_departments() -> pd.DataFrame:
    with db_connect_cached() as conn:
        return pd.read_sql(
            text(
                "SELECT source_key, old_department, trust_direct_evidence "
                "FROM dbo.dept_mapping_strict_departments ORDER BY source_key, old_department"
            ),
            conn,
        )


@st.cache_data
def load_departments() -> pd.DataFrame:
    return dept_mapping.get_departments(ENGINE)


@st.cache_data
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


def get_shared_dept_filter() -> dict:
    """The ONE search/sort/page-size state every UNLOCKED Department
    Review tab (Crosswalk/Unmatched/Broken Out/Decided) mirrors — change
    it on any unlocked tab and every other unlocked tab picks it up the
    next time it renders. A tab with its own "Lock filters" checked keeps
    its private settings instead (see get_dept_tab_locks / get_dept_tab_
    filters) and neither reads from nor writes to this."""
    return st.session_state.setdefault("dept_shared_filter", {
        "search": "", "sort_label": None, "sort_desc": True, "page_size": 10,
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
        "page_size": st.session_state.get(size_key, 10),
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
    st.session_state[size_key] = source["page_size"] if source["page_size"] in (5, 10, 25, 50) else 10

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
        defaults = {"search": "", "sort_label": default_sort_label, "sort_desc": True, "page_size": 10}
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
    st.session_state[size_key] = source["page_size"] if source["page_size"] in (5, 10, 25, 50) else 10

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
    page_size = pcol1.selectbox("Groups per page", [5, 10, 25, 50], key=size_key, on_change=_touch)
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
    st.session_state[size_widget_key] = st.session_state.get(page_size_key, 10)
    st.session_state[num_widget_key] = min(st.session_state.get(page_num_key, 1), total_pages)

    def _sync_size():
        st.session_state[page_size_key] = st.session_state[size_widget_key]
        st.session_state[page_num_key] = 1
        _sync_dept_tab(tab_key)

    def _sync_num():
        st.session_state[page_num_key] = st.session_state[num_widget_key]

    st.divider()
    c1, c2, _ = st.columns([1, 1, 3])
    c1.selectbox("Groups per page", [5, 10, 25, 50], key=size_widget_key, on_change=_sync_size)
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
    with container.expander(f"Show affected items ({n_upcs_total:,})", key=f"{key_prefix}_{combo_id}"):
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


@st.cache_data
def load_dept_pending_changes() -> dict:
    """Staged whole-combo Approves — {combo_id: {...}} — read from the
    database, not st.session_state. Every click writes here immediately
    (durable — survives a closed tab or an app restart) and is visible to
    every editor right away — same st.cache_data(no ttl)+.clear()
    convention as load_items/load_broken_out_combos."""
    return dept_mapping.get_pending_changes(ENGINE)


@st.cache_data
def load_dept_pending_upc_changes() -> dict:
    """Same idea as load_dept_pending_changes, but for individual per-UPC
    decisions staged on the Broken Out tab — {upc: {...}}."""
    return dept_mapping.get_pending_upc_changes(ENGINE)


@st.cache_data
def load_combo_suggestions() -> dict:
    """Disputed combos only (2+ people proposing different departments) —
    {combo_id: [{...}, ...]}. A combo with a single, agreed-on suggestion
    never appears here; it's already a normal decided row in
    load_dept_pending_changes instead."""
    return dept_mapping.get_combo_suggestions(ENGINE)


@st.cache_data
def load_upc_change_suggestions() -> dict:
    """Pending suggestions on already-decided Broken Out items, awaiting
    their owner's accept/deny — {upc: [{...}, ...]}. Unlike combo-level
    disputes, this is never peer-voting: only the owner (or admin) can act
    on these, see dept_mapping.accept_upc_suggestion/deny_upc_suggestion."""
    return dept_mapping.get_upc_change_suggestions(ENGINE)


@st.cache_data
def load_broken_out_claims() -> dict:
    return dept_mapping.get_broken_out_claims(ENGINE)


def clear_dept_suggestion_caches() -> None:
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
            hc0, hc1 = st.columns([0.4, 8])
            hc0.checkbox(
                "Save for later", value=False, key=f"dept_pending_snooze_combo_{combo_id}",
                label_visibility="collapsed",
                help="Move this to Saved for later — it's still unresolved, just out of the main Needs "
                     "agreement list until you check it again.",
            )
            hc1.markdown(f"**{(first['source_key'] or '').upper()} — {first['label']}** — {first['n_upcs_total']:,} item(s)")
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
            # Admin override and Show affected items share one row — both
            # are secondary, occasional actions, so they don't need a full
            # row each.
            ac1, ac2 = st.columns(2)
            if is_admin:
                with ac1.expander("Admin override", key=f"admin_override_combo_expander_{combo_id}"):
                    st.caption(
                        "Forces this decision through immediately, to ANY department — not just the ones "
                        "suggested above — regardless of the votes on file. Locks it: nobody else can "
                        "suggest or agree on it again until you remove the override from its Ready to push card."
                    )
                    oc1, oc2 = st.columns([3, 1.4])
                    override_dept = oc1.selectbox(
                        "Force to department", load_departments()["department"].tolist(),
                        key=f"admin_override_combo_dept_{combo_id}", label_visibility="collapsed",
                    )
                    if oc2.button("Override", key=f"admin_override_combo_btn_{combo_id}", width='stretch', type="primary"):
                        dept_mapping.admin_override_combo(
                            ENGINE, combo_id, override_dept, first.get("tier"), first["source_key"],
                            first["label"], first["n_upcs_total"], actor,
                        )
                        clear_dept_suggestion_caches()
                        st.session_state["_toast"] = (f"Admin override: **{first['label']}** → {override_dept}.", "🛡️")
                        st.rerun()
            render_affected_items_expander(combo_id, first["n_upcs_total"], "dispute_items", container=ac2)


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

    def _accept(upc, suggested_by, department):
        dept_mapping.accept_upc_suggestion(ENGINE, upc, suggested_by, department, actor)

    def _deny(upc, suggested_by):
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


@st.cache_data
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


@st.cache_data
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


def push_item_master_edit(upc: str, change: dict, actor: str) -> None:
    with db_begin() as conn:
        conn.execute(
            text(
                """
                UPDATE dbo.items
                SET description = :description, department = :department,
                    category = :category, subcategory = :subcategory,
                    brand = :brand, pack = :pack, size = :size, uom = :uom, source_key = :source_key,
                    updated_at = SYSUTCDATETIME()
                WHERE upc = :upc
                """
            ),
            {
                "upc": upc, "description": change["description"], "department": change["department"],
                "category": change["category"], "subcategory": change["subcategory"], "brand": change["brand"],
                "pack": change.get("pack"), "size": change.get("size"), "uom": change.get("uom"),
                "source_key": change["source_key"],
            },
        )
        # Also persist to manual_overrides so this correction survives the
        # next monthly Merge, which otherwise rebuilds dbo.items from
        # raw_items from scratch.
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
    visible_upcs = rows_df["upc"].tolist()

    if visible_upcs:
        scol1, scol2, scol3 = st.columns([1, 1, 3])
        if scol1.button("Select all shown", key="im_pending_select_all"):
            for upc in visible_upcs:
                st.session_state[f"im_pending_include_{upc}"] = True
            st.rerun()
        if scol2.button("Deselect all shown", key="im_pending_select_none"):
            for upc in visible_upcs:
                st.session_state[f"im_pending_include_{upc}"] = False
            st.rerun()

    needs_live_lookup = any(c["change_type"] in ("edit", "delete") for c in pending.values())
    live_by_upc = load_items().set_index("UPC").to_dict("index") if needs_live_lookup else {}
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
        push_fns = {"add": push_item_master_add, "delete": push_item_master_delete, "edit": push_item_master_edit}
        for upc in included_upcs:
            change = pending[upc]
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


@st.cache_data
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


def undo_recent_move(move_id: int) -> None:
    """Undoes ONE specific entry in the stack (by its own move_id, not
    combo_id — the same combo can legitimately have several stacked
    entries now). Only removes that one entry; any earlier entry for the
    same combo stays in place, still undoable, so a chain of moves can be
    walked back one real step at a time instead of only ever one step
    total."""
    moves = load_dept_recent_moves()
    move = next((m for m in moves if m["move_id"] == move_id), None)
    if move is None:
        return
    combo_id = move["combo_id"]
    dept_mapping.restore_combo_snapshot(ENGINE, combo_id, move["snapshot"], st.session_state["name"])
    dept_mapping.delete_recent_move(ENGINE, move["move_id"])
    load_dept_recent_moves.clear()
    clear_pending_for_combo(combo_id)
    clear_dept_review_caches()


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


def perform_send_back(combo_id: int, source_key: str, label: str, n_upcs_total: int, send_back_label: str, is_whole: bool) -> None:
    snapshot = dept_mapping.get_combo_snapshot(ENGINE, combo_id)
    if is_whole:
        dept_mapping.revert_combo(ENGINE, combo_id, st.session_state["name"])
    else:
        dept_mapping.revert_broken_out_combo(ENGINE, combo_id, st.session_state["name"])
    record_recent_move(combo_id, source_key, label, n_upcs_total, send_back_label, snapshot)
    clear_pending_for_combo(combo_id)
    clear_dept_review_caches()
    st.session_state.pop("dept_confirm_send_back", None)
    st.success(f"Sent back to review — {label}")
    st.rerun()


@st.dialog("Send back to review?")
def confirm_send_back_dialog():
    info = st.session_state.get("dept_confirm_send_back")
    if info is None:
        return
    st.warning(
        f"**{info['source_key'].upper()}** — {info['label']}\n\n"
        "This group has decisions on it already. Sending it back discards ALL of the following:"
    )
    for line in info["decision_lines"]:
        st.markdown(f"- {line}")
    st.caption("This can still be undone afterward from Pending Changes' \"Recent moves\" — but only as one step.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, send it back anyway", type="primary", width='stretch'):
        perform_send_back(
            info["combo_id"], info["source_key"], info["label"], info["n_upcs_total"],
            info["send_back_label"], info["is_whole"],
        )
    if c2.button("Cancel", width='stretch'):
        st.session_state.pop("dept_confirm_send_back", None)
        st.rerun()


def request_send_back(combo_id: int, source_key: str, label: str, n_upcs_total: int, send_back_label: str, is_whole: bool) -> None:
    """Entry point for every Send Back button — a combo with nothing to
    lose (never broken out further than the bare not_reviewed default, or
    a Whole Group combo, which never has per-UPC data at all) applies
    immediately with no interruption; one with real decisions on it stops
    for an explicit confirmation first, since Send Back is otherwise a
    silent, one-click way to discard real work."""
    decision_lines = [] if is_whole else summarize_combo_decisions(combo_id)
    if not decision_lines:
        perform_send_back(combo_id, source_key, label, n_upcs_total, send_back_label, is_whole)
        return
    st.session_state["dept_confirm_send_back"] = {
        "combo_id": combo_id, "source_key": source_key, "label": label,
        "n_upcs_total": n_upcs_total, "send_back_label": send_back_label,
        "is_whole": is_whole, "decision_lines": decision_lines,
    }
    st.rerun()


def perform_break_out(combo_id: int, source_key: str, label: str, n_upcs_total: int, upc_decisions: dict) -> None:
    snapshot = dept_mapping.get_combo_snapshot(ENGINE, combo_id)
    dept_mapping.break_out_combo(ENGINE, combo_id, st.session_state["name"], upc_decisions=upc_decisions)
    record_recent_move(combo_id, source_key, label, n_upcs_total, "Broken Out to UPC-Level", snapshot)
    load_dept_review_queue.clear()
    load_broken_out_combos.clear()
    st.session_state.pop("dept_confirm_break_out", None)
    st.success(f"Moved to Broken Out — {label}")
    st.rerun()


@st.dialog("Break out with a head start?")
def confirm_break_out_dialog():
    info = st.session_state.get("dept_confirm_break_out")
    if info is None:
        return
    n_hits = len(info["upc_decisions"])
    st.markdown(
        f"**{info['source_key'].upper()}** — {info['label']}\n\n"
        f"{n_hits:,} of {info['n_upcs_total']:,} item(s) could be auto-decided right now via Brand/UPC "
        "Root/Description Match — the same matching the engine itself runs. Start with those already "
        "filled in, or leave every item blank so you decide each one yourself?"
    )
    c1, c2 = st.columns(2)
    if c1.button("Apply auto-decisions", type="primary", width='stretch'):
        perform_break_out(info["combo_id"], info["source_key"], info["label"], info["n_upcs_total"], info["upc_decisions"])
    if c2.button("Start blank", width='stretch'):
        perform_break_out(info["combo_id"], info["source_key"], info["label"], info["n_upcs_total"], {})
    if st.button("Cancel"):
        st.session_state.pop("dept_confirm_break_out", None)
        st.rerun()


def request_break_out(combo_id: int, source_key: str, label: str, n_upcs_total: int) -> None:
    """Entry point for every Break Out button — computes whether any
    per-UPC auto-matching could decide something for this combo right now
    (see dept_mapping.compute_upc_decisions_for_combo); if so, asks the
    human which starting point they want instead of silently picking one.
    Nothing to choose between (no hits) just breaks out immediately."""
    upc_decisions = dept_mapping.compute_upc_decisions_for_combo(ENGINE, combo_id)
    if not upc_decisions:
        perform_break_out(combo_id, source_key, label, n_upcs_total, {})
        return
    st.session_state["dept_confirm_break_out"] = {
        "combo_id": combo_id, "source_key": source_key, "label": label,
        "n_upcs_total": n_upcs_total, "upc_decisions": upc_decisions,
    }
    st.rerun()


def upc_exists(upc: str) -> bool:
    with db_connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM dbo.items WHERE upc = :upc"), {"upc": upc}
        ).first() is not None


st.title("NWG Item Master App")

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
    div[role="radiogroup"] {
        gap: 0;
        border-bottom: 1px solid rgba(250, 250, 250, 0.2);
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"] {
        margin: 0;
        cursor: pointer;
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"] > div {
        padding: 8px 16px 10px 16px;
        margin-bottom: -1px;
        border-bottom: 2px solid transparent;
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"] > div > div > div:first-child {
        display: none;
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"] p {
        margin: 0;
        font-size: 14px;
        color: rgba(250, 250, 250, 0.6);
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"]:hover p {
        color: rgba(250, 250, 250, 0.9);
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"][data-selected="true"] > div {
        border-bottom: 2px solid #ff4b4b;
    }
    div[role="radiogroup"] label[data-testid="stRadioOption"][data-selected="true"] p {
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
    page_size = st.session_state.get(page_size_key, 100)
    total_pages = max(1, (matched_count - 1) // page_size + 1)
    if st.session_state.get(page_num_key, 1) > total_pages:
        st.session_state[page_num_key] = total_pages

    pcol1, pcol2, pcol3 = st.columns([1, 1, 3])
    page_size = pcol1.selectbox("Rows per page", [50, 100, 250, 500, 1000], key=page_size_key)
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
            page_df, width='stretch', hide_index=True,
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
        review_subtab = st.radio(
            "Department Review section",
            ["Crosswalk", "Unmatched", "Broken Out", "Pending Changes", "Decided", "Settings"],
            horizontal=True, label_visibility="collapsed", key="dept_review_subtab",
        )
        if total_pending_upcs and review_subtab != "Pending Changes":
            st.caption(f"\U0001f4cb {total_pending_upcs:,} item(s) staged, not yet pushed to the database — see Pending Changes.")

        if st.session_state.get("dept_confirm_send_back") is not None:
            confirm_send_back_dialog()
        if st.session_state.get("dept_confirm_break_out") is not None:
            confirm_break_out_dialog()

        if review_subtab == "Settings":
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
                num_rows="dynamic",
                column_config={
                    "source_key": st.column_config.SelectboxColumn("Source", options=sources_for_strict, required=True),
                    "old_department": st.column_config.TextColumn("Old Department (exact text)", required=True),
                    "trust_direct_evidence": st.column_config.CheckboxColumn("Trust Direct Evidence?", default=False),
                },
            )
            if st.button("Save Strict Departments", type="primary"):
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
                st.success(
                    "Saved. This takes effect the next time the Department engine runs "
                    "(the next Run Merge) — it doesn't re-decide anything immediately."
                )
                st.rerun()

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

            add_col1, add_col2 = st.columns([3, 1])
            new_dept = add_col1.text_input(
                "Add a new Department", key="new_department_input", label_visibility="collapsed",
                placeholder="Add a new Department (e.g. BULK)",
            )
            if add_col2.button("Add", key="add_department_btn", width='stretch'):
                if new_dept.strip():
                    dept_mapping.add_department(ENGINE, new_dept)
                    load_departments.clear()
                    st.rerun()

            st.dataframe(
                departments_df.rename(columns={"department": "Department", "source_type": "Source"}),
                hide_index=True, width='stretch',
            )

            manual_depts = departments_df[departments_df["source_type"] == "manual"]["department"].tolist()
            if manual_depts:
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
                num_rows="dynamic",
                column_config={
                    "source_key": st.column_config.SelectboxColumn("Source", options=sources_for_defaults, required=True),
                    "old_department": st.column_config.TextColumn("Old Department (exact text)", required=True),
                    "new_department": st.column_config.SelectboxColumn("Default Department", options=department_choices, required=True),
                },
            )
            if st.button("Save Unmatched Defaults", type="primary"):
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
                st.success("Saved. This takes effect the next time the Department engine runs.")
                st.rerun()

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
            render_auto_merge_result(st.session_state.pop("_dept_push_auto_merge_result", None))
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
                        c0, c1, c2, c3 = st.columns([0.4, 3.6, 2, 1])
                        c0.checkbox(
                            "Include", value=True, key=f"dept_pending_include_combo_{combo_id}",
                            label_visibility="collapsed",
                            help="Included in the next push — uncheck to save this one for later.",
                        )
                        c1.markdown(f"**{change['source_key'].upper()}** — {change['label']}")
                        c2.markdown(f"Approve as **{change['department']}**")
                        is_primary_stager = actor == change.get("staged_by")
                        undo_help = (
                            None if is_primary_stager
                            else f"Only {change.get('staged_by') or 'the person who staged this'} (or an admin) can undo it outright — "
                                 "your click just lets them know you think it should be."
                        )
                        if c3.button("Undo", key=f"undo_pending_{combo_id}", width='stretch', help=undo_help):
                            result = dept_mapping.request_undo_combo(ENGINE, combo_id, actor, is_admin=is_admin)
                            clear_dept_suggestion_caches()
                            if result["executed"]:
                                st.session_state.pop(f"dept_pending_include_combo_{combo_id}", None)
                                st.session_state["_toast"] = (f"Undone: **{change['label']}**.", "↩️")
                            else:
                                st.session_state["_toast"] = (
                                    f"Only **{result['waiting_on']}** (or an admin) can undo **{change['label']}** — "
                                    "let them know, or ask an admin.",
                                    "⏳",
                                )
                            st.rerun()
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
                        # Admin override and Show affected items share one
                        # row — both are secondary, occasional actions.
                        ac1, ac2 = st.columns(2)
                        if is_admin:
                            with ac1.expander("Admin override", key=f"admin_override_ready_combo_expander_{combo_id}"):
                                if is_locked:
                                    st.caption(f"Locked by **{change['overridden_by']}**'s override. Change it, or unlock it for normal editing.")
                                    oc1, oc2 = st.columns([3, 1.4])
                                    override_dept = oc1.selectbox(
                                        "Change override to", pc_department_options,
                                        key=f"admin_override_ready_combo_dept_{combo_id}", label_visibility="collapsed",
                                    )
                                    if oc2.button(
                                        "Update override", key=f"admin_override_ready_combo_btn_{combo_id}",
                                        width='stretch', type="primary", disabled=override_dept == change["department"],
                                    ):
                                        dept_mapping.admin_override_combo(
                                            ENGINE, combo_id, override_dept, change["tier"], change["source_key"],
                                            change["label"], change["n_upcs_total"], actor,
                                        )
                                        clear_dept_suggestion_caches()
                                        st.session_state["_toast"] = (f"Admin override: **{change['label']}** → {override_dept}.", "🛡️")
                                        st.rerun()
                                    if st.button("Remove override (unlock)", key=f"remove_override_combo_{combo_id}", width='stretch'):
                                        dept_mapping.remove_combo_override(ENGINE, combo_id, actor)
                                        clear_dept_suggestion_caches()
                                        st.session_state["_toast"] = (f"Unlocked **{change['label']}** — open to normal editing again.", "🔓")
                                        st.rerun()
                                else:
                                    st.caption(
                                        "Forces this decision to ANY department immediately, regardless of who's "
                                        "currently backing it, and LOCKS it — nobody else can suggest or agree on "
                                        "it again until you remove the override."
                                    )
                                    oc1, oc2 = st.columns([3, 1.4])
                                    override_dept = oc1.selectbox(
                                        "Force to department", pc_department_options,
                                        key=f"admin_override_ready_combo_dept_{combo_id}", label_visibility="collapsed",
                                    )
                                    if oc2.button(
                                        "Override", key=f"admin_override_ready_combo_btn_{combo_id}",
                                        width='stretch', type="primary", disabled=override_dept == change["department"],
                                    ):
                                        dept_mapping.admin_override_combo(
                                            ENGINE, combo_id, override_dept, change["tier"], change["source_key"],
                                            change["label"], change["n_upcs_total"], actor,
                                        )
                                        clear_dept_suggestion_caches()
                                        st.session_state["_toast"] = (f"Admin override: **{change['label']}** → {override_dept}.", "🛡️")
                                        st.rerun()
                        render_affected_items_expander(combo_id, change["n_upcs_total"], "pending_items", container=ac2)

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
                        c0, c1, c2, c3 = st.columns([0.4, 3.6, 2, 1])
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
                        undo_help = (
                            None if is_primary_editor
                            else f"Only {primary or 'whoever first worked on this group'} (or an admin) can move the "
                                 "whole group back outright — your click just lets them know."
                        )
                        if c3.button("Undo Group", key=f"undo_pending_upc_combo_{combo_id}", width='stretch', help=undo_help):
                            result = dept_mapping.request_undo_upc_group_all(ENGINE, combo_id, actor, is_admin=is_admin)
                            clear_dept_suggestion_caches()
                            if result["executed"]:
                                st.session_state.pop(f"dept_pending_include_upc_group_{combo_id}", None)
                                st.session_state["_toast"] = (f"Undone: **{first['label']}** ({len(items)} item(s)).", "↩️")
                            else:
                                st.session_state["_toast"] = (
                                    f"Only **{result['waiting_on']}** (or an admin) can move **{first['label']}** back — "
                                    "let them know, or ask an admin.",
                                    "⏳",
                                )
                            st.rerun()
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
                                    "New Department": "",
                                }
                                for upc, change in items
                            ])
                            edited = st.data_editor(
                                display_df, hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items)),
                                key=f"pending_upc_editor_{combo_id}",
                                disabled=["UPC", "Description", "Department", "Owner", "Revised By", "Admin Override By"],
                                column_config={
                                    "New Department": st.column_config.SelectboxColumn(
                                        options=[""] + pc_department_options, required=False,
                                        help="Fill in for any row you want to change — your own items apply "
                                             "directly, anyone else's becomes a suggestion for its owner. Paste "
                                             "or drag-fill down the column to set several at once.",
                                    ),
                                },
                            )
                            if st.button("Apply Changes", key=f"pending_upc_apply_{combo_id}"):
                                decisions = {
                                    row["UPC"]: {
                                        "department": row["New Department"], "combo_id": combo_id,
                                        "label": first["label"], "source_key": first.get("source_key"),
                                        "description": next(c.get("description") for u, c in items if u == row["UPC"]),
                                    }
                                    for _, row in edited.iterrows()
                                    if row["New Department"] and not pd.isna(row["New Department"])
                                }
                                if not decisions:
                                    st.info("Fill in a New Department for at least one row first.")
                                else:
                                    results = dept_mapping.stage_broken_out_decisions(ENGINE, decisions, actor, is_admin=is_admin)
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
                                    st.session_state["_toast"] = (", ".join(msg_bits) + ".", "🗳️")
                                    st.rerun()

                        if is_admin:
                            with st.expander("Admin override (whole group)", key=f"admin_override_upc_group_expander_{combo_id}"):
                                locked_count = sum(1 for _, c in items if c.get("overridden_by"))
                                if locked_count:
                                    st.caption(f"{locked_count} item(s) currently locked by an admin override.")
                                    if st.button("Remove all overrides in this group", key=f"remove_upc_group_override_{combo_id}", width='stretch'):
                                        n = dept_mapping.remove_upc_override_group(ENGINE, combo_id, actor)
                                        clear_dept_suggestion_caches()
                                        st.session_state["_toast"] = (f"Unlocked {n} item(s) in **{first['label']}**.", "🔓")
                                        st.rerun()
                                st.caption(
                                    "Forces EVERY currently staged item in this group to one department and "
                                    "locks all of them — nobody else can change any of them until you remove "
                                    "the override (above, once it's set)."
                                )
                                oc1, oc2 = st.columns([3, 1.4])
                                group_override_dept = oc1.selectbox(
                                    "Force whole group to department", pc_department_options,
                                    key=f"admin_override_upc_group_dept_{combo_id}", label_visibility="collapsed",
                                )
                                if oc2.button("Override Group", key=f"admin_override_upc_group_btn_{combo_id}", width='stretch', type="primary"):
                                    n = dept_mapping.admin_override_upc_group(ENGINE, combo_id, group_override_dept, actor)
                                    clear_dept_suggestion_caches()
                                    st.session_state["_toast"] = (f"Admin override: {n} item(s) in **{first['label']}** → {group_override_dept}.", "🛡️")
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
                        for upc in included_pending_upc_changes:
                            dept_mapping.delete_pending_upc_change(ENGINE, upc)
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
                    st.success(f"Pushed {pushed_count:,} item(s) to the database.")
                    # These decisions live in dept_mapping_combos/
                    # dept_mapping_upc_overrides — Department in dbo.items
                    # only reflects them after Merge substitutes them in, so
                    # recompute a fresh merge draft now instead of leaving
                    # Item Master showing stale Department text until a
                    # separate Merge trip. Still needs a push on the Merge
                    # tab to actually go live — see
                    # auto_recompute_and_push_merge's docstring.
                    with st.spinner("Recomputing the merge draft with your decision(s)..."):
                        st.session_state["_dept_push_auto_merge_result"] = auto_recompute_and_push_merge()
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
                            if rc2.button("Undo now", key=f"undo_requested_combo_{combo_id}", width='stretch', type="primary"):
                                dept_mapping.request_undo_combo(ENGINE, combo_id, actor, is_admin=is_admin)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Undone: **{change['label']}**.", "↩️")
                                st.rerun()
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
                            if rc2.button("Undo now", key=f"undo_requested_upc_group_{combo_id}", width='stretch', type="primary"):
                                dept_mapping.request_undo_upc_group_all(ENGINE, combo_id, actor, is_admin=is_admin)
                                clear_dept_suggestion_caches()
                                st.session_state["_toast"] = (f"Undone: **{first['label']}**.", "↩️")
                                st.rerun()

            # ---- Recent moves (structural Break Out/Send Back undo — kept
            # last, since it's the least time-critical, most historical
            # section). ----
            if visible_recent_moves:
                st.divider()
                st.markdown("#### Recent moves")
                st.caption("Break Out / Send Back actions from the other tabs — applied immediately, undoable here.")
                for move in list(visible_recent_moves):
                    with st.container(border=True):
                        c1, c2, c3 = st.columns([4, 2, 1])
                        c1.markdown(f"**{move['source_key'].upper()}** — {move['label']}")
                        c2.markdown(f"{move['description']} — {move['n_upcs_total']:,} item(s)")
                        if c3.button("Undo", key=f"undo_recent_{move['move_id']}", width='stretch'):
                            undo_recent_move(move["move_id"])
                            st.rerun()

        elif review_subtab == "Broken Out":
            st.caption(
                "A group here couldn't be trusted as a whole — its items get decided one at a time "
                "instead. Pick a Department for as many as you're ready to and stage them; a group "
                "moves to Decided automatically once every one of its items has a real decision. Not "
                "sure about the whole group anymore? Send it back to combo-level review instead — "
                "that discards any item decisions made here so far."
            )
            broken_df = load_broken_out_combos()
            broken_df = broken_df[~broken_df["combo_id"].isin(pending_changes.keys())]
            if broken_df.empty:
                st.success("Nothing in Broken Out right now.")
            else:
                broken_df = broken_df.copy()
                broken_df["pending_count"] = broken_df["override_count"] - broken_df["decided_count"]
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

                        with st.expander(f"Review items ({int(row['pending_count']) - staged_here} left)", key=f"broken_out_expander_{combo_id}"):
                            items_df = load_pending_upc_overrides(combo_id)
                            items_df = items_df[~items_df["upc"].isin(pending_upc_changes.keys())]
                            if items_df.empty:
                                st.caption("Nothing left to decide here — push will move this group to Decided.")
                            else:
                                manual_count = items_df["manually_edited_by"].notna().sum()
                                if manual_count:
                                    m_label = "item has" if manual_count == 1 else "items have"
                                    st.caption(f"✏️ {manual_count} {m_label} a manual correction on file — see the column below.")

                                def _stage_upc_decisions(decisions: dict) -> None:
                                    # Re-checked against the DB fresh for every UPC,
                                    # not trusted from this (possibly stale, possibly
                                    # hours-old) browser tab — see
                                    # stage_broken_out_decisions. A UPC someone else
                                    # has since decided routes to a suggestion instead
                                    # of silently overwriting or corrupting anything.
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
                                    st.rerun()

                                # Bulk actions operate directly on items_df (not the
                                # per-row editor below) — trying to programmatically
                                # pre-fill a data_editor's OWN widget state after the
                                # user may have already touched individual rows is
                                # fragile (Streamlit keeps a widget's edits sticky
                                # once it has state); staging straight from the
                                # source data sidesteps that entirely and is exactly
                                # what "Stage These Item Decisions" already does.
                                bcol1, bcol2, bcol3 = st.columns([2.2, 1.6, 1.6])
                                has_suggestions = items_df["suggested_department"].notna().any()
                                if bcol1.button(
                                    "Accept all suggested departments", key=f"accept_sugg_{combo_id}",
                                    disabled=not has_suggestions,
                                    help="Stages every item below that has a Suggested Department, using that suggestion — skips items with no suggestion.",
                                ):
                                    decisions = {
                                        r["upc"]: {
                                            "department": str(r["suggested_department"]).strip(), "combo_id": combo_id,
                                            "label": label, "description": r["description"], "source_key": row["source_key"],
                                        }
                                        for _, r in items_df.iterrows()
                                        if pd.notna(r["suggested_department"]) and str(r["suggested_department"]).strip()
                                    }
                                    if decisions:
                                        _stage_upc_decisions(decisions)
                                    else:
                                        st.info("No suggested departments to accept here.")
                                bulk_dept = bcol2.selectbox(
                                    "Set all to…", [""] + department_options,
                                    key=f"bulk_dept_{combo_id}", label_visibility="collapsed",
                                )
                                if bcol3.button("Apply to all items", key=f"bulk_apply_{combo_id}", disabled=not bulk_dept):
                                    decisions = {
                                        r["upc"]: {
                                            "department": bulk_dept, "combo_id": combo_id,
                                            "label": label, "description": r["description"], "source_key": row["source_key"],
                                        }
                                        for _, r in items_df.iterrows()
                                    }
                                    _stage_upc_decisions(decisions)

                                display_items = items_df.rename(columns={
                                    "upc": "UPC", "description": "Description", "brand": "Brand",
                                    "pack": "Pack", "size": "Size", "uom": "UOM",
                                    "suggested_department": "Suggested Department",
                                    "manually_edited_by": "Manually Edited By",
                                }).copy()
                                # Pre-filled with the suggestion (where there is
                                # one) rather than left blank — safe now that
                                # claiming makes this exclusively yours to review,
                                # unlike the old shared/unclaimed grid where a
                                # pre-filled value could look identical to one a
                                # human actually reviewed. Still just a starting
                                # point — clear a cell or use "Set all to…" to
                                # override it, and the native grid supports the
                                # usual copy/paste and drag-fill across a
                                # selected range for fast bulk correction.
                                display_items["Department"] = display_items["Suggested Department"].fillna("")
                                edited_items = st.data_editor(
                                    display_items,
                                    key=f"broken_out_items_editor_{combo_id}",
                                    width='stretch', hide_index=True,
                                    disabled=["UPC", "Description", "Brand", "Pack", "Size", "UOM", "Suggested Department", "suggested_via", "Manually Edited By"],
                                    column_order=["UPC", "Description", "Brand", "Pack", "Size", "UOM", "Suggested Department", "Manually Edited By", "Department"],
                                    column_config={
                                        "Department": st.column_config.SelectboxColumn(options=[""] + department_options, required=False),
                                    },
                                )
                                if st.button("Stage These Item Decisions", key=f"stage_upc_{combo_id}"):
                                    new_decisions = {}
                                    for _, item_row in edited_items.iterrows():
                                        dept = item_row["Department"]
                                        if dept and not pd.isna(dept) and str(dept).strip():
                                            new_decisions[item_row["UPC"]] = {
                                                "department": str(dept).strip(), "combo_id": combo_id,
                                                "label": label, "description": item_row["Description"],
                                                "source_key": row["source_key"],
                                            }
                                    if new_decisions:
                                        _stage_upc_decisions(new_decisions)
                                    else:
                                        st.info("Pick a Department for at least one item first.")

                        auto_decided_df = load_auto_decided_upc_overrides(combo_id)
                        if not auto_decided_df.empty:
                            with st.expander(
                                f"Auto-decided items, not yet confirmed ({len(auto_decided_df)})",
                                key=f"broken_out_auto_expander_{combo_id}",
                            ):
                                st.caption(
                                    "Filled in automatically (Brand/UPC Root/Description Match) — the "
                                    "department is already set, but this combo hasn't graduated to Decided "
                                    "yet, so confirming one stages it here like any other decision: it shows "
                                    "up on Pending Changes and needs a Push (and is undoable there) rather "
                                    "than applying immediately. Once the whole combo is fully decided and "
                                    "pushed, its Decided tab card offers a no-push sign-off instead."
                                )
                                display_auto = auto_decided_df.rename(columns={
                                    "upc": "UPC", "description": "Description", "brand": "Brand",
                                    "pack": "Pack", "size": "Size", "uom": "UOM",
                                    "department": "Department", "decided_via": "Decided Via",
                                    "manually_edited_by": "Manually Edited By",
                                }).copy()
                                display_auto["Stage"] = False
                                edited_auto = st.data_editor(
                                    display_auto,
                                    key=f"broken_out_auto_editor_{combo_id}",
                                    width='stretch', hide_index=True,
                                    disabled=[
                                        "UPC", "Description", "Brand", "Pack", "Size", "UOM",
                                        "Department", "Decided Via", "Manually Edited By",
                                    ],
                                    column_order=[
                                        "UPC", "Description", "Brand", "Pack", "Size", "UOM",
                                        "Department", "Decided Via", "Manually Edited By", "Stage",
                                    ],
                                )
                                acol1, acol2 = st.columns(2)

                                def _stage_auto_as_decisions(upcs_df) -> None:
                                    decisions = {
                                        r["upc"]: {
                                            "department": r["department"], "combo_id": combo_id,
                                            "label": label, "description": r["description"],
                                            "source_key": row["source_key"],
                                        }
                                        for _, r in upcs_df.iterrows()
                                    }
                                    _stage_upc_decisions(decisions)
                                    load_auto_decided_upc_overrides.clear()

                                if acol1.button("Stage checked items", key=f"stage_checked_auto_{combo_id}"):
                                    to_stage = auto_decided_df[auto_decided_df["upc"].isin(
                                        edited_auto.loc[edited_auto["Stage"] == True, "UPC"]  # noqa: E712
                                    )]
                                    if not to_stage.empty:
                                        _stage_auto_as_decisions(to_stage)
                                    else:
                                        st.info("Check at least one item first.")
                                if acol2.button("Stage all shown", key=f"stage_all_auto_{combo_id}"):
                                    _stage_auto_as_decisions(auto_decided_df)

                render_bottom_pagination("broken_out_page_size", page_num_key, "broken_out", total_pages)

        elif review_subtab == "Decided":
            st.caption(
                "Every group that's actually FINISHED — a group still being decided item by item lives "
                "on the Broken Out tab instead, even if it's mostly done, until its very last item is "
                "decided. Status shows exactly how: Whole Group (Auto/Manual), or graduated from Broken "
                "Out (Fully Auto, Partially Auto, or Manually Decided). Any row can be sent back for "
                "review — a Whole Group row reverts to a fresh combo-level decision; a Broken Out row "
                "discards its item-by-item decisions and starts over the same way."
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
                        top1, top2, top3 = st.columns([3, 2.4, 1.4])
                        top1.markdown(f"**{row['source_key'].upper()}** — {label}")
                        if is_whole:
                            status_line = f"{row['status']} — Decided as **{row['decided_department']}**, {int(row['n_upcs_total']):,} item(s)"
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
                        if top3.button("Send Back to Review", key=f"revert_decided_{combo_id}", width='stretch'):
                            # Not a new department decision — undoes the
                            # standing one and reopens the combo for fresh
                            # review, so it applies immediately rather than
                            # through Pending Changes (same reasoning as
                            # Break Out / Broken Out's own Send Back). Still
                            # undoable from "Recent moves" — and if this is
                            # a Broken Out combo with real per-item
                            # decisions, request_send_back stops for a
                            # confirmation first instead of silently
                            # discarding them (a Whole Group combo never
                            # has per-UPC data, so it always skips straight
                            # to the immediate action).
                            request_send_back(combo_id, row["source_key"], label, int(row["n_upcs_total"]), "Send Back to Review", is_whole)
                        if is_whole:
                            # Auto and Manual are the only two whole-group decided_via
                            # states (get_decided_combos' own status logic treats
                            # anything that isn't literally "Auto" as Manual) — so
                            # confirming here just needs to flip that one value; the
                            # status label above updates to "— Manual" on its own.
                            if row["status"] == "Whole Group — Auto":
                                if st.button("✓ Mark as reviewed", key=f"confirm_whole_{combo_id}"):
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
                                            dept_mapping.confirm_upc_decisions(ENGINE, auto_upcs, st.session_state["name"])
                                            load_combo_upc_decisions.clear()
                                            load_decided_combos.clear()
                                            st.rerun()
                                    st.dataframe(
                                        items_df.rename(columns={
                                            "upc": "UPC", "description": "Description", "brand": "Brand",
                                            "department": "Department", "decided_via": "Decided Via",
                                            "decided_by": "Decided By", "pushed_by": "Pushed By",
                                        }),
                                        hide_index=True, width='stretch', height=min(300, 40 + 35 * len(items_df)),
                                    )

                render_bottom_pagination("decided_page_size", page_num_key, "decided", total_pages)

        elif review_subtab in ("Crosswalk", "Unmatched"):
            tier = "review" if review_subtab == "Crosswalk" else "unmatched"

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

                render_bottom_pagination(f"dept_review_page_size_{tier}", page_num_key, f"dept_review_{tier}", total_pages)

    # -------------------------------------------------------------------
    # Add Item
    # -------------------------------------------------------------------
    if active_tab == "Add Item":
        st.subheader("Add a new item")
        item_master_pending = item_master_pending_cross_link()
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
                if not new_upc or not new_description:
                    st.error("UPC and Description are required.")
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
                "For every UPC across all enabled sources, walks priority order (lowest "
                "priority_rank first) and takes Category/Subcategory/Brand/Description all "
                "together from the first source with every field filled in — falling back "
                "to the highest-priority source with any data for that UPC otherwise. "
                "Mirrors merge_all_sources in script.py. Department is different: NWG/P1's "
                "own Department is always trusted directly; for any other winning source, "
                "Department instead comes from Department Review's decision for that UPC "
                "(Crosswalk/Unmatched/Broken Out), left blank if nothing's been decided yet "
                "— never that other source's own raw, often messy Department text. Manual "
                "edits and manually-added items (from manual_overrides) are then applied on "
                "top of all of this, and manually-deleted UPCs (deleted_upcs) are excluded "
                "— so this is safe to re-run every month without losing prior manual work. "
                "Computing a merge doesn't change anything live — review it below, then "
                "Push to actually replace dbo.items and recompute Department Review's groups."
            )
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
                    lcol2.metric("Changed", f"{last_merge['changed_count']:,}")
                    lcol3.metric("Removed", f"{last_merge['removed_count']:,}")
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
                f"\U0001f7e2 **Computed merge ready to push** — {compute_meta['item_count']:,} item(s) "
                "would replace the live item master. Review the details below, then push when ready."
            )
            with st.container(border=True):
                st.markdown(f"**{compute_meta['item_count']:,} item(s) total** if pushed")
                mcol1, mcol2, mcol3 = st.columns(3)
                mcol1.metric("Added", f"{compute_meta.get('added_count') or 0:,}", help="Brand new UPCs not currently in the live item master.")
                mcol2.metric("Changed", f"{compute_meta.get('changed_count') or 0:,}", help="Existing UPCs where at least one field would come out different from what's live now.")
                mcol3.metric("Removed", f"{compute_meta.get('removed_count') or 0:,}", help="UPCs currently live that this computed merge no longer produces at all.")
                st.caption(
                    f"{compute_meta['overrides_applied']} manual override(s) applied, "
                    f"{compute_meta['deleted_excluded']} manually-deleted UPC(s) excluded"
                )
                render_merge_change_breakdown(compute_meta.get("changed_by_field") or {}, compute_meta.get("changed_by_source") or {})
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
                "Pushing REPLACES the entire live item master with what's shown above, then re-runs "
                "Department Review's group engine against it — a safety snapshot of today's current "
                "data is taken automatically first, so this can be undone from the Snapshots tab."
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
                    f"(safety snapshot #{result['safety_snapshot_id']} taken first)."
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
            "A snapshot is a full, point-in-time copy of the item master and every Department Review "
            "decision — something to come back to later if a batch of changes needs undoing entirely. "
            "Take one deliberately before a big round of changes, not automatically on every Merge "
            "(that would create far too many to be useful). Restoring one always takes its own safety "
            "snapshot of the current data first, so a restore is itself undoable."
        )
        if not load_has_snapshot_this_month():
            st.info("No snapshot has been taken yet this month.")

        with st.form("take_snapshot_form"):
            label = st.text_input(
                "Label (optional)", placeholder="e.g. Before September price changes",
            )
            if st.form_submit_button("Take Snapshot Now", type="primary"):
                snapshot_id = dept_mapping.take_snapshot(ENGINE, st.session_state["name"], label or None)
                clear_snapshot_caches()
                st.success(f"Snapshot #{snapshot_id} taken.")
                st.rerun()

        st.divider()
        snapshots_df = load_snapshots()
        if snapshots_df.empty:
            st.caption("No snapshots yet.")
        else:
            st.caption(f"{len(snapshots_df)} snapshot(s), newest first.")
            for _, row in snapshots_df.iterrows():
                snapshot_id = int(row["snapshot_id"])
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
                    title = row["label"] if pd.notna(row["label"]) and row["label"] else f"({row['snapshot_month']} snapshot)"
                    c1.markdown(f"**{title}**")
                    c1.caption(f"Taken {row['taken_at']} by {row['taken_by'] or 'unknown'}")
                    c2.markdown(f"{row['item_count']:,} items, {row['combo_count']:,} combos")
                    restore_key = f"confirm_restore_{snapshot_id}"
                    if c3.button("Restore", key=f"restore_btn_{snapshot_id}", width='stretch'):
                        st.session_state[restore_key] = True
                    if c4.button("Delete", key=f"delete_btn_{snapshot_id}", width='stretch'):
                        dept_mapping.delete_snapshot(ENGINE, snapshot_id)
                        clear_snapshot_caches()
                        st.rerun()
                    if st.session_state.get(restore_key):
                        st.warning(
                            f"This REPLACES the entire live item master and every Department Review "
                            f"decision with snapshot #{snapshot_id}'s data ({row['item_count']:,} items, "
                            f"{row['combo_count']:,} combos) — a safety snapshot of today's current data "
                            "is taken automatically first, so this can itself be undone by restoring that "
                            "one afterward."
                        )
                        rc1, rc2 = st.columns([1, 1])
                        if rc1.button(
                            f"Confirm restore snapshot #{snapshot_id}", key=f"confirm_restore_btn_{snapshot_id}",
                            type="primary",
                        ):
                            safety_id = dept_mapping.restore_snapshot(ENGINE, snapshot_id, st.session_state["name"])
                            clear_snapshot_caches()
                            load_items.clear()
                            load_manual_items.clear()
                            load_dept_review_queue.clear()
                            load_broken_out_combos.clear()
                            load_decided_combos.clear()
                            load_combo_member_items.clear()
                            load_pending_upc_overrides.clear()
                            load_stale_sources.clear()
                            load_stale_sources_since_compute.clear()
                            load_source_pending_changes.clear()
                            load_item_master_pending.clear()
                            load_dept_pending_changes.clear()
                            load_dept_pending_upc_changes.clear()
                            st.session_state.pop(restore_key, None)
                            st.success(f"Restored snapshot #{snapshot_id} (today's prior data saved as snapshot #{safety_id}).")
                            st.rerun()
                        if rc2.button("Cancel", key=f"cancel_restore_btn_{snapshot_id}"):
                            st.session_state.pop(restore_key, None)
                            st.rerun()
