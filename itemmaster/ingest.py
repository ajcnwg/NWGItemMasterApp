"""
Generic per-source ingestion: reads an uploaded distributor file according to
a `sources` row's configuration (sheet name, header row, column mapping),
cleans the UPC, and returns a standardized DataFrame ready to stage into
raw_items.

The UPC-cleaning idiom (strip non-digits, drop N trailing digits for a
check-digit source, keep at most the last 12) mirrors generic_clean_upc /
clean_upc in script.py (BRdata PowerBI merge tool) — ported here so a new
source configured entirely through the Sources tab never needs new Python,
matching that script's own "Data Source Definitions" self-service design.
One deliberate difference from script.py: leading zeros are stripped from
the final result instead of zero-padding to a fixed 12 digits.
"""

import io
import re

import pandas as pd
from sqlalchemy import text

INVALID_UPC = None
STANDARD_FIELDS = ["Department", "Category", "Subcategory", "Brand", "Description"]

REASON_EXPLANATIONS = {
    "invalid_upc": (
        "No usable digits were found in this row's UPC column (it was blank, "
        "text, or contained no digits at all after cleaning). This row was "
        "NOT staged. To fix: correct the UPC in next month's source file, or "
        "if this is a real item, add it manually on the Add Item tab once you "
        "know its correct UPC."
    ),
    "duplicate_upc": (
        "This UPC appeared more than once in the uploaded file for this "
        "source. Only the first occurrence was kept — this row was dropped. "
        "If these are genuinely two different items, the source file has a "
        "UPC data error that needs fixing at the distributor's end; if they're "
        "the same item listed twice, this is expected and no action is needed."
    ),
    "excluded_value": (
        "This row's value in the source's configured Exclude Column matched one "
        "of its Exclude Values (e.g. a display or pallet item, not a sellable "
        "unit) — deliberately dropped by that source's own configuration, not "
        "an error. Change the Exclude Values on the Sources tab if this "
        "shouldn't be excluded."
    ),
}


def strip_leading_code(value: str) -> str:
    """URM's Group/Subgroup values are prefixed with a numeric code and a
    space (e.g. '001 SALAD DRESSING...'). Strip that leading code + the
    space right after it, regardless of how many digits it has. Ported
    from script.py's strip_leading_code."""
    return re.sub(r"^\s*\d+\s+", "", value)


def _strip_float_artifact(text: str) -> str:
    """Excel often stores a numeric-looking cell (GTIN, check digit, plain
    UPC) as a float, so pandas can hand back "36800445154.0" even when read
    with dtype=str. Drop a trailing ".0" before digit-stripping so it isn't
    mistaken for a real trailing zero."""
    return re.sub(r"\.0$", "", text)


def clean_upc(value) -> str | None:
    if value is None:
        return INVALID_UPC
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return INVALID_UPC
    if re.search(r"[A-Za-z]", text):
        return INVALID_UPC  # letters mean it isn't a UPC (a typo, or Excel's "7.06E+11")
    digits = re.sub(r"\D", "", _strip_float_artifact(text))
    if not digits:
        return INVALID_UPC
    digits = digits[-12:].lstrip("0")
    return digits or INVALID_UPC


def invalid_upc_reason(value) -> str:
    """Why clean_upc rejected a value, in a few words."""
    text = "" if value is None else str(value).strip()
    if not text or text.lower() == "nan":
        return "it's blank"
    if re.search(r"[A-Za-z]", text):
        return "UPCs are digits only"
    if not re.sub(r"\D", "", text):
        return "it has no digits"
    return "it's all zeros"


# A UPC Excel has turned into scientific notation ("7.06128E+11", "7,06E+11").
SCI_UPC = r"\s*\d+([.,]\d+)?[eE][+-]?\d+\s*"


def generic_clean_upc(value, strip_trailing_digits: int = 0) -> str | None:
    if value is None:
        return INVALID_UPC
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return INVALID_UPC
    if re.search(r"[A-Za-z]", text):
        return INVALID_UPC  # same rule as clean_upc: letters mean it isn't a UPC
    digits = re.sub(r"\D", "", _strip_float_artifact(text))
    if strip_trailing_digits:
        digits = digits[:-strip_trailing_digits] if len(digits) > strip_trailing_digits else ""
    return clean_upc(digits)


SIZE_FORMATS = {
    "plain": "Plain value(s) — Pack/Size/UOM columns are used as-is",
    "size_uom_combined": "Size + Unit combined in Size Column (e.g. \"16OZ\", \"1.5 LB\")",
    "pack_size_uom_combined": "Pack + Size + Unit combined in Size Column (e.g. \"12/16OZ\")",
}

