#!/usr/bin/env python3
"""Data routes for RO-ED AI Agent — items, declarations, search, stats"""

from fastapi import APIRouter, Depends, Query
from typing import List, Optional

import database
from middleware import get_current_user
from schemas import ItemResponse, DeclarationResponse, SearchResultResponse, StatsResponse

router = APIRouter()


def _user_scope(current_user: dict) -> Optional[int]:
    """Return user_id for scoping, or None if admin (sees all)."""
    return None if current_user["role"] == "admin" else current_user["id"]


@router.get("/items", response_model=List[ItemResponse])
async def list_items(
    job_id: Optional[str] = None,
    limit: int = Query(500, le=5000),
    current_user: dict = Depends(get_current_user),
):
    """Get consolidated items across jobs. Scoped to user unless admin."""
    user_id = _user_scope(current_user)

    # Get jobs first, then collect items
    if job_id:
        items = database.get_job_items(job_id)
    else:
        jobs = (
            database.get_user_jobs(user_id, limit=limit)
            if user_id
            else database.get_all_jobs(limit=limit)
        )
        items = []
        for job in jobs:
            items.extend(database.get_job_items(job["job_id"]))

    return items[:limit]


@router.get("/declarations", response_model=List[DeclarationResponse])
async def list_declarations(
    job_id: Optional[str] = None,
    limit: int = Query(500, le=5000),
    current_user: dict = Depends(get_current_user),
):
    """Get consolidated declarations across jobs. Scoped to user unless admin."""
    user_id = _user_scope(current_user)

    if job_id:
        declarations = database.get_job_declarations(job_id)
    else:
        jobs = (
            database.get_user_jobs(user_id, limit=limit)
            if user_id
            else database.get_all_jobs(limit=limit)
        )
        declarations = []
        for job in jobs:
            declarations.extend(database.get_job_declarations(job["job_id"]))

    return declarations[:limit]


@router.get("/search", response_model=List[SearchResultResponse])
async def search_documents(
    query: str = "",
    pdf_name: Optional[str] = None,
    page_type: Optional[str] = None,
    limit: int = Query(100, le=1000),
    current_user: dict = Depends(get_current_user),
):
    """Full-text search across page contents (FTS5). Scoped to user unless admin."""
    user_id = _user_scope(current_user)
    results = database.search_page_contents(
        query=query, user_id=user_id, pdf_name=pdf_name,
        page_type=page_type, limit=limit,
    )
    return results


@router.get("/search/pdfs", response_model=List[str])
async def list_searchable_pdfs(current_user: dict = Depends(get_current_user)):
    """Get list of PDFs available for search."""
    user_id = _user_scope(current_user)
    return database.get_page_content_pdfs(user_id=user_id)


@router.get("/search/stats")
async def search_stats(current_user: dict = Depends(get_current_user)):
    """Get page content stats."""
    user_id = _user_scope(current_user)
    return database.get_page_content_stats(user_id=user_id)


@router.get("/stats", response_model=StatsResponse)
async def get_stats(current_user: dict = Depends(get_current_user)):
    """Get overall or per-user stats."""
    if current_user["role"] == "admin":
        return database.get_stats()
    else:
        return database.get_user_stats(current_user["id"])


@router.get("/cost-stats")
async def get_cost_stats(current_user: dict = Depends(get_current_user)):
    """Get cost breakdown stats."""
    user_id = _user_scope(current_user)
    jobs = database.get_user_jobs(user_id, limit=1000) if user_id else database.get_all_jobs(limit=1000)

    from datetime import datetime, date

    total_cost = sum(j.get('cost_usd', 0) or 0 for j in jobs)
    total_jobs = len(jobs)
    avg_per_pdf = total_cost / total_jobs if total_jobs > 0 else 0

    today = date.today().isoformat()
    today_jobs = [j for j in jobs if (j.get('created_at') or '').startswith(today)]
    today_cost = sum(j.get('cost_usd', 0) or 0 for j in today_jobs)

    # This month
    month_prefix = today[:7]  # "2026-04"
    month_jobs = [j for j in jobs if (j.get('created_at') or '').startswith(month_prefix)]
    month_cost = sum(j.get('cost_usd', 0) or 0 for j in month_jobs)

    # Daily breakdown (last 30 days)
    daily = {}
    for j in jobs:
        d = (j.get('created_at') or '')[:10]
        if d:
            daily[d] = daily.get(d, 0) + (j.get('cost_usd', 0) or 0)

    return {
        "total_cost": round(total_cost, 4),
        "total_jobs": total_jobs,
        "avg_per_pdf": round(avg_per_pdf, 4),
        "today_cost": round(today_cost, 4),
        "today_jobs": len(today_jobs),
        "month_cost": round(month_cost, 4),
        "month_jobs": len(month_jobs),
        "daily_breakdown": daily,
    }


