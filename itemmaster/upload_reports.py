"""Upload reports — one per upload event (the starting baseline, a monthly run
of every file by the script, or a person's upload in the app), each keeping
what that upload added and what dropped out of each source's file.

Kept for a year (a source's latest report is kept however old it is). Only
each source's latest report can be acted on — older ones describe an item
master that has moved on, so they're history to look at.

Also here: the duplicate-UPC review — pairs of items whose UPCs are the same
digits apart from one leading digit (e.g. 1029100797 / 81029100797), judged
from every source's description, brand and size, and pairs a person said
aren't duplicates.
"""
import re

import pandas as pd
from sqlalchemy import text

REPORT_DDL = [
    """IF OBJECT_ID('dbo.upload_reports') IS NULL
    CREATE TABLE dbo.upload_reports (
        report_id INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_upload_reports PRIMARY KEY,
        kind VARCHAR(20) NOT NULL,              -- baseline | auto | manual
        created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        created_by NVARCHAR(100) NULL,
        title NVARCHAR(400) NULL
    )""",
    """IF OBJECT_ID('dbo.upload_report_sources') IS NULL
    CREATE TABLE dbo.upload_report_sources (
        report_id INT NOT NULL,
        source_key NVARCHAR(50) NOT NULL,
        filename NVARCHAR(400) NULL,
        ingestion_log_id INT NULL,
        rows_kept INT NULL,
        n_added INT NOT NULL DEFAULT 0,
        n_removed INT NOT NULL DEFAULT 0,
        CONSTRAINT PK_upload_report_sources PRIMARY KEY (report_id, source_key)
    )""",
    """IF OBJECT_ID('dbo.upload_report_items') IS NULL
    CREATE TABLE dbo.upload_report_items (
        report_id INT NOT NULL,
        source_key NVARCHAR(50) NOT NULL,
        upc VARCHAR(20) NOT NULL,
        kind CHAR(1) NOT NULL,                   -- A = new to the item master, R = dropped out of the file
        description NVARCHAR(500) NULL,
        brand NVARCHAR(200) NULL,
        department NVARCHAR(200) NULL,
        CONSTRAINT PK_upload_report_items PRIMARY KEY (report_id, source_key, kind, upc)
    )""",
    # (what a source's file listed just before an upload replaces it — kept for
    # the moment of the upload only; a #temp table doesn't outlive a
    # parameterized statement, so this is a real table)
    """IF OBJECT_ID('dbo.upload_report_prev') IS NULL
    CREATE TABLE dbo.upload_report_prev (
        report_id INT NOT NULL,
        source_key NVARCHAR(50) NOT NULL,
        upc VARCHAR(20) NOT NULL,
        description NVARCHAR(500) NULL,
        brand NVARCHAR(200) NULL,
        department NVARCHAR(200) NULL,
        CONSTRAINT PK_upload_report_prev PRIMARY KEY (report_id, source_key, upc)
    )""",
    """IF OBJECT_ID('dbo.dup_not_duplicate') IS NULL
    CREATE TABLE dbo.dup_not_duplicate (
        upc_a VARCHAR(20) NOT NULL,
        upc_b VARCHAR(20) NOT NULL,
        decided_by NVARCHAR(100) NULL,
        decided_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_dup_not_duplicate PRIMARY KEY (upc_a, upc_b)
    )""",
    # A combined item: deleted, and remembered as part of the one kept — a
    # file that still lists it never brings it back (see mark_deleted_returning).
    """IF COL_LENGTH('dbo.deleted_upcs', 'combined_into') IS NULL
        ALTER TABLE dbo.deleted_upcs ADD combined_into VARCHAR(20) NULL""",
    # which source a deleted item came from, so restoring it puts that back too
    """IF COL_LENGTH('dbo.deleted_upcs', 'source_key') IS NULL
        ALTER TABLE dbo.deleted_upcs ADD source_key VARCHAR(50) NULL""",
    # the deleted item's place in its Department Review group(s), put back on restore
    """IF COL_LENGTH('dbo.deleted_upcs', 'dept_state') IS NULL
        ALTER TABLE dbo.deleted_upcs ADD dept_state NVARCHAR(MAX) NULL""",
    """IF COL_LENGTH('dbo.item_master_pending_changes', 'combined_into') IS NULL
        ALTER TABLE dbo.item_master_pending_changes ADD combined_into VARCHAR(20) NULL""",
]
KEEP_DAYS = 365
TABLES = ["upload_reports", "upload_report_sources", "upload_report_items", "dup_not_duplicate"]