# A number with or without a leading digit before the decimal point —
# real distributor files write both "0.7 OZ" and ".7 OZ" for the same
# thing (confirmed in URM's own data).
_NUM = r"(?:\d+(?:[.,]\d+)?|[.,]\d+)"
# Leading number, an optional space AND/OR a single separator character
# (URM's own data uses a bare hyphen in place of a space — "6-OZ",
# "1-GAL" — with no second number following it, unlike the pack pattern
# below), then whatever text is left over as the unit — covers the large
# majority of real distributor size text ("16OZ", "16 OZ", "1.5 LB",
# "750ML", "2 GAL", ".7 OZ", "6-OZ").
_SIZE_UOM_PATTERN = re.compile(rf"^\s*({_NUM})\s*[-/]?\s*(.*)$")
# Same idea, plus a leading whole-number count and a "/" or "-" separator
# for a combined pack count ("12/16OZ" -> pack "12", size "16", unit
# "OZ"; real distributor files mix both separators for the same thing,
# e.g. URM's own file has both "10/16OZ" and "4-.7 OZ").
_PACK_SIZE_UOM_PATTERN = re.compile(rf"^\s*(\d+)\s*[/-]\s*({_NUM})\s*(.*)$")
# A value with no digits at all ("EACH", "LARGE", "GALLON") isn't a size
# at all — it's describing a unit/variant with nothing to measure, so it
# belongs in UOM, not Size.
_NO_DIGITS_PATTERN = re.compile(r"^[^\d]+$")

# Common real-world abbreviations/typos seen across distributor files
# (confirmed against URM's own real data: "OZ" truncated to "Z" is
# widespread — "3.2Z", "6.75Z" — and "#" is a common shorthand for
# pounds). Applied case-insensitively to the extracted UOM only, after
# splitting, never to the whole raw value — so this can't accidentally
# eat part of a real size number.
DEFAULT_UOM_ALIASES = {"Z": "OZ", "#": "LB", "0Z": "OZ", "CT.": "CT"}


def _parse_uom_aliases(uom_aliases: str = None) -> dict:
    """Parses a source's own 'UOM_from=UOM_to, ...' config string (e.g.
    'QRT=QT, GALLON=GAL') on top of the built-in defaults — a source can
    override a default (e.g. map 'Z' to something other than 'OZ') by
    listing its own 'Z=...' entry, since dict update lets later entries win."""
    aliases = dict(DEFAULT_UOM_ALIASES)
    if uom_aliases:
        for pair in uom_aliases.split(","):
            pair = pair.strip()
            if "=" in pair:
                frm, to = pair.split("=", 1)
                frm, to = frm.strip(), to.strip()
                if frm:
                    aliases[frm.upper()] = to
    return aliases


def _clean_uom(value: str, aliases: dict) -> str:
    if not value:
        return value
    # A stray leading period or space ahead of the real unit (". LB",
    # ".OZ") is common enough in real files to strip unconditionally,
    # rather than needing a per-source alias for every such case.
    value = value.strip().lstrip(". ").strip()
    return aliases.get(value.upper(), value)


def split_pack_size_uom(values: pd.Series, size_format: str, uom_aliases: str = None) -> tuple:
    """Splits a combined Size column into (Pack, Size, UOM) Series
    according to size_format ('size_uom_combined' or
    'pack_size_uom_combined'). Never raises and never drops data: a value
    that doesn't match any recognized shape falls back to the WHOLE
    original value in Size, with Pack/UOM left blank for that row — a
    source is never blocked by one weird row's format, and anything a
    generic pattern gets wrong is still fixable by hand afterward on
    Item Master / UPC Overrides.

    Row-by-row rather than a single vectorized regex, because the real
    decision here isn't one fixed shape — a leading "1/2 OZ" is a genuine
    fraction (half an ounce), never a "1-pack of 2 OZ", while "10/16OZ" is
    genuinely a pack of 10 — confirmed against URM's own real data, where
    every embedded-fraction case has a leading 1 and every embedded-pack
    case has a leading count of 2 or more. That distinction has to be made
    per-value, not by one global pattern."""
    aliases = _parse_uom_aliases(uom_aliases)
    packs, sizes, uoms = [], [], []
    for raw in values:
        text_val = "" if pd.isna(raw) else str(raw).strip()
        pack, size, uom = "", text_val, ""

        if not text_val:
            pass
        elif _NO_DIGITS_PATTERN.match(text_val):
            # No digits anywhere — "EACH", "LARGE", "GALLON" — it's a
            # descriptor, not a measurement. Goes to UOM, Size stays blank.
            size, uom = "", text_val
        elif size_format == "pack_size_uom_combined":
            m = _PACK_SIZE_UOM_PATTERN.match(text_val)
            if m and int(m.group(1)) >= 2:
                pack, size, uom = m.group(1), m.group(2), m.group(3)
            elif m:
                # Leading count of 1 ("1/2 OZ") is a genuine fraction, not
                # a 1-pack — compute the actual decimal size instead of
                # naively re-parsing the original text (which would just
                # re-match the leading "1" and leave a garbage "/2 OZ" uom).
                try:
                    size = str(1 / float(m.group(2).replace(",", ".")))
                except (ValueError, ZeroDivisionError):
                    size = m.group(2)
                uom = m.group(3)
            else:
                # No separator at all — fall through to the plain
                # size+uom pattern against the whole value instead.
                m2 = _SIZE_UOM_PATTERN.match(text_val)
                if m2:
                    size, uom = m2.group(1), m2.group(2)
        else:
            m = _SIZE_UOM_PATTERN.match(text_val)
            if m:
                size, uom = m.group(1), m.group(2)

        packs.append(pack)
        sizes.append(size)
        uoms.append(_clean_uom(uom, aliases))

    index = values.index
    return pd.Series(packs, index=index), pd.Series(sizes, index=index), pd.Series(uoms, index=index)