def _style_excel(writer, df, sheet_name):
    """Apply styled headers matching the UI (dark bg, white text, uppercase)."""
    df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=1, header=False)
    workbook = writer.book
    worksheet = writer.sheets[sheet_name]

    # Header format — matches dark-bar style
    header_fmt = workbook.add_format({
        'bold': True,
        'font_color': '#FEFFD6',
        'bg_color': '#383832',
        'border': 1,
        'border_color': '#383832',
        'font_size': 10,
        'text_wrap': True,
        'valign': 'vcenter',
    })

    # Data format
    data_fmt = workbook.add_format({
        'font_size': 10,
        'border': 1,
        'border_color': '#E0E0E0',
        'valign': 'vcenter',
    })

    # Number format
    num_fmt = workbook.add_format({
        'font_size': 10,
        'border': 1,
        'border_color': '#E0E0E0',
        'num_format': '#,##0.00',
        'valign': 'vcenter',
    })

    # Write headers
    for col_num, col_name in enumerate(df.columns):
        worksheet.write(0, col_num, col_name.upper(), header_fmt)

    # Auto-fit column widths
    for col_num, col_name in enumerate(df.columns):
        max_len = max(len(str(col_name)), df[col_name].astype(str).str.len().max() if len(df) > 0 else 0)
        worksheet.set_column(col_num, col_num, min(max_len + 3, 40))

    # Set row height for header
    worksheet.set_row(0, 28)


@router.get("/items/download")
async def download_items_excel(current_user: dict = Depends(get_current_user)):
    """Download all items as Excel — same headers as UI."""
    import pandas as pd
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    user_id = _user_scope(current_user)
    jobs = database.get_user_jobs(user_id, limit=500) if user_id else database.get_all_jobs(limit=500)

    all_items = []
    for job in jobs:
        decl_currency = ""
        decls = database.get_job_declarations(job["job_id"])
        if decls:
            decl_currency = decls[0].get("currency", "")
        for item in database.get_job_items(job["job_id"]):
            all_items.append({
                "Job": job["job_id"],
                "Item Name": item.get("item_name", ""),
                "Customs Duty Rate": item.get("customs_duty_rate", ""),
                "Quantity (1)": item.get("quantity", ""),
                "Invoice Unit Price": item.get("invoice_unit_price", ""),
                "CIF Unit Price": item.get("cif_unit_price", ""),
                "Currency": decl_currency,
                "Commercial Tax %": item.get("commercial_tax_percent", ""),
                "Exchange Rate (1)": item.get("exchange_rate", ""),
                "HS Code": item.get("hs_code", ""),
                "Origin Country": item.get("origin_country", ""),
                "Customs Value (MMK)": item.get("customs_value_mmk", ""),
                "Processed": job.get("created_at", ""),
            })

    all_cols = ["Job", "Item Name", "Customs Duty Rate", "Quantity (1)", "Invoice Unit Price",
                "CIF Unit Price", "Currency", "Commercial Tax %", "Exchange Rate (1)", "HS Code",
                "Origin Country", "Customs Value (MMK)", "Processed"]
    df = pd.DataFrame(all_items) if all_items else pd.DataFrame(columns=all_cols)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        _style_excel(writer, df, 'Product Items')
    output.seek(0)

    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="all_product_items.xlsx"'}
    )


