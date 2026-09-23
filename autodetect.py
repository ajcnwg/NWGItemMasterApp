"""
Generic "look at a sample file and guess the Sources form" helper — used by
the Add Source form's optional auto-fill step. Never applied automatically:
every guess is meant to be reviewed/edited by a person before saving, since
no heuristic here can know a source's real business rules (which fields
combine, whether a check digit needs dropping, etc.).

Four separable problems:
  1. Where's the real header row? (detect_header_row) — a file often has a
     title banner or a few blank rows above the actual column headers.
  2. Given real column headers, which one is UPC/Department/Category/
     Subcategory/Brand/Description? (guess_column_mapping) — keyword
     matching, generic across any distributor's naming conventions.
  3. What should this source be called? (guess_source_identity) — a
     source_key/label/file_keyword guess from the filename alone.
  4. Given real sample DATA (not just headers), are there any of script.py's
     per-source cleaning rules this file probably needs? (suggest_cleaning_
     rules) — e.g. a "_"/blank placeholder in Brand, an "OTHER"-style
     placeholder in Department, a low-cardinality short-code column that
     looks like a UOM/type/flag worth excluding certain values from, a
     leading numeric code on Category/Subcategory values. Every suggestion
     here is a starting guess for a person to confirm against the real
     values — never applied on its own.
"""

import re

import pandas as pd

# Ordered most-specific-first so e.g. "Subcategory" claims a "Sub Category"
# column before the plainer "Category" keywords get a chance to grab it.
FIELD_KEYWORDS = {
    "upc_column": ["upc", "gtin", "ean13", "ean", "item code", "sku"],
    "description_column": ["item description", "description", "desc"],
    "brand_column": ["brand desc", "brand name", "brand"],
    "subcategory_column": [
        "sub category", "subcategory", "sub-category",
        "sub group", "subgroup", "sub-group",
        "sub segment", "subsegment", "sub-segment",
        "segment",
    ],
    "category_column": ["category", "group desc", "group"],
    "department_column": ["department", "dept", "division", "class"],
}

FIELD_ORDER = [
    "upc_column", "description_column", "brand_column",
    "subcategory_column", "category_column", "department_column",
]


def _normalize(name) -> str:
    return re.sub(r"[_\-]+", " ", str(name).strip().lower())


def guess_column_mapping(columns: list) -> dict:
    """Best-effort guess at which raw column is which standard field.
    Never guesses a UPC-suffix column, check-digit stripping, or any
    combine/derived rule — those need a person's judgment. Returns
    {field_name: column_name_or_None}."""
    normalized = {c: _normalize(c) for c in columns}
    assigned = set()
    result = {}

    for field in FIELD_ORDER:
        keywords = FIELD_KEYWORDS[field]
        match = None

        # Pass 1: a column whose normalized name exactly equals a keyword.
        for kw in keywords:
            for col, norm in normalized.items():
                if col not in assigned and norm == kw:
                    match = col
                    break
            if match:
                break

        # Pass 2: fall back to a substring match.
        if not match:
            for kw in keywords:
                for col, norm in normalized.items():
                    if col not in assigned and kw in norm:
                        match = col
                        break
                if match:
                    break

        result[field] = match
        if match:
            assigned.add(match)

    return result


def list_sheet_names(uploaded_file) -> list:
    """[] for a CSV (no sheet concept); every sheet name otherwise."""
    filename = uploaded_file.name.lower()
    if filename.endswith(".csv"):
        return []
    uploaded_file.seek(0)
    engine = "pyxlsb" if filename.endswith(".xlsb") else "openpyxl"
    return pd.ExcelFile(uploaded_file, engine=engine).sheet_names


def detect_header_row(uploaded_file, sheet_name=None, max_scan_rows: int = 15) -> int:
    """Scans the first rows (read with no assumed header) and returns the
    1-based row number that looks most like real column headers: mostly
    non-blank, mostly unique, mostly non-numeric text — as opposed to a
    title-banner row (one long string, mostly blank) or a data row (lots of
    numbers, repeated values)."""
    filename = uploaded_file.name.lower()
    uploaded_file.seek(0)
    if filename.endswith(".csv"):
        raw = pd.read_csv(uploaded_file, header=None, dtype=str, nrows=max_scan_rows)
    else:
        engine = "pyxlsb" if filename.endswith(".xlsb") else "openpyxl"
        raw = pd.read_excel(
            uploaded_file, sheet_name=sheet_name or 0, header=None, dtype=str,
            nrows=max_scan_rows, engine=engine,
        )

    numeric_pattern = re.compile(r"^-?\d+(\.\d+)?$")

    def row_stats(i):
        values = raw.iloc[i].dropna().astype(str).str.strip()
        values = values[values != ""]
        if len(values) == 0:
            return 0, 0.0, 0.0
        unique_ratio = values.nunique() / len(values)
        numeric_ratio = values.apply(lambda v: bool(numeric_pattern.match(v))).mean()
        return len(values), unique_ratio, numeric_ratio

    stats = [row_stats(i) for i in range(len(raw))]

    # A "how label-like is this row on its own" score alone favors the
    # WIDEST label-like row, which can wrongly pick a title banner over the
    # real header (e.g. NWG's banner row has ~90 unique per-store metric
    # columns, wider than its own real 8-column header below it). So also
    # require the row right below to look like real data relative to this
    # one — a jump in numeric content — and weight by that jump.
    best_row, best_score = 0, -1.0
    for i in range(len(raw) - 1):
        length, unique_ratio, numeric_ratio = stats[i]
        if length == 0:
            continue
        _, _, next_numeric_ratio = stats[i + 1]
        own_score = length * unique_ratio * (1 - numeric_ratio)
        jump = max(next_numeric_ratio - numeric_ratio, 0.05)
        score = own_score * jump
        if score > best_score:
            best_score = score
            best_row = i

    return best_row + 1


