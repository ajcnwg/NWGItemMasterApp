"""
Adds the six real distributor sources to dbo.sources, matching the exact
column mappings script.py's hardcoded loaders use (PRIORITY_ORDER, the
SPINS/NWG column maps, and the URM/KEHE/C&S process_* functions) — but
pointed at the actual files copied into this app's own Inputs\\ folder.

Safe to re-run: skips any source_key that already exists.
"""

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import get_engine

SOURCES = [
    {
        "source_key": "nwg",
        "source_label": "Scan Advantage",
        "enabled": True,
        "priority_rank": 1,
        "file_keyword": "NWG",
        "sheet_name": None,
        "header_row": 2,  # real headers are on row 2; row 1 is a title banner
        "upc_column": "UPC",
        "upc_suffix_column": None,
        "strip_trailing_digits": 0,
        "department_column": "DEPARTMENT",
        "category_column": "CATEGORY",
        "subcategory_column": "SUBCATEGORY",
        "brand_column": "BRAND",
        "description_column": "DESCRIPTION",
        "notes": (
            "script.py also blanks Brand '_' and Department 'PENDING FOR ASSIGNMENT', "
            "and defaults a blank Department to 'GENERAL MERCHANDISE' — not applied here yet."
        ),
    },
    {
        "source_key": "spins",
        "source_label": "SPINS",
        "enabled": True,
        "priority_rank": 2,
        "file_keyword": "SPINS",
        "sheet_name": None,
        "header_row": 1,
        "upc_column": "UPC EAN13",
        "upc_suffix_column": None,
        "strip_trailing_digits": 1,  # 13-digit EAN -> drop check digit
        "department_column": "DEPARTMENT",
        "category_column": "CATEGORY",
        "subcategory_column": "SUBCATEGORY",
        "brand_column": "BRAND",
        "description_column": "DESCRIPTION",
        "notes": (
            "script.py also blanks Department/Category/Subcategory when Department=='OTHER', "
            "and renames any Brand ending in 'PL' to exactly 'PL' — not applied here yet."
        ),
    },
    {
        "source_key": "urm",
        "source_label": "URM",
        "enabled": True,
        "priority_rank": 3,
        "file_keyword": "URM",
        "sheet_name": "URM Item Listing",
        "header_row": 4,  # rows 1-3 are a title banner; real header is row 4
        "upc_column": "GTIN",
        "upc_suffix_column": "Chk Dgt",  # real UPC = GTIN + check digit concatenated
        "strip_trailing_digits": 0,
        "department_column": "Department",
        "category_column": "Group Desc",
        "subcategory_column": "Sub Group Desc",
        "brand_column": "Brand",
        "description_column": "Item Description",
        "notes": (
            "Real UPC = GTIN + Chk Dgt (handled via UPC Suffix Column). Using the "
            "already-descriptive 'Group Desc'/'Sub Group Desc' columns, so script.py's "
            "leading-numeric-code stripping on the raw 'Group'/'Sub Group' isn't needed here."
        ),
    },
    {
        "source_key": "kehe",
        "source_label": "KEHE",
        "enabled": True,
        "priority_rank": 4,
        "file_keyword": "KEHE",
        "sheet_name": None,
        "header_row": 1,
        "upc_column": "UPC12",
        "upc_suffix_column": None,
        "strip_trailing_digits": 1,
        "department_column": "Division",
        "category_column": "Product Category",
        "subcategory_column": "Product Subcategory",
        "brand_column": "BRAND",
        "description_column": "Item Description",
        "notes": (
            "script.py also excludes rows where UOM is 'DS' (display) or 'PL' (pallet) — "
            "not filtered here yet; review the Upload & Ingest preview before merging."
        ),
    },
    {
        "source_key": "cs_pnw",
        "source_label": "C&S PNW",
        "enabled": True,
        "priority_rank": 5,
        "file_keyword": "CS PNW",
        "sheet_name": "ALL ITEMS",
        "header_row": 1,
        "upc_column": "UPC",
        "upc_suffix_column": None,
        "strip_trailing_digits": 0,
        "department_column": "GL DESCRIPTION",
        "category_column": "Cat. Name",
        "subcategory_column": "Neilsen Category Desc.",
        "brand_column": "Trade Brand",
        "description_column": "Item Description",
        "notes": "C&S PNW, from the 'ALL ITEMS' sheet.",
    },
    {
        "source_key": "cs_ca",
        "source_label": "C&S CA",
        "enabled": True,
        "priority_rank": 6,
        "file_keyword": "CS CA",
        "sheet_name": "ALL ITEMS",
        "header_row": 1,
        "upc_column": "UPC",
        "upc_suffix_column": None,
        "strip_trailing_digits": 0,
        "department_column": "GL DESCRIPTION",
        "category_column": "Cat. Name",
        "subcategory_column": "Neilsen Category Desc.",
        "brand_column": "Trade Brand",
        "description_column": "Item Description",
        "notes": "C&S CA, from the 'ALL ITEMS' sheet.",
    },
]

INSERT_SQL = """
INSERT INTO dbo.sources
    (source_key, source_label, enabled, priority_rank, file_keyword, sheet_name, header_row,
     upc_column, upc_suffix_column, strip_trailing_digits,
     department_column, category_column, subcategory_column, brand_column, description_column, notes)
VALUES
    (:source_key, :source_label, :enabled, :priority_rank, :file_keyword, :sheet_name, :header_row,
     :upc_column, :upc_suffix_column, :strip_trailing_digits,
     :department_column, :category_column, :subcategory_column, :brand_column, :description_column, :notes)
"""


def main():
    engine = get_engine()
    with engine.begin() as conn:
        for source in SOURCES:
            exists = conn.execute(
                text("SELECT 1 FROM dbo.sources WHERE source_key = :k"),
                {"k": source["source_key"]},
            ).first()
            if exists:
                print(f"'{source['source_key']}' already exists — skipping.")
                continue
            conn.execute(text(INSERT_SQL), source)
            print(f"Added source '{source['source_key']}' ({source['source_label']}).")


if __name__ == "__main__":
    main()