_ENSURED = set()


def ensure_tables(conn) -> None:
    """Creates the tables / columns the first time (checked once per process
    per database)."""
    key = str(conn.engine.url)
    if key in _ENSURED:
        return
    # On its own connection (committed straight away, whatever the caller's
    # transaction does), and "already there" is fine — two app sessions
    # starting at once can both try to create a table.
    with conn.engine.begin() as own:
        for ddl in REPORT_DDL:
            try:
                own.execute(text(ddl))
            except Exception as e:  # noqa: BLE001
                if "2714" not in str(e) and "already" not in str(e).lower():
                    raise
    _ENSURED.add(key)


def ensure_baseline(conn, actor: str = None) -> None:
    """The first report: the item master as it stands before any report was
    kept — every item, under the source it came from."""
    ensure_tables(conn)
    if conn.execute(text("SELECT TOP 1 1 FROM dbo.upload_reports")).first():
        return
    rid = conn.execute(text(
        "INSERT INTO dbo.upload_reports (kind, created_by, title, created_at) OUTPUT inserted.report_id "
        "VALUES ('baseline', :by, 'Baseline — the starting item master', "
        "COALESCE((SELECT MIN(uploaded_at) FROM dbo.source_raw_uploads), SYSUTCDATETIME()))"),
        {"by": actor}).scalar()
    conn.execute(text(
        "INSERT INTO dbo.upload_report_items (report_id, source_key, upc, kind, description, brand, department) "
        "SELECT :r, COALESCE(NULLIF(source_key, ''), '(added by hand)'), upc, 'A', LEFT(description, 500), brand, department "
        "FROM dbo.items"), {"r": rid})
    conn.execute(text(
        "INSERT INTO dbo.upload_report_sources (report_id, source_key, filename, rows_kept, n_added) "
        "SELECT :r, a.source_key, u.filename, (SELECT COUNT(*) FROM dbo.raw_items r WHERE r.source_key = a.source_key), a.n "
        "FROM (SELECT source_key, COUNT(*) AS n FROM dbo.upload_report_items WHERE report_id = :r GROUP BY source_key) a "
        "LEFT JOIN dbo.source_raw_uploads u ON u.source_key = a.source_key"), {"r": rid})


def start_report(conn, kind: str, actor: str, title: str = None) -> int:
    """A new report for an upload that's about to be saved (kind auto or
    manual); prunes reports over a year old (keeping each source's latest)."""
    ensure_baseline(conn, actor)
    rid = conn.execute(text(
        "INSERT INTO dbo.upload_reports (kind, created_by, title) OUTPUT inserted.report_id VALUES (:k, :by, :t)"),
        {"k": kind, "by": actor, "t": title}).scalar()
    prune(conn)
    return rid


def prune(conn) -> int:
    old = [r[0] for r in conn.execute(text(
        "SELECT report_id FROM dbo.upload_reports WHERE created_at < DATEADD(day, -:d, SYSUTCDATETIME()) "
        "AND report_id NOT IN (SELECT MAX(report_id) FROM dbo.upload_report_sources GROUP BY source_key)"),
        {"d": KEEP_DAYS}).all()]
    for rid in old:
        for t in ("upload_report_items", "upload_report_sources", "upload_reports"):
            conn.execute(text(f"DELETE FROM dbo.{t} WHERE report_id = :r"), {"r": rid})
    return len(old)


def capture_before(conn, report_id: int, source_key: str) -> None:
    """Before a source's rows are replaced: keep what its file listed, to see
    what the new file drops."""
    p = {"r": report_id, "sk": source_key}
    conn.execute(text("DELETE FROM dbo.upload_report_prev WHERE report_id = :r AND source_key = :sk"), p)
    conn.execute(text(
        "INSERT INTO dbo.upload_report_prev (report_id, source_key, upc, description, brand, department) "
        "SELECT :r, :sk, upc, MAX(LEFT(description, 500)), MAX(brand), MAX(department) "
        "FROM dbo.raw_items WHERE source_key = :sk GROUP BY upc"), p)


