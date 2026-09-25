"""
Spreadsheet upload for Add Item / Delete Item / UPC Overrides.

Download a blank template, fill it in (or paste into it), upload it, check
the preview, stage everything in one click. Kept free of Streamlit so the
checks can be tested directly.
"""

import io

import pandas as pd

from itemmaster.ingest import INVALID_UPC, clean_upc

FIELDS = ["description", "department", "category", "subcategory", "brand", "pack", "size", "uom"]
HEADERS = {
    "upc": "UPC", "description": "Description", "department": "Department", "category": "Category",
    "subcategory": "Subcategory", "brand": "Brand", "pack": "Pack", "size": "Size", "uom": "UOM",
}
TEMPLATE_COLUMNS = {
    "add": ["upc"] + FIELDS,
    "delete": ["upc"],
    "edit": ["upc"] + FIELDS,
}
INSTRUCTIONS = {
    "add": "One row per new item. UPC and Description are required; everything else is optional. "
           "Department must be one of the Departments listed on the Departments sheet.",
    "delete": "One UPC per row. Each must already be in the item master.",
    "edit": "One row per item to change. Fill in only the columns you want to change — "
            "a blank cell keeps that item's current value (so for just Departments, fill in UPC and Department). "
            "A Department for an item in a Broken Out group becomes that group's item decision; "
            "everything else becomes a UPC override, which wins over the item's group.",
}