class FileProblem(ValueError):
    """A file that can't be taken as it is. The message says, in plain words,
    what's wrong with it and what to do — it's shown to people as-is.
    `detail` is optional supporting information (e.g. the columns the file does
    have), shown smaller underneath."""

    def __init__(self, message: str, detail: str = None):
        super().__init__(message)
        self.detail = detail


# Each column a source's settings can name, and what people call it.
MAPPED_COLUMNS = {
    "upc_column": "UPC", "upc_suffix_column": "UPC suffix (check digit)", "department_column": "Department",
    "category_column": "Category", "subcategory_column": "Subcategory", "brand_column": "Brand",
    "description_column": "Description", "pack_column": "Pack", "size_column": "Size", "uom_column": "UOM",
    "exclude_column": "Exclude",
}


def missing_columns(raw_df: pd.DataFrame, source: dict) -> list:
    """[(what, column name)] for every column the source's settings name that
    the file doesn't have."""
    have = set(raw_df.columns)
    out = []
    for key, what in MAPPED_COLUMNS.items():
        col = source.get(key)
        if isinstance(col, str) and col.strip() and col not in have:
            out.append((what, col))
    return out


def _source_name(source: dict) -> str:
    return source.get("source_label") or source.get("source_key") or "this source"


def _read_problem(e: Exception, uploaded_file, source: dict) -> FileProblem:
    """A plain-words FileProblem for whatever went wrong reading a file."""
    name = getattr(uploaded_file, "name", "The file")
    msg = str(e)
    kind = type(e).__name__
    if kind in ("BadZipFile", "InvalidFileException") or "zip file" in msg.lower():
        return FileProblem(f"{name} isn't a readable Excel file — it may be damaged, still open/locked, or a "
                           "different kind of file renamed to .xlsx. Open it in Excel, save it as .xlsx, and upload it again.")
    if "Worksheet" in msg and "not found" in msg or ("sheet" in msg.lower() and "not found" in msg.lower()):
        try:
            if hasattr(uploaded_file, "seek"):
                uploaded_file.seek(0)
            sheets = pd.ExcelFile(uploaded_file).sheet_names
            have = f" Its sheets are: {', '.join(sheets)}."
        except Exception:
            have = ""
        return FileProblem(f"{name} has no sheet named “{source.get('sheet_name')}”, which {_source_name(source)}'s "
                           f"settings read from.{have} Check it's the right file, or fix Sheet Name on the Sources tab.")
    if kind == "EmptyDataError":
        return FileProblem(f"{name} is empty — there's nothing in it to read.")
    if kind == "UnicodeDecodeError":
        return FileProblem(f"{name} uses a text encoding that couldn't be read. Save it from Excel as "
                           "“CSV UTF-8” or as .xlsx, and upload it again.")
    if kind == "ParserError":
        return FileProblem(f"{name} isn't a well-formed CSV (the rows don't line up into columns). "
                           "Open it in Excel, save it as .xlsx, and upload it again.")
    return FileProblem(f"{name} couldn't be read ({msg[:200]}). Check it's the right file for {_source_name(source)}.")