def record_after(conn, report_id: int, source_key: str, filename: str, log_id: int, rows_kept: int) -> dict:
    """After a source's rows were replaced: what the new file adds (UPCs the
    item master doesn't have, and that weren't deleted) and what it drops."""
    p = {"r": report_id, "sk": source_key}
    conn.execute(text(
        "INSERT INTO dbo.upload_report_items (report_id, source_key, upc, kind, description, brand, department) "
        "SELECT :r, :sk, p.upc, 'R', p.description, p.brand, p.department FROM dbo.upload_report_prev p "
        "WHERE p.report_id = :r AND p.source_key = :sk AND NOT EXISTS (SELECT 1 FROM dbo.raw_items r WHERE r.source_key = :sk AND r.upc = p.upc)"), p)
    conn.execute(text(
        "INSERT INTO dbo.upload_report_items (report_id, source_key, upc, kind, description, brand, department) "
        "SELECT :r, :sk, r.upc, 'A', MAX(LEFT(r.description, 500)), MAX(r.brand), MAX(r.department) FROM dbo.raw_items r "
        "WHERE r.source_key = :sk AND NOT EXISTS (SELECT 1 FROM dbo.items i WHERE i.upc = r.upc) "
        "AND NOT EXISTS (SELECT 1 FROM dbo.deleted_upcs d WHERE d.upc = r.upc) GROUP BY r.upc"), p)
    n = dict(conn.execute(text(
        "SELECT kind, COUNT(*) FROM dbo.upload_report_items WHERE report_id = :r AND source_key = :sk GROUP BY kind"), p).all())
    conn.execute(text(
        "MERGE dbo.upload_report_sources AS t USING (SELECT :r AS report_id, :sk AS source_key) AS s "
        "ON t.report_id = s.report_id AND t.source_key = s.source_key "
        "WHEN MATCHED THEN UPDATE SET filename = :f, ingestion_log_id = :l, rows_kept = :k, n_added = :a, n_removed = :rm "
        "WHEN NOT MATCHED THEN INSERT (report_id, source_key, filename, ingestion_log_id, rows_kept, n_added, n_removed) "
        "VALUES (:r, :sk, :f, :l, :k, :a, :rm);"),
        {**p, "f": filename, "l": log_id, "k": rows_kept, "a": n.get("A", 0), "rm": n.get("R", 0)})
    conn.execute(text("DELETE FROM dbo.upload_report_prev WHERE report_id = :r AND source_key = :sk"), p)
    return {"added": n.get("A", 0), "removed": n.get("R", 0)}


def list_reports(engine) -> pd.DataFrame:
    """Every report kept, newest first, with its sources and totals."""
    with engine.begin() as conn:
        ensure_baseline(conn)
        return pd.read_sql(text(
            "SELECT r.report_id, r.kind, r.created_at, r.created_by, r.title, s.source_key, s.filename, s.rows_kept, "
            "s.n_added, s.n_removed FROM dbo.upload_reports r LEFT JOIN dbo.upload_report_sources s ON s.report_id = r.report_id "
            "ORDER BY r.report_id DESC, s.source_key"), conn)


def current_report_ids(reports: pd.DataFrame) -> dict:
    """{source: the id of its latest report} — the only ones that can be acted on."""
    s = reports.dropna(subset=["source_key"])
    return s.groupby("source_key")["report_id"].max().astype(int).to_dict() if not s.empty else {}


def report_items(engine, report_id: int, source_key: str = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(
            "SELECT source_key, upc, kind, description, brand, department FROM dbo.upload_report_items "
            "WHERE report_id = :r" + (" AND source_key = :sk" if source_key else "") + " ORDER BY kind, upc"),
            conn, params={"r": report_id, "sk": source_key})