@router.get("/declarations/download")
async def download_declarations_excel(current_user: dict = Depends(get_current_user)):
    """Download all declarations as Excel — same headers as UI."""
    import pandas as pd
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    user_id = _user_scope(current_user)
    jobs = database.get_user_jobs(user_id, limit=500) if user_id else database.get_all_jobs(limit=500)

    all_decls = []
    for job in jobs:
        for decl in database.get_job_declarations(job["job_id"]):
            all_decls.append({
                "Job": job["job_id"],
                "Declaration No": decl.get("declaration_no", ""),
                "Date": decl.get("declaration_date", ""),
                "Importer": decl.get("importer_name", ""),
                "Consignor": decl.get("consignor_name", ""),
                "Invoice Number": decl.get("invoice_number", ""),
                "Invoice Number (Customs Declaration)": decl.get("invoice_number_customs_declaration", ""),
                "Invoice Number (Commercial Invoice)": decl.get("invoice_number_commercial_invoice", ""),
                "Invoice Price": decl.get("invoice_price", ""),
                "Currency": decl.get("currency", ""),
                "Exchange Rate": decl.get("exchange_rate", ""),
                "Currency 2": decl.get("currency_2", ""),
                "Customs Value": decl.get("total_customs_value", ""),
                "Duty": decl.get("import_export_customs_duty", ""),
                "Tax": decl.get("commercial_tax_ct", ""),
                "Income Tax": decl.get("advance_income_tax_at", ""),
                "Security": decl.get("security_fee_sf", ""),
                "MACCS": decl.get("maccs_service_fee_mf", ""),
                "Exemption/Reduction": decl.get("exemption_reduction", ""),
                "Processed": job.get("created_at", ""),
            })

    all_cols = ["Job", "Declaration No", "Date", "Importer", "Consignor",
                "Invoice Number", "Invoice Number (Customs Declaration)", "Invoice Number (Commercial Invoice)",
                "Invoice Price", "Currency", "Exchange Rate", "Currency 2",
                "Customs Value", "Duty", "Tax", "Income Tax", "Security", "MACCS",
                "Exemption/Reduction", "Processed"]
    df = pd.DataFrame(all_decls) if all_decls else pd.DataFrame(columns=all_cols)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        _style_excel(writer, df, 'Declarations')
    output.seek(0)

    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="all_declarations.xlsx"'}
    )


@router.get("/ai-tables")
async def get_ai_tables(current_user: dict = Depends(get_current_user)):
    """Get all LLM-discovered additional tables across all jobs."""
    import json

    conn = database._connect()
    user_id = _user_scope(current_user)

    if user_id:
        rows = conn.execute("""
            SELECT job_id, pdf_name, cross_validation_json, created_at
            FROM jobs WHERE cross_validation_json IS NOT NULL AND user_id = ?
            ORDER BY created_at DESC LIMIT 50
        """, (user_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT job_id, pdf_name, cross_validation_json, created_at
            FROM jobs WHERE cross_validation_json IS NOT NULL
            ORDER BY created_at DESC LIMIT 50
        """).fetchall()
    conn.close()

    # Aggregate all tables across jobs, grouped by table name
    all_tables = {}  # table_name → {columns, rows, jobs}

    for row in rows:
        job_id, pdf_name, cv_json, created_at = row
        try:
            cv = json.loads(cv_json)
            tables = cv.get("additional_tables", [])
            for t in tables:
                name = t.get("table_name", "Unknown")
                cols = t.get("columns", [])
                trows = t.get("rows", [])
                pages = t.get("source_pages", [])

                if name not in all_tables:
                    all_tables[name] = {"table_name": name, "columns": cols, "rows": [], "job_count": 0}

                # Merge columns (union of all seen columns)
                for c in cols:
                    if c not in all_tables[name]["columns"]:
                        all_tables[name]["columns"].append(c)

                # Add rows with job context
                for r in trows:
                    r["_job_id"] = job_id
                    r["_pdf_name"] = pdf_name
                    r["_date"] = (created_at or "").split(" ")[0]
                    all_tables[name]["rows"].append(r)

                all_tables[name]["job_count"] += 1
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "tables": list(all_tables.values()),
        "total_jobs": len(rows),
    }