def read_raw_file(uploaded_file, source: dict) -> pd.DataFrame:
    """Reads the uploaded file into a DataFrame with original column headers,
    using the source's configured sheet name and header row. Anything that
    stops it being read raises FileProblem with a plain-words reason."""
    name = getattr(uploaded_file, "name", "") or ""
    if not name.lower().endswith((".csv", ".xlsx", ".xls", ".xlsb")):
        raise FileProblem(f"{name or 'That file'} isn't a spreadsheet — upload an .xlsx, .xls, .xlsb or .csv file.")
    try:
        df = _read_raw_file(uploaded_file, source)
    except FileProblem:
        raise
    except Exception as e:
        raise _read_problem(e, uploaded_file, source) from e
    if df.empty or df.dropna(how="all").empty:
        raise FileProblem(f"{name} has no rows under its header row (row {int(source.get('header_row') or 1)}) — "
                          "it may be empty, or the header row setting on the Sources tab may be wrong.")
    return df


def _read_raw_file(uploaded_file, source: dict) -> pd.DataFrame:
    filename = uploaded_file.name.lower()
    header_row_index = max(int(source["header_row"]) - 1, 0)

    if filename.endswith(".csv"):
        return pd.read_csv(uploaded_file, header=header_row_index, dtype=str)

    engine = "pyxlsb" if filename.endswith(".xlsb") else "openpyxl"
    sheet_name = source["sheet_name"] or 0
    if isinstance(sheet_name, str) and "," in sheet_name:
        # e.g. URM's own file splits its catalog across a "Grocery" and a
        # "General Merchandise" sheet with identical columns — read every
        # comma-separated sheet name and concatenate them into one frame.
        sheet_names = [s.strip() for s in sheet_name.split(",") if s.strip()]
        frames = [
            pd.read_excel(uploaded_file, sheet_name=s, header=header_row_index, dtype=str, engine=engine)
            for s in sheet_names
        ]
        return pd.concat(frames, ignore_index=True)
    return pd.read_excel(
        uploaded_file, sheet_name=sheet_name, header=header_row_index, dtype=str, engine=engine
    )


