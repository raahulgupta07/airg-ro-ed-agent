#!/usr/bin/env python3
"""Corrections route — save user corrections for few-shot learning."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

import database
from middleware import get_current_user

router = APIRouter()


class CorrectionRequest(BaseModel):
    job_id: str
    table_key: str  # "declaration" or "product_items"
    field_key: str  # e.g. "Invoice Price", "Origin Country"
    item_index: Optional[int] = None  # None for declaration, 0+ for items
    original_value: Optional[str] = None
    corrected_value: str


@router.post("/")
async def submit_correction(req: CorrectionRequest, current_user: dict = Depends(get_current_user)):
    """Save a user correction. This feeds into few-shot learning for future extractions."""

    # Save correction to DB
    correction_id = database.save_correction(
        job_id=req.job_id,
        profile_id=1,
        table_key=req.table_key,
        field_key=req.field_key,
        item_index=req.item_index,
        original_value=req.original_value,
        corrected_value=req.corrected_value,
        user_id=current_user.get("id"),
        username=current_user.get("username"),
    )

    # Also update the actual value in the job's items/declarations so it shows correctly on reload
    try:
        conn = database._connect()
        if req.table_key == "declaration":
            # Map field_key to column name
            col_map = {
                "Declaration No": "declaration_no",
                "Declaration Date": "declaration_date",
                "Importer (Name)": "importer_name",
                "Consignor (Name)": "consignor_name",
                "Invoice Number": "invoice_number",
                "Invoice Number (Customs Declaration)": "invoice_number_customs_declaration",
                "Invoice Number (Commercial Invoice)": "invoice_number_commercial_invoice",
                "Invoice Price": "invoice_price",
                "Currency": "currency",
                "Exchange Rate": "exchange_rate",
                "Total Customs Value": "total_customs_value",
                "Import/Export Customs Duty": "import_export_customs_duty",
                "Commercial Tax (CT)": "commercial_tax_ct",
                "Advance Income Tax (AT)": "advance_income_tax_at",
                "Security Fee (SF)": "security_fee_sf",
                "MACCS Service Fee (MF)": "maccs_service_fee_mf",
                "Exemption/Reduction": "exemption_reduction",
            }
            col = col_map.get(req.field_key)
            if col:
                conn.execute(f"UPDATE declarations SET {col} = ? WHERE job_id = ?",
                             (req.corrected_value, req.job_id))
        elif req.table_key == "product_items" and req.item_index is not None:
            col_map = {
                "Item name": "item_name",
                "Customs duty rate": "customs_duty_rate",
                "Quantity (1)": "quantity",
                "Invoice unit price": "invoice_unit_price",
                "Commercial tax %": "commercial_tax_percent",
                "Exchange Rate (1)": "exchange_rate",
                "HS Code": "hs_code",
                "Origin Country": "origin_country",
                "Customs Value (MMK)": "customs_value_mmk",
            }
            col = col_map.get(req.field_key)
            if col:
                # Get the item's row id by offset
                rows = conn.execute(
                    "SELECT id FROM items WHERE job_id = ? ORDER BY id", (req.job_id,)
                ).fetchall()
                if req.item_index < len(rows):
                    item_id = rows[req.item_index][0]
                    conn.execute(f"UPDATE items SET {col} = ? WHERE id = ?",
                                 (req.corrected_value, item_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Log activity
    try:
        database.log_activity(
            current_user.get("id"), current_user.get("username"),
            "CORRECTION", f"{req.table_key}.{req.field_key}: '{req.original_value}' → '{req.corrected_value}'"
        )
    except Exception:
        pass

    # Count corrections for this field to track learning progress
    field_count = database.get_correction_count_for_field(1, req.table_key, req.field_key)

    # Auto-learn fee baseline when user corrects a fee field
    FEE_FIELDS = {
        "Commercial Tax (CT)", "Advance Income Tax (AT)",
        "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction",
    }
    if req.table_key == "declaration" and req.field_key in FEE_FIELDS:
        try:
            # Get importer name from this job's declaration
            conn = database._connect()
            row = conn.execute(
                "SELECT importer_name, commercial_tax_ct, advance_income_tax_at, "
                "security_fee_sf, maccs_service_fee_mf, exemption_reduction "
                "FROM declarations WHERE job_id = ?", (req.job_id,)
            ).fetchone()
            conn.close()
            if row and row[0]:
                # Build baseline from current DB values (which now include this correction)
                baseline = database.get_fee_baseline(row[0]) or {}
                baseline.update({
                    "CT": float(row[1] or 0),
                    "AT": float(row[2] or 0),
                    "SF": float(row[3] or 0),
                    "MF": float(row[4] or 0),
                    "Exemption": float(row[5] or 0),
                    "verified": True,
                })
                database.save_fee_baseline(row[0], baseline)
        except Exception:
            pass

    return {
        "status": "ok",
        "correction_id": correction_id,
        "field_corrections_count": field_count,
        "message": f"Correction saved. {field_count} corrections for {req.field_key} — will improve future extractions.",
    }


@router.get("/")
async def list_corrections(limit: int = 20, current_user: dict = Depends(get_current_user)):
    """List recent corrections."""
    corrections = database.get_corrections(limit=limit)
    return corrections


@router.get("/stats")
async def correction_stats(current_user: dict = Depends(get_current_user)):
    """Get correction stats by field."""
    return database.get_correction_stats()


@router.get("/job/{job_id}")
async def job_corrections(job_id: str, current_user: dict = Depends(get_current_user)):
    """Get all corrections for a specific job."""
    corrections = database.get_corrections(job_id=job_id, limit=100)
    return corrections
