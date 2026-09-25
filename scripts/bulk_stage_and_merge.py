"""
One-time bulk run: stages all six real sources (from Inputs\\) into
raw_items via the same stage_source() the app's Upload & Ingest tab uses
(so ingestion_log/ingestion_rejected_rows get populated identically), then
runs the same priority merge the app's Merge tab runs.

Usage:
    python scripts/bulk_stage_and_merge.py
"""

import io
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from itemmaster.db import get_engine
from itemmaster.ingest import map_and_clean, read_raw_file, stage_source

INPUTS = str(Path(__file__).resolve().parent.parent / "Inputs")

FILES = {
    "nwg": "NWG (26we 07.09.26).xlsx",
    "spins": "SPINS_Total-UPC-wHeirarchy_Ran7-7-26.xlsx",
    "urm": "Weekly URM Item List w Linking.2026-07-20-05-11-51.xlsx",
    "kehe": "KEHE Link-Codes-Master 7.7.26.xlsx",
    "cs_pnw": "CS PNW Daily Order Guide 7.20.26.xlsx",
    "cs_ca": "CS CA Daily Order Guide 7.20.26.xlsx",
}


def fake_upload(path):
    with open(path, "rb") as f:
        data = f.read()
    buf = io.BytesIO(data)
    buf.name = path
    return buf


def stage_all(engine):
    with engine.connect() as conn:
        sources = {row["source_key"]: dict(row) for row in conn.execute(text("SELECT * FROM dbo.sources")).mappings()}

    for source_key, filename in FILES.items():
        print(f"\n--- Staging {source_key} ({filename}) ---")
        source = sources[source_key]
        path = f"{INPUTS}\\{filename}"
        raw_df = read_raw_file(fake_upload(path), source)
        cleaned_df, stats, rejected_df = map_and_clean(raw_df, source)
        log_id = stage_source(
            engine, source_key, cleaned_df, rejected_df, stats,
            uploaded_by="bulk_stage_and_merge.py", original_filename=filename,
        )
        print(f"log_id={log_id} stats={stats}")


def run_merge(engine):
    print("\n--- Running merge ---")
    with engine.connect() as conn:
        enabled_sources = pd.read_sql(
            text("SELECT source_key, priority_rank FROM dbo.sources WHERE enabled = 1 ORDER BY priority_rank"), conn
        )
        raw = pd.read_sql(
            text(
                "SELECT r.upc AS UPC, r.source_key AS SourceKey, r.department AS Department, "
                "r.category AS Category, r.subcategory AS Subcategory, r.brand AS Brand, "
                "r.description AS Description "
                "FROM dbo.raw_items r JOIN dbo.sources s ON r.source_key = s.source_key "
                "WHERE s.enabled = 1"
            ),
            conn,
        )
        overrides = pd.read_sql(text("SELECT * FROM dbo.manual_overrides"), conn)
        deleted = set(pd.read_sql(text("SELECT upc FROM dbo.deleted_upcs"), conn)["upc"])

    priority_order = enabled_sources["source_key"].tolist()
    lookups = {sk: raw[raw["SourceKey"] == sk].set_index("UPC").to_dict("index") for sk in priority_order}
    all_upcs = set(raw["UPC"])
    merge_fields = ["Department", "Category", "Subcategory", "Brand", "Description"]

    final_rows = {}
    for upc in all_upcs:
        chosen_row, chosen_source = None, None
        fallback_row, fallback_source = None, None
        for sk in priority_order:
            row = lookups.get(sk, {}).get(upc)
            if row is None:
                continue
            if fallback_row is None:
                fallback_row, fallback_source = row, sk
            if all(row.get(f) for f in merge_fields):
                chosen_row, chosen_source = row, sk
                break
        if chosen_row is None:
            chosen_row, chosen_source = fallback_row, fallback_source

        final_rows[upc] = {
            "upc": upc,
            "description": chosen_row.get("Description") or upc,
            "department": chosen_row.get("Department") or None,
            "category": chosen_row.get("Category") or None,
            "subcategory": chosen_row.get("Subcategory") or None,
            "brand": chosen_row.get("Brand") or None,
            "source_key": chosen_source,
        }

    for _, o in overrides.iterrows():
        row = final_rows.get(o["upc"], {
            "upc": o["upc"], "description": o["upc"], "department": None,
            "category": None, "subcategory": None, "brand": None, "source_key": None,
        })
        for field in ["description", "department", "category", "subcategory", "brand"]:
            if pd.notna(o[field]) and o[field] != "":
                row[field] = o[field]
        row["source_key"] = row["source_key"] or "manual"
        final_rows[o["upc"]] = row

    for upc in deleted:
        final_rows.pop(upc, None)

    final_df = pd.DataFrame(final_rows.values())
    print(f"Final merged item count: {len(final_df)}")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dbo.items"))
        if not final_df.empty:
            final_df.to_sql("items", conn, schema="dbo", if_exists="append", index=False, chunksize=1000)
    print("Merge complete.")


if __name__ == "__main__":
    engine = get_engine()
    stage_all(engine)
    run_merge(engine)