def map_and_clean(raw_df: pd.DataFrame, source: dict) -> pd.DataFrame:
    """Maps a raw source DataFrame onto the standard UPC/Department/Category/
    Subcategory/Brand/Description shape and cleans the UPC column, using
    only this one source row's configuration."""
    column_by_field = {
        "Department": source["department_column"],
        "Category": source["category_column"],
        "Subcategory": source["subcategory_column"],
        "Brand": source["brand_column"],
        "Description": source["description_column"],
        "Pack": source.get("pack_column"),
        "Size": source.get("size_column"),
        "UOM": source.get("uom_column"),
    }

    upc_column = source["upc_column"]
    missing = missing_columns(raw_df, source)
    if missing:
        # Every column the settings name must be there — a file without them
        # would silently come in with blank fields (or wrong UPCs).
        cols = [str(c) for c in raw_df.columns]
        header_hint = (f" Most of its column names are blank, so row {int(source.get('header_row') or 1)} may not be "
                       "the header row." if sum(c.startswith("Unnamed") for c in cols) > len(cols) / 2 else "")
        raise FileProblem(
            f"This file is missing {len(missing)} column(s) {_source_name(source)}'s settings need: "
            + ", ".join(f"{what} (“{col}”)" for what, col in missing)
            + f".{header_hint} Check it's the right file for this source, or update the column names on the Sources tab.",
            detail=f"Columns in the file: {', '.join(cols[:40])}{'…' if len(cols) > 40 else ''}")

    upc_suffix_column = source.get("upc_suffix_column")
    out = pd.DataFrame(index=raw_df.index)
    if upc_suffix_column and upc_suffix_column in raw_df.columns:
        # e.g. a source that stores a UPC's check digit in its own column
        # (GTIN + Chk Dgt): concatenate the raw text of both columns as-is
        # (no artificial zero-padding on either piece) before running the
        # same strip-non-digit/zero-pad-to-12 cleanup as every other source.
        primary_raw = raw_df[upc_column].fillna("").astype(str).str.strip().apply(_strip_float_artifact)
        suffix_raw = raw_df[upc_suffix_column].fillna("").astype(str).str.strip().apply(_strip_float_artifact)
        # A blank primary column (e.g. missing GTIN) makes the row invalid
        # even if the suffix (e.g. a lone check digit) isn't blank — a check
        # digit by itself is not a real UPC.
        raw_upc = (primary_raw + suffix_raw).where(primary_raw != "", "")
    else:
        raw_upc = raw_df[upc_column].fillna("").astype(str).str.strip()

    out["RawUPC"] = raw_upc
    # Excel turns a long UPC into "7.06128E+11" when the column isn't Text —
    # the real digits are gone, so the whole file is turned away (it's
    # almost always every row, and every one of them would be wrong).
    sci = raw_upc[raw_upc.str.fullmatch(SCI_UPC)]
    if len(sci):
        raise FileProblem(
            f"{len(sci):,} of its UPCs are in scientific notation (e.g. “{sci.iloc[0]}”) — Excel shortened them, so "
            "their real digits are lost. Re-export the file with the UPC column formatted as Text, then upload it again.")
    lettered = raw_upc[raw_upc.str.contains(r"[A-Za-z]", regex=True)]
    out["UPC"] = raw_upc.apply(lambda v: generic_clean_upc(v, int(source["strip_trailing_digits"])))
    for field, col in column_by_field.items():
        if col and col in raw_df.columns:
            # Confirmed against script.py's own clean_text_columns: a cell
            # that string-casts to the literal text "nan" (happens on some
            # pandas versions for a truly blank cell) must be blanked too,
            # not left as visible "nan" text.
            out[field] = raw_df[col].fillna("").astype(str).str.strip().replace("nan", "")
        else:
            out[field] = ""

    # 'plain' (the default): Pack/Size/UOM columns, if any, are used as-is
    # — nothing to parse. The other two formats mean Size Column actually
    # holds a combined value (e.g. "16OZ" or "12/16OZ") that needs
    # splitting apart; see split_pack_size_uom for the actual parsing and
    # its graceful fallback when a value doesn't match the expected shape.
    size_format = source.get("size_format") or "plain"
    if size_format != "plain":
        pack, size, uom = split_pack_size_uom(out["Size"], size_format, source.get("uom_aliases"))
        if size_format == "pack_size_uom_combined":
            out["Pack"] = pack
        out["Size"] = size
        out["UOM"] = uom

    # Per-source value-cleaning rules, ported 1:1 from script.py's hardcoded
    # loaders (e.g. NWG's Brand=="_" blanking, SPINS's Department=="OTHER"
    # blanking) but made generic/editable on the Sources tab instead of
    # requiring new Python for each distributor's own quirks.
    strip_leading_code_fields = source.get("strip_leading_code_fields")
    if strip_leading_code_fields:
        # e.g. URM's Group/Subgroup values are prefixed with a numeric code
        # and a space ("001 SALAD DRESSING..." -> "SALAD DRESSING...").
        for field in [f.strip() for f in strip_leading_code_fields.split(",") if f.strip()]:
            if field in out.columns:
                out[field] = out[field].apply(strip_leading_code)

    blank_brand_when_equals = source.get("blank_brand_when_equals")
    if blank_brand_when_equals:
        out.loc[out["Brand"] == blank_brand_when_equals, "Brand"] = ""

    blank_department_when_equals = source.get("blank_department_when_equals")
    if blank_department_when_equals:
        mask = out["Department"] == blank_department_when_equals
        out.loc[mask, ["Department", "Category", "Subcategory"]] = ""

    brand_suffix_match = source.get("brand_suffix_match")
    brand_suffix_result = source.get("brand_suffix_result")
    if brand_suffix_match and brand_suffix_result:
        mask = out["Brand"].str.endswith(brand_suffix_match, na=False)
        out.loc[mask, "Brand"] = brand_suffix_result

    blank_department_default = source.get("blank_department_default")
    if blank_department_default:
        out.loc[out["Department"] == "", "Department"] = blank_department_default

    rejected_frames = []

    exclude_column = source.get("exclude_column")
    exclude_values = source.get("exclude_values")
    if exclude_column and exclude_values and exclude_column in raw_df.columns:
        # e.g. KEHE's UOM "DS"/"PL" (display/pallet items script.py drops
        # entirely) — a generic row-exclusion rule, not tied to any one
        # source. Case-SENSITIVE exact match, confirmed against script.py's
        # own literal `out["UOM"] == "DS"` / `== "PL"` comparison (no case
        # folding) — enter Exclude Values on the Sources tab in the exact
        # case they appear in the source file.
        values_to_exclude = {v.strip() for v in exclude_values.split(",") if v.strip()}
        exclude_mask = (
            raw_df[exclude_column].fillna("").astype(str).str.strip().isin(values_to_exclude)
        )
        if exclude_mask.any():
            excluded_rows = out[exclude_mask].copy()
            excluded_rows["Reason"] = "excluded_value"
            rejected_frames.append(excluded_rows)
        out = out[~exclude_mask].copy()

    invalid_mask = out["UPC"].isna()
    if invalid_mask.any():
        invalid_rows = out[invalid_mask].copy()
        invalid_rows["Reason"] = "invalid_upc"
        rejected_frames.append(invalid_rows)
    out = out[~invalid_mask].copy()

    dedup_deprioritize_brand_value = source.get("dedup_deprioritize_brand_value")
    if dedup_deprioritize_brand_value:
        # e.g. SPINS: among duplicate UPCs, prefer to drop the one branded
        # "PL" rather than a plain "keep whichever came first in the file" —
        # stable sort so a non-matching-brand row always sorts before a
        # matching one, then keep-first dedup targets that one first.
        deprioritize_last = (out["Brand"] == dedup_deprioritize_brand_value)
        out = out.iloc[deprioritize_last.astype(int).argsort(kind="stable")]
        is_dup = out.duplicated(subset="UPC", keep="first")
        # Confirmed against script.py's own remove_spins_duplicate_upcs:
        # ONLY a duplicate row that itself carries the deprioritized Brand
        # actually gets dropped here — a duplicate UPC with any OTHER
        # Brand is left in the final dataset untouched (script.py flags it
        # separately for manual review instead; this app has no equivalent
        # review queue yet, so it's simply kept rather than silently
        # dropped like a generic duplicate would be).
        duplicate_mask = is_dup & (out["Brand"] == dedup_deprioritize_brand_value)
    else:
        duplicate_mask = out.duplicated(subset="UPC", keep="first")
    if duplicate_mask.any():
        duplicate_rows = out[duplicate_mask].copy()
        duplicate_rows["Reason"] = "duplicate_upc"
        rejected_frames.append(duplicate_rows)
    out = out[~duplicate_mask].copy()

    rejected_df = (
        pd.concat(rejected_frames, ignore_index=True)
        if rejected_frames
        else pd.DataFrame(columns=["RawUPC", "UPC", *STANDARD_FIELDS, "Reason"])
    )

    stats = {
        "rows_parsed": len(raw_df),
        "rows_staged": len(out),
        "dropped_invalid_upc": int(invalid_mask.sum()),
        # UPCs with letters in them (bad UPCs — dropped with the invalid ones), and a few examples
        "letter_upcs": len(lettered), "letter_upc_examples": lettered.head(3).tolist(),
        "dropped_duplicate_upc": int(duplicate_mask.sum()),
    }

    return out.drop(columns=["RawUPC"]), stats, rejected_df