def read_sample_columns(uploaded_file, sheet_name, header_row: int) -> list:
    """Real column headers once header_row/sheet_name are known — just
    enough rows to see the columns, not the whole file."""
    filename = uploaded_file.name.lower()
    uploaded_file.seek(0)
    header_index = max(header_row - 1, 0)
    if filename.endswith(".csv"):
        df = pd.read_csv(uploaded_file, header=header_index, dtype=str, nrows=5)
    else:
        engine = "pyxlsb" if filename.endswith(".xlsb") else "openpyxl"
        df = pd.read_excel(
            uploaded_file, sheet_name=sheet_name or 0, header=header_index, dtype=str,
            nrows=5, engine=engine,
        )
    return list(df.columns)


def sheet_has_minimum_info(uploaded_file, sheet_name) -> bool:
    """Quick per-sheet check — header + column mapping only, no full data
    sample — for whether a sheet looks like it's even worth defaulting to
    "selected" in a multi-sheet picker. UPC and Description are the two
    fields every source absolutely needs (UPC Column is a required form
    field); a sheet where neither can be guessed at all is very likely a
    summary/legend/pivot sheet, not real item-level data. Deliberately
    cheap (skips suggest_cleaning_rules' value-based sample-row scan)
    since this runs once per sheet just to set a checkbox default."""
    try:
        header_row = detect_header_row(uploaded_file, sheet_name)
        columns = read_sample_columns(uploaded_file, sheet_name, header_row)
        mapping = guess_column_mapping(columns)
        return bool(mapping["upc_column"]) and bool(mapping["description_column"])
    except Exception:
        return False


def read_sample_rows(uploaded_file, sheet_name, header_row: int, nrows: int = 1000) -> pd.DataFrame:
    """A bigger sample than read_sample_columns — enough real DATA (not just
    headers) for suggest_cleaning_rules' value-based heuristics to have
    something to look at."""
    filename = uploaded_file.name.lower()
    uploaded_file.seek(0)
    header_index = max(header_row - 1, 0)
    if filename.endswith(".csv"):
        return pd.read_csv(uploaded_file, header=header_index, dtype=str, nrows=nrows)
    engine = "pyxlsb" if filename.endswith(".xlsb") else "openpyxl"
    return pd.read_excel(
        uploaded_file, sheet_name=sheet_name or 0, header=header_index, dtype=str,
        nrows=nrows, engine=engine,
    )


# Filenames often lead with the distributor's own short code (KEHE, SPINS,
# NWG, URM, UNFI...) but not always as literally the first word (e.g.
# "Weekly URM Item List..."), so prefer the first ALL-CAPS-looking token
# over the first word if one exists nearby.
_DATE_TOKEN_PATTERN = re.compile(
    r"""
    \(?\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\)?   |  # 7.7.26, 07-09-26, (26we 07.09.26) partial
    \d{4}[-./]\d{1,2}[-./]\d{1,2}           |  # 2026-07-20
    \d{4}-\d{1,2}-\d{1,2}-\d{1,2}-\d{1,2}-\d{1,2}  # 2026-07-20-05-11-51 style timestamp
    """,
    re.VERBOSE,
)
_ACRONYM_PATTERN = re.compile(r"^[A-Z0-9&]{2,8}$")


def _cleaned_words(filename: str) -> list:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    stem = _DATE_TOKEN_PATTERN.sub(" ", stem)
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"[()]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return [w for w in stem.split(" ") if w]