# ---------------------------------------------------------------------------
# Possible duplicate UPCs
# ---------------------------------------------------------------------------
_STOP = {"oz", "ct", "pk", "fz", "fl", "lb", "the", "and", "with", "of", "gr", "ea", "pc", "cs", "nan", "none"}
_UNIT = {  # → (kind, factor to ounces / a count)
    "oz": ("oz", 1), "ounce": ("oz", 1), "ounces": ("oz", 1), "fz": ("oz", 1), "fo": ("oz", 1), "floz": ("oz", 1),
    "fl": ("oz", 1), "z": ("oz", 1), "lb": ("oz", 16), "lbs": ("oz", 16), "pound": ("oz", 16),
    "g": ("oz", 1 / 28.3495), "gr": ("oz", 1 / 28.3495), "gm": ("oz", 1 / 28.3495), "kg": ("oz", 35.274),
    "ml": ("oz", 1 / 29.5735), "l": ("oz", 33.814), "lt": ("oz", 33.814), "ltr": ("oz", 33.814),
    "liter": ("oz", 33.814), "gal": ("oz", 128), "qt": ("oz", 32), "pt": ("oz", 16),
    "ct": ("ct", 1), "count": ("ct", 1), "pk": ("ct", 1), "pc": ("ct", 1), "pcs": ("ct", 1), "ea": ("ct", 1),
}
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(fl\.?\s*oz|[a-z]+)\b")


def _words(t) -> set:
    return {w for w in re.findall(r"[a-z]+", str(t or "").lower()) if len(w) > 2 and w not in _STOP}


def _size(value, unit):
    """(kind, amount) — "oz" for weights and volumes (in ounces), "ct" for counts — or None."""
    unit = re.sub(r"[^a-z]", "", str(unit or "").lower())
    if unit not in _UNIT:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not v == v or v <= 0:
        return None
    kind, f = _UNIT[unit]
    return kind, round(v * f, 2)


def _sizes(description, size, uom) -> set:
    """Every size a file gives an item: its Size + UOM, and any in its description."""
    out = {_size(n, unit) for n, unit in _SIZE.findall(str(description or "").lower())}
    out.add(_size(str(size or "").strip(), uom))
    return {x for x in out if x}


def _size_match(a: set, b: set):
    """(match, conflict, matched): sizes of the same kind within 10% → match;
    same kind given on both but none close → conflict."""
    matched, common = [], False
    for ka, va in a:
        for kb, vb in b:
            if ka == kb:
                common = True
                if abs(va - vb) <= 0.1 * max(va, vb):
                    matched.append((ka, va))
    return bool(matched), common and not matched, matched


def _fmt_size(kind, v) -> str:
    return f"{v:g} oz" if kind == "oz" else f"{v:g} ct"


def _clean(v) -> str:
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in ("nan", "none") else s


VERDICTS = ["Same item", "Likely same", "Unsure", "Different", "Placeholder code"]