# ---------------------------------------------------------------------------
# Which UPCs each source's files have listed, and for how many uploads in a
# row a UPC has been missing from its source's file — so items that stopped
# appearing can be reviewed (and, if no file has them any more, removed).
# ---------------------------------------------------------------------------
UPC_SEEN_DDL = """
IF OBJECT_ID('dbo.source_upc_seen') IS NULL
CREATE TABLE dbo.source_upc_seen (
    source_key NVARCHAR(50) NOT NULL,
    upc VARCHAR(20) NOT NULL,
    first_seen_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    last_seen_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    last_file NVARCHAR(400) NULL,
    missed_uploads INT NOT NULL DEFAULT 0,
    CONSTRAINT PK_source_upc_seen PRIMARY KEY (source_key, upc)
)"""


DELETED_RETURN_DDL = """
IF COL_LENGTH('dbo.deleted_upcs', 'returned_at') IS NULL
    ALTER TABLE dbo.deleted_upcs ADD returned_at DATETIME2 NULL, returned_file NVARCHAR(400) NULL"""


def mark_deleted_returning(conn, source_key: str, filename: str) -> int:
    """A deleted UPC that a newly saved file lists comes back: a Merge no longer
    leaves it out, and once it's in the item master it's compared with how it
    was when deleted (see dept_mapping.settle_returned_items)."""
    from itemmaster import upload_reports
    conn.execute(text(DELETED_RETURN_DDL))
    upload_reports.ensure_tables(conn)
    # (a UPC combined into another item stays out, whatever a file lists)
    return conn.execute(text(
        "UPDATE dbo.deleted_upcs SET returned_at = SYSUTCDATETIME(), returned_file = :f "
        "WHERE returned_at IS NULL AND combined_into IS NULL "
        "AND upc IN (SELECT upc FROM dbo.raw_items WHERE source_key = :sk)"),
        {"sk": source_key, "f": filename}).rowcount


def ensure_upc_seen(conn) -> None:
    """Creates the table the first time, filled from each source's current
    rows (all of them "seen in its latest file", missed 0)."""
    created = conn.execute(text("SELECT OBJECT_ID('dbo.source_upc_seen')")).scalar() is None
    if created:
        conn.execute(text(UPC_SEEN_DDL))
        conn.execute(text(
            "INSERT INTO dbo.source_upc_seen (source_key, upc, first_seen_at, last_seen_at, last_file, missed_uploads) "
            "SELECT r.source_key, r.upc, MIN(r.loaded_at), MIN(r.loaded_at), MAX(u.filename), 0 FROM dbo.raw_items r "
            "LEFT JOIN dbo.source_raw_uploads u ON u.source_key = r.source_key GROUP BY r.source_key, r.upc"))