def guess_source_identity(filename: str) -> dict:
    """Best-effort source_key/source_label/file_keyword guess from a
    filename alone — always meant to be reviewed, since there's no reliable
    way to know a distributor's preferred short name from a filename."""
    words = _cleaned_words(filename)
    acronym = next((w for w in words[:5] if _ACRONYM_PATTERN.match(w)), None)
    label_seed = acronym or (words[0] if words else filename)

    source_key = re.sub(r"[^a-z0-9]+", "_", label_seed.lower()).strip("_") or "new_source"

    return {
        "source_key": source_key,
        "source_label": label_seed,
        "file_keyword": label_seed,
    }


def guess_source_identities_batch(filenames: list) -> dict:
    """Like guess_source_identity, but for a BATCH of filenames analyzed
    together — disambiguates any that would otherwise get the identical
    guess. E.g. UNFI Natural's 3 separate per-warehouse files ("UNFI
    Natural Ridgefield WA DC Assortment Template...", "...Riverside CA
    DC...", "...Rocklin CA DC...") all lead with the same "UNFI" acronym,
    so guess_source_identity alone would give all three the same key. This
    finds words common to every filename in the batch (the noise — "UNFI",
    "Natural", "DC", "Assortment", "Template" here) and appends each file's
    own leftover distinguishing word (its warehouse city) to disambiguate.
    Returns {filename: {source_key, source_label, file_keyword}}."""
    base = {fn: guess_source_identity(fn) for fn in filenames}
    if len(filenames) <= 1:
        return base

    word_lists = {fn: [w.lower() for w in _cleaned_words(fn)] for fn in filenames}
    common_words = set(word_lists[filenames[0]])
    for fn in filenames[1:]:
        common_words &= set(word_lists[fn])

    by_key = {}
    for fn, ident in base.items():
        by_key.setdefault(ident["source_key"], []).append(fn)

    result = dict(base)
    for key, fns in by_key.items():
        if len(fns) <= 1:
            continue
        for i, fn in enumerate(fns):
            distinguishing = [w for w in word_lists[fn] if w not in common_words]
            suffix = distinguishing[0] if distinguishing else str(i + 1)
            new_key = re.sub(r"[^a-z0-9]+", "_", f"{key}_{suffix}").strip("_")
            result[fn] = {
                "source_key": new_key,
                "source_label": f"{base[fn]['source_label']} {suffix.title()}",
                "file_keyword": f"{base[fn]['file_keyword']} {suffix.title()}",
            }
    return result


# Common placeholder/sentinel text seen across real distributor files for
# "nothing meaningful here" — generic, not tied to any one source. Matched
# case-insensitively against real column values.
_BRAND_BLANK_PLACEHOLDERS = {"_", "-", "n/a", "na", "none", "unknown", "unk", "tbd"}
_DEPARTMENT_BLANK_PLACEHOLDERS = {
    "other", "unknown", "n/a", "na", "pending", "pending for assignment",
    "unassigned", "uncategorized", "tbd", "misc", "miscellaneous",
}
_PRIVATE_LABEL_MARKERS = {"pl", "private label", "store brand"}
_LEADING_CODE_PATTERN = re.compile(r"^\s*\d+\s+\S")