def template_bytes(kind: str, departments: list) -> bytes:
    """An Excel file: the columns to fill in, how to fill them in, and the
    valid Departments."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        cols = [HEADERS[c] for c in TEMPLATE_COLUMNS[kind]]
        pd.DataFrame(columns=cols).to_excel(xw, sheet_name="Items", index=False)
        pd.DataFrame({"How to fill this in": [INSTRUCTIONS[kind]]}).to_excel(xw, sheet_name="Instructions", index=False)
        if kind != "delete":
            pd.DataFrame({"Department": departments}).to_excel(xw, sheet_name="Departments", index=False)
        sheet = xw.sheets["Items"]
        for i, _ in enumerate(cols):
            sheet.column_dimensions[chr(ord("A") + i)].width = 18 if i else 16
        # Text, so Excel doesn't turn a UPC into 1.23E+11 or drop its zeros.
        for row in range(2, 2001):
            sheet.cell(row=row, column=1).number_format = "@"
        if "department" in TEMPLATE_COLUMNS[kind]:
            from openpyxl.worksheet.datavalidation import DataValidation
            col = chr(ord("A") + TEMPLATE_COLUMNS[kind].index("department"))
            dv = DataValidation(type="list", formula1=f"=Departments!$A$2:$A${len(departments) + 1}", allow_blank=True,
                                showErrorMessage=True, errorTitle="Not a Department", error="Pick a Department from the list.")
            sheet.add_data_validation(dv)
            dv.add(f"{col}2:{col}2000")
    return buf.getvalue()


def _norm_header(h) -> str:
    return "".join(ch for ch in str(h).lower() if ch.isalnum())


def read_upload(file) -> pd.DataFrame:
    """The uploaded rows, with columns renamed to the internal field names.
    Reads the "Items" sheet if there is one, otherwise the first sheet."""
    name = getattr(file, "name", "") or ""
    if name.lower().endswith(".csv"):
        df = pd.read_csv(file, dtype=str, keep_default_na=False)
    else:
        xl = pd.ExcelFile(file)
        sheet = "Items" if "Items" in xl.sheet_names else xl.sheet_names[0]
        df = xl.parse(sheet, dtype=str, keep_default_na=False)
    lookup = {_norm_header(v): k for k, v in HEADERS.items()}
    lookup.update({"upccode": "upc", "itemdescription": "description", "dept": "department", "unitofmeasure": "uom"})
    df = df.rename(columns={c: lookup[_norm_header(c)] for c in df.columns if _norm_header(c) in lookup})
    df = df[[c for c in df.columns if c in HEADERS]]
    for c in df.columns:
        df[c] = df[c].astype(str).str.strip().replace({"nan": "", "None": ""})
    return df[(df != "").any(axis=1)].reset_index(drop=True)


def check_upload(kind: str, df: pd.DataFrame, live: dict, departments: list, pending: dict, actor: str,
                 broken_out: dict | None = None) -> tuple:
    """Checks every row. live: {upc: {field: value}} for the current item
    master; pending: the staged Item Master changes ({upc: {...}}).

    broken_out (edit only): dept_mapping.broken_out_items for these UPCs —
    a Department for one of those goes to its group's item decision rather
    than a UPC override.

    Returns (preview, changes, item_decisions): preview is one row per
    uploaded row with a Status ("Ready", "No change", or what's wrong) and
    where it goes; changes is {upc: change} for save_item_master_pending_bulk
    and item_decisions {upc: {"department", "combo_id", "label",
    "description", "source_key"}} for stage_broken_out_decisions, Ready rows only."""
    canon = {d.strip().upper(): d for d in departments}
    broken_out = broken_out or {}
    rows, changes, decisions, seen = [], {}, {}, set()
    if "upc" not in df.columns:
        return pd.DataFrame([{"Row": "", "UPC": "", "Status": "No UPC column found — use the template's headers."}]), {}, {}
    for i, r in df.iterrows():
        raw = r.get("upc", "")
        upc = clean_upc(raw)
        vals = {f: (r.get(f) or "").strip() for f in FIELDS if f in df.columns}
        status = "Ready"
        change = None
        goes = ""
        if upc == INVALID_UPC:
            status = f"Not a valid UPC: “{raw}”"
        elif upc in seen:
            status = "Duplicate — this UPC is on an earlier row too"
        elif upc in pending and pending[upc].get("staged_by") != actor:
            status = f"Already has a change staged by {pending[upc].get('staged_by')}"
        elif vals.get("department") and vals["department"].upper() not in canon:
            status = f"Unknown Department “{vals['department']}”"
        elif kind == "add":
            if upc in live:
                status = "Already in the item master — use UPC Overrides to change it"
            elif not vals.get("description"):
                status = "Description is required"
            else:
                change = {"change_type": "add", **{f: vals.get(f) or None for f in FIELDS}, "source_key": None}
        elif kind == "delete":
            if upc not in live:
                status = "Not in the item master"
            else:
                change = {"change_type": "delete", **{f: live[upc].get(f) for f in FIELDS},
                          "source_key": live[upc].get("source_key")}
        else:  # edit
            if upc not in live:
                status = "Not in the item master"
            else:
                cur = live[upc]
                new = {f: (vals[f] if vals.get(f) else cur.get(f)) for f in FIELDS}
                if new.get("department"):
                    new["department"] = canon.get(str(new["department"]).upper(), new["department"])
                bo = broken_out.get(upc)
                parts = []
                if bo and vals.get("department"):
                    # Its Department is decided in its Broken Out group, not by an override.
                    if new["department"] != bo["current"]:
                        decisions[upc] = {"department": new["department"], "combo_id": bo["combo_id"],
                                          "label": bo["label"], "description": cur.get("description"),
                                          "source_key": bo["source_key"]}
                        parts.append(f"Department → item decision in Broken Out group {bo['group']}")
                    new["department"] = cur.get("department")
                blank = lambda v: v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""
                differs = [f for f in FIELDS if (None if blank(new[f]) else str(new[f]).strip())
                           != (None if blank(cur.get(f)) else str(cur.get(f)).strip())]
                if differs:
                    change = {"change_type": "edit", **new, "source_key": cur.get("source_key")}
                    parts.append("UPC override of " + ", ".join(HEADERS[f] for f in differs))
                status = ("Ready — " + "; ".join(parts)) if parts else "No change"
                goes = " + ".join(("Broken Out item decision" if p.startswith("Department →") else "UPC override")
                                  for p in parts)
        if change is not None:
            if change.get("department"):
                change["department"] = canon.get(str(change["department"]).upper(), change["department"])
            changes[upc] = change
        if upc != INVALID_UPC:
            seen.add(upc)
        desc = vals.get("description") or (live.get(upc, {}).get("description") if upc in live else "")
        row = {"Row": i + 2, "UPC": upc if upc != INVALID_UPC else raw, "Description": desc, "Status": status}
        if kind == "edit":
            row["Goes to"] = goes
        rows.append(row)
    return pd.DataFrame(rows), changes, decisions