def record_upc_seen(conn, source_key: str, filename: str, new_file: bool = True) -> None:
    """After a source's rows were replaced (raw_items now holds exactly its new
    file): every UPC in it is seen now; with a new file, every UPC it used to
    list but doesn't any more has missed one more upload."""
    ensure_upc_seen(conn)
    if new_file:
        conn.execute(text(
            "UPDATE s SET missed_uploads = missed_uploads + 1 FROM dbo.source_upc_seen s WHERE s.source_key = :sk "
            "AND NOT EXISTS (SELECT 1 FROM dbo.raw_items r WHERE r.source_key = :sk AND r.upc = s.upc)"), {"sk": source_key})
    conn.execute(text(
        """MERGE dbo.source_upc_seen AS t
           USING (SELECT DISTINCT upc FROM dbo.raw_items WHERE source_key = :sk) AS src
           ON t.source_key = :sk AND t.upc = src.upc
           WHEN MATCHED THEN UPDATE SET last_seen_at = SYSUTCDATETIME(), missed_uploads = 0, last_file = :f
           WHEN NOT MATCHED THEN INSERT (source_key, upc, last_file) VALUES (:sk, src.upc, :f);"""),
        {"sk": source_key, "f": filename})


def load_upc_seen(engine, source_key: str = None) -> pd.DataFrame:
    """The seen-history rows (one source's, or all), for the "not in the file
    any more" lists."""
    with engine.begin() as conn:
        ensure_upc_seen(conn)
        return pd.read_sql(text(
            "SELECT source_key, upc, last_seen_at, last_file, missed_uploads FROM dbo.source_upc_seen"
            + (" WHERE source_key = :sk" if source_key else "")), conn, params={"sk": source_key} if source_key else {})


SIGNATURE_COLUMNS = ["UPC", "Description", "Brand", "Department", "Category", "Subcategory", "Pack", "Size", "UOM"]


def rows_signature(cleaned_df: pd.DataFrame) -> str:
    """A fingerprint of a cleaned file's rows — the same rows give the same
    fingerprint, whatever the file is called or the order its rows are in."""
    import hashlib
    df = cleaned_df.reindex(columns=SIGNATURE_COLUMNS)
    df = df.astype(object).where(df.notna(), "").astype(str).apply(lambda s: s.str.strip())
    df = df.sort_values(SIGNATURE_COLUMNS, kind="stable")
    return hashlib.sha1(df.to_csv(index=False).encode("utf-8")).hexdigest()


def current_signature(engine, source_key: str) -> str:
    """The same fingerprint for the rows a source has now (its last saved file)."""
    with engine.connect() as conn:
        df = pd.read_sql(text(
            "SELECT upc AS UPC, description AS Description, brand AS Brand, department AS Department, "
            "category AS Category, subcategory AS Subcategory, pack AS Pack, size AS Size, uom AS UOM "
            "FROM dbo.raw_items WHERE source_key = :sk"), conn, params={"sk": source_key})
    return rows_signature(df)