def judge_pairs(engine, pairs: pd.DataFrame) -> pd.DataFrame:
    """Adds Verdict (see VERDICTS) and each UPC's listing in every source's
    file ("Files (shorter)/(longer)"), judged on all of them: the same size
    and brand with the same words → Same item; a different size, or nothing
    in common → Different; codes made of 9s/0s → Placeholder code."""
    if pairs.empty:
        return pairs.assign(Verdict=pd.Series(dtype=str))
    ups = sorted(set(pairs["UPC (shorter)"]) | set(pairs["UPC (longer)"]))
    with engine.connect() as conn:
        raw = pd.read_sql(text(
            "SELECT source_key, upc, description, brand, size, uom FROM dbo.raw_items "
            "WHERE upc IN (SELECT v FROM OPENJSON(:u) WITH (v VARCHAR(40) '$'))"), conn,
            params={"u": pd.Series(ups).to_json(orient="values")})
    info = {}
    for u, g in raw.groupby("upc"):
        info[u] = {
            "w": set().union(*[_words(f"{r.description} {r.brand}") for r in g.itertuples()]),
            "b": set().union(*[_words(r.brand) for r in g.itertuples()]),
            "s": set().union(*[_sizes(r.description, r.size, r.uom) for r in g.itertuples()]),
            # each file's own line: (source, description, brand, size) — for "What the files say"
            "lines": sorted({(r.source_key, _clean(r.description), _clean(r.brand),
                              (_clean(r.size) + " " + _clean(r.uom)).strip()) for r in g.itertuples()}),
        }
        info[u]["files"] = " · ".join(f"{k}: {d}" for k, d, _, _ in info[u]["lines"])
    empty = {"w": set(), "b": set(), "s": set(), "files": "(no current file lists it)", "lines": []}

    def verdict(sh, lg, d_sh, d_lg, b_sh, b_lg):
        """(verdict, why) — why in plain words, from what the files say."""
        if any(set(x) <= {"9", "0"} and "99999" in x for x in (sh, lg)):
            return "Placeholder code", "A made-up code (all 9s or 0s), not a real product's UPC."
        a, b = info.get(sh, empty), info.get(lg, empty)
        wa, wb = a["w"] | _words(f"{d_sh} {b_sh}"), b["w"] | _words(f"{d_lg} {b_lg}")
        shared, fewest = len(wa & wb), max(1, min(len(wa), len(wb)))
        ov = shared / fewest
        brand_shared = bool(a["b"] & wb) or bool(b["b"] & wa)
        brand_ok = brand_shared or not a["b"] or not b["b"]
        size_ok, size_conflict, matched = _size_match(a["s"], b["s"])
        words = f"{shared} of {fewest} words match"
        if size_conflict:
            show = lambda ss: ", ".join(sorted({_fmt_size(k, v) for k, v in ss}))
            return "Different", f"Different sizes in the files: {show(a['s'])} vs {show(b['s'])}."
        one = a["s"] or b["s"]
        size_txt = (f"same size ({', '.join(sorted({_fmt_size(k, v) for k, v in matched}))})" if size_ok
                    else f"size {', '.join(sorted({_fmt_size(k, v) for k, v in one}))} on one, none on the other" if one
                    else "no size on either")
        brand_txt = ("same brand" if brand_shared else "different brand" if not brand_ok else "brand on only one")
        why = f"{size_txt[:1].upper() + size_txt[1:]}, {brand_txt}, {words}."
        if ov >= 0.5 and brand_ok and size_ok:
            return "Same item", why
        if (ov >= 0.34 and brand_ok) or (size_ok and (ov >= 0.2 or brand_shared)):
            return "Likely same", why
        if ov < 0.2 and not size_ok and not brand_shared:
            return "Different", f"Little in common: {words}" + ("" if brand_ok else ", different brand") + "."
        return "Unsure", why

    out = pairs.copy()
    judged = [verdict(r["UPC (shorter)"], r["UPC (longer)"], r["Description (shorter)"], r["Description (longer)"],
                      r["Brand (shorter)"], r["Brand (longer)"]) for _, r in pairs.iterrows()]
    out["Verdict"] = [v for v, _ in judged]
    out["Why"] = [w for _, w in judged]
    out["Files (shorter)"] = out["UPC (shorter)"].map(lambda u: info.get(u, empty)["files"])
    out["Files (longer)"] = out["UPC (longer)"].map(lambda u: info.get(u, empty)["files"])
    out["Lines (shorter)"] = out["UPC (shorter)"].map(lambda u: info.get(u, empty)["lines"])
    out["Lines (longer)"] = out["UPC (longer)"].map(lambda u: info.get(u, empty)["lines"])
    return out


def not_duplicates(engine) -> set:
    """{(shorter, longer)} pairs a person said aren't the same item."""
    with engine.begin() as conn:
        ensure_tables(conn)
        return {(a, b) for a, b in conn.execute(text("SELECT upc_a, upc_b FROM dbo.dup_not_duplicate")).all()}


def mark_not_duplicate(engine, pairs: list, actor: str) -> None:
    with engine.begin() as conn:
        ensure_tables(conn)
        for a, b in pairs:
            conn.execute(text(
                "IF NOT EXISTS (SELECT 1 FROM dbo.dup_not_duplicate WHERE upc_a = :a AND upc_b = :b) "
                "INSERT INTO dbo.dup_not_duplicate (upc_a, upc_b, decided_by) VALUES (:a, :b, :by)"),
                {"a": a, "b": b, "by": actor})


def undo_not_duplicate(engine, a: str, b: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.dup_not_duplicate WHERE upc_a = :a AND upc_b = :b"), {"a": a, "b": b})