def suggest_cleaning_rules(sample_df: pd.DataFrame, mapping: dict) -> dict:
    """Data-driven suggestions for script.py-style per-source cleaning
    rules, generic across any source — looks at real sample VALUES (not
    just column names). Every value here is a starting guess; none of it
    is applied without a person confirming it matches what they actually
    want for this source."""
    suggestions = {
        "blank_brand_when_equals": None,
        "blank_department_when_equals": None,
        "brand_suffix_match": None,
        "brand_suffix_result": None,
        "dedup_deprioritize_brand_value": None,
        "exclude_column": None,
        "exclude_column_value_counts": None,
        "strip_leading_code_fields": None,
    }

    brand_col = mapping.get("brand_column")
    if brand_col and brand_col in sample_df.columns:
        counts = sample_df[brand_col].dropna().astype(str).str.strip()
        counts = counts[counts != ""].value_counts()
        for val in counts.index:
            if val.lower() in _BRAND_BLANK_PLACEHOLDERS:
                suggestions["blank_brand_when_equals"] = val
                break
        for val in counts.index:
            if val.lower() in _PRIVATE_LABEL_MARKERS:
                suggestions["brand_suffix_match"] = val
                suggestions["brand_suffix_result"] = val
                suggestions["dedup_deprioritize_brand_value"] = val
                break

    department_col = mapping.get("department_column")
    if department_col and department_col in sample_df.columns:
        counts = sample_df[department_col].dropna().astype(str).str.strip()
        counts = counts[counts != ""].value_counts()
        for val in counts.index:
            if val.lower() in _DEPARTMENT_BLANK_PLACEHOLDERS:
                suggestions["blank_department_when_equals"] = val
                break

    leading_code_fields = []
    for field_key, standard_name in [("category_column", "Category"), ("subcategory_column", "Subcategory")]:
        col = mapping.get(field_key)
        if col and col in sample_df.columns:
            values = sample_df[col].dropna().astype(str).str.strip()
            values = values[values != ""]
            if len(values) >= 5 and values.apply(lambda v: bool(_LEADING_CODE_PATTERN.match(v))).mean() > 0.5:
                leading_code_fields.append(standard_name)
    if leading_code_fields:
        suggestions["strip_leading_code_fields"] = ",".join(leading_code_fields)

    # A candidate exclude-column: some OTHER column (not already mapped to a
    # standard field) that's short, repeated TEXT codes — the shape of a
    # UOM/type/flag column, e.g. KEHE's UOM (EA/DS/PL/...), not a numeric
    # metric column (which can look low-cardinality by shape alone in a
    # sample). A name hint (contains "uom"/"type"/"status"/"flag"/"class")
    # is trusted over shape alone, since a real code column can have more
    # distinct values than a strict cardinality cap would otherwise allow.
    # Deliberately does NOT guess which values to exclude — that's a
    # judgment call the value-count preview is meant to inform, not this.
    numeric_pattern = re.compile(r"^-?\d+(\.\d+)?$")
    name_hints = ("uom", "unit of measure", "type", "flag", "status", "class", "code")
    mapped_columns = {v for v in mapping.values() if v}
    candidates = []
    for col in sample_df.columns:
        if col in mapped_columns:
            continue
        values = sample_df[col].dropna().astype(str).str.strip()
        values = values[values != ""]
        if len(values) < 5:
            continue
        if values.apply(lambda v: bool(numeric_pattern.match(v))).all():
            continue
        nunique = values.nunique()
        avg_len = values.str.len().mean()
        has_name_hint = any(hint in _normalize(col) for hint in name_hints)
        cardinality_ok = 2 <= nunique <= (40 if has_name_hint else 10)
        if cardinality_ok and avg_len <= 6:
            candidates.append((col, nunique, has_name_hint))

    best_col = None
    if candidates:
        hinted = [c for c in candidates if c[2]]
        pool = hinted if hinted else candidates
        best_col = min(pool, key=lambda c: c[1])[0]
    if best_col:
        suggestions["exclude_column"] = best_col
        suggestions["exclude_column_value_counts"] = (
            sample_df[best_col].dropna().astype(str).str.strip().value_counts().to_dict()
        )

    return suggestions


def analyze_source(uploaded_file, sheet_name, next_priority_rank: int = 100, identity: dict = None) -> dict:
    """Runs the whole pipeline for one (file, sheet) pair — header row,
    column mapping, identity guess, cleaning-rule suggestions — and returns
    one flat dict of every Sources-form field, ready to drop straight into
    a form's defaults. This is what makes each sheet independently
    analyzable: call it once per (file, sheet) a person selects, since
    different sheets in the same file (or different files entirely) can
    have completely different layouts. Pass `identity` (from
    guess_source_identities_batch) when analyzing multiple files together,
    so filenames that would otherwise guess the same source_key (e.g. UNFI
    Natural's 3 separate per-warehouse files) get disambiguated."""
    header_row = detect_header_row(uploaded_file, sheet_name)
    columns = read_sample_columns(uploaded_file, sheet_name, header_row)
    mapping = guess_column_mapping(columns)
    sample_rows = read_sample_rows(uploaded_file, sheet_name, header_row, nrows=2000)
    rules = suggest_cleaning_rules(sample_rows, mapping)
    identity = identity or guess_source_identity(uploaded_file.name)

    unmatched = [f.replace("_column", "") for f, v in mapping.items() if not v]

    return {
        "source_key": identity["source_key"],
        "source_label": identity["source_label"],
        "file_keyword": identity["file_keyword"],
        "priority_rank": next_priority_rank,
        "sheet_name": sheet_name or "",
        "header_row": header_row,
        "upc_column": mapping["upc_column"] or "",
        "department_column": mapping["department_column"] or "",
        "category_column": mapping["category_column"] or "",
        "subcategory_column": mapping["subcategory_column"] or "",
        "brand_column": mapping["brand_column"] or "",
        "description_column": mapping["description_column"] or "",
        "exclude_column": rules["exclude_column"] or "",
        "exclude_column_value_counts": rules["exclude_column_value_counts"],
        "exclude_values": "",
        "blank_brand_when_equals": rules["blank_brand_when_equals"] or "",
        "blank_department_when_equals": rules["blank_department_when_equals"] or "",
        "brand_suffix_match": rules["brand_suffix_match"] or "",
        "brand_suffix_result": rules["brand_suffix_result"] or "",
        "dedup_deprioritize_brand_value": rules["dedup_deprioritize_brand_value"] or "",
        "strip_leading_code_fields": rules["strip_leading_code_fields"] or "",
        "detected_columns": columns,
        "unmatched_fields": unmatched,
        "source_filename": uploaded_file.name,
    }