def stage_source(engine, source_key: str, cleaned_df: pd.DataFrame, rejected_df: pd.DataFrame,
                 stats: dict, uploaded_by: str = None, original_filename: str = None,
                 on_retry=None, new_file: bool = True, report: dict = None) -> int:
    """Replaces this source's staged rows in raw_items with cleaned_df, and
    permanently records what happened (row counts + every rejected row with
    a reason) in ingestion_log / ingestion_rejected_rows — every upload,
    not just the first one, so a reviewer can always see why a row from
    any month's file didn't make it in. Returns the new ingestion_log id.
    Retries a transient connection timeout (e.g. Azure SQL serverless
    waking up from idle) via robust_begin — see db.py.

    report: {"kind": "manual" | "auto", "title": ...} — this upload goes into
    an upload report (see upload_reports); the report's id is put in
    report["id"], so the next file of the same upload joins the same report."""
    from itemmaster.db import robust_begin
    from itemmaster import upload_reports

    with robust_begin(engine, on_retry=on_retry) as conn:
        if report is not None:
            if not report.get("id"):
                report["id"] = upload_reports.start_report(conn, report.get("kind", "manual"), uploaded_by, report.get("title"))
            upload_reports.capture_before(conn, report["id"], source_key)
        conn.execute(text("DELETE FROM dbo.raw_items WHERE source_key = :sk"), {"sk": source_key})

        if not cleaned_df.empty:
            to_insert = cleaned_df.rename(columns={
                "UPC": "upc", "Department": "department", "Category": "category",
                "Subcategory": "subcategory", "Brand": "brand", "Description": "description",
                "Pack": "pack", "Size": "size", "UOM": "uom",
            }).copy()
            to_insert["source_key"] = source_key
            to_insert = to_insert.replace({"": None})
            to_insert.to_sql("raw_items", conn, schema="dbo", if_exists="append", index=False, chunksize=1000)

        log_id = conn.execute(
            text(
                """
                INSERT INTO dbo.ingestion_log
                    (source_key, uploaded_by, original_filename, rows_parsed, rows_staged,
                     dropped_invalid_upc, dropped_duplicate_upc)
                OUTPUT inserted.id
                VALUES
                    (:source_key, :uploaded_by, :original_filename, :rows_parsed, :rows_staged,
                     :dropped_invalid_upc, :dropped_duplicate_upc)
                """
            ),
            {
                "source_key": source_key,
                "uploaded_by": uploaded_by,
                "original_filename": original_filename,
                "rows_parsed": stats["rows_parsed"],
                "rows_staged": stats["rows_staged"],
                "dropped_invalid_upc": stats["dropped_invalid_upc"],
                "dropped_duplicate_upc": stats["dropped_duplicate_upc"],
            },
        ).scalar()

        if not rejected_df.empty:
            to_insert = rejected_df.rename(columns={
                "RawUPC": "raw_upc", "Department": "department", "Category": "category",
                "Subcategory": "subcategory", "Brand": "brand", "Description": "description",
                "Reason": "reason",
            }).drop(columns=["UPC", "Pack", "Size", "UOM"], errors="ignore").copy()
            to_insert["log_id"] = log_id
            to_insert["source_key"] = source_key
            to_insert = to_insert.replace({"": None})
            to_insert.to_sql("ingestion_rejected_rows", conn, schema="dbo", if_exists="append", index=False, chunksize=1000)

        # (new_file=False: the same file re-read with new settings — not an upload anything "missed",
        # and not a file bringing a deleted item back)
        record_upc_seen(conn, source_key, original_filename, new_file=new_file)
        if new_file:
            mark_deleted_returning(conn, source_key, original_filename)
        if report is not None:
            report.setdefault("sources", {})[source_key] = upload_reports.record_after(
                conn, report["id"], source_key, original_filename, log_id, int(stats.get("rows_staged") or len(cleaned_df)))

    from itemmaster.dept_mapping import log_activity  # (here: dept_mapping imports this module's neighbours)
    log_activity(engine, uploaded_by, "Uploads & Merge", f"Uploaded {original_filename or 'a file'}", source_key,
                 int(stats.get("rows_staged") or len(cleaned_df)),
                 details={k: stats.get(k) for k in ("rows_parsed", "rows_staged", "dropped_invalid_upc", "dropped_duplicate_upc")})
    return log_id


def save_raw_upload(engine, source_key: str, raw_df: pd.DataFrame, filename: str, uploaded_by: str = None) -> None:
    """Keeps the as-read (pre-mapping) DataFrame for this source's most
    recently committed upload, so a later column-mapping change (Pack/
    Size Column, Department Column, a typo fix, etc.) can be replayed
    without asking for the same file again. One row per source — this
    replaces whatever was kept for a previous upload, matching "the
    current inputs," not a full history."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.source_raw_uploads WHERE source_key = :sk"), {"sk": source_key})
        conn.execute(
            text(
                "INSERT INTO dbo.source_raw_uploads (source_key, filename, raw_csv, uploaded_by) "
                "VALUES (:source_key, :filename, :raw_csv, :uploaded_by)"
            ),
            {
                "source_key": source_key, "filename": filename,
                "raw_csv": raw_df.to_csv(index=False), "uploaded_by": uploaded_by,
            },
        )


def load_raw_upload(engine, source_key: str):
    """Returns {"df", "filename", "uploaded_by", "uploaded_at"} for this
    source's last committed upload, or None if it's never had one (e.g.
    it predates this feature, or nothing's been uploaded for it yet)."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT filename, raw_csv, uploaded_by, uploaded_at "
                "FROM dbo.source_raw_uploads WHERE source_key = :sk"
            ),
            {"sk": source_key},
        ).mappings().fetchone()
    if row is None:
        return None
    return {
        "df": pd.read_csv(io.StringIO(row["raw_csv"]), dtype=str),
        "filename": row["filename"], "uploaded_by": row["uploaded_by"], "uploaded_at": row["uploaded_at"],
    }


