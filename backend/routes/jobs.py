#!/usr/bin/env python3
"""Job routes for RO-ED AI Agent"""

import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, status
from fastapi.responses import StreamingResponse
from typing import List, Optional
from io import BytesIO

import config
import database
from middleware import get_current_user, check_permission, get_data_scope
from schemas import JobResponse, JobDetailResponse, DuplicateCheckResponse

router = APIRouter()


def _mirror_to_storage(save_path: Path, job_id: Optional[str] = None) -> Optional[str]:
    """Mirror a saved local PDF to the configured storage provider (S3/GCS).
    Returns the storage reference (e.g. 's3:uploads/foo.pdf') if uploaded, else None.
    Never raises — storage failure must not break the upload flow.
    """
    try:
        from storage import get_provider
        provider = get_provider()
        if getattr(provider, "name", "local") == "local":
            return None
        with open(save_path, "rb") as f:
            data = f.read()
        key = f"uploads/{Path(save_path).name}"
        provider.upload(key, data, content_type="application/pdf")
        ref = f"{provider.name}:{key}"
        if job_id:
            try:
                database.update_job_pdf_storage(job_id, ref)
            except Exception as e:
                print(f"[storage] update_job_pdf_storage failed: {e}")
        return ref
    except Exception as e:
        print(f"[storage] mirror upload failed: {e}")
        return None


def _user_scope(current_user: dict) -> Optional[int]:
    """Return user_id for scoping based on data_scope permission."""
    scope = get_data_scope(current_user)
    if scope in ("all_readonly", "all_full"):
        return None  # See all data
    return current_user["id"]  # Own data only


@router.get("/", response_model=List[JobResponse])
async def list_jobs(
    limit: int = Query(50, le=500),
    current_user: dict = Depends(get_current_user),
):
    """List jobs. Admin sees all, users see only their own."""
    user_id = _user_scope(current_user)
    if user_id:
        jobs = database.get_user_jobs(user_id, limit=limit)
    else:
        jobs = database.get_all_jobs(limit=limit)
    return jobs


@router.get("/processing")
async def get_processing_jobs(current_user: dict = Depends(get_current_user)):
    """Get currently processing jobs."""
    user_id = _user_scope(current_user)
    if user_id:
        jobs = database.get_user_jobs(user_id, limit=10)
    else:
        jobs = database.get_all_jobs(limit=10)
    return [j for j in jobs if j.get("status") == "PROCESSING"]


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Get full job details including items, declarations, logs."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Check access: user can only see own jobs
    user_id = _user_scope(current_user)
    if user_id and job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    return job


@router.delete("/{job_id}")
async def delete_job(job_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a job. Requires delete_jobs permission."""
    check_permission(current_user, "delete_jobs")
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    user_id = _user_scope(current_user)
    if user_id and job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    success = database.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=500, detail="Delete failed")

    database.log_activity(
        current_user["id"], current_user["username"], "DELETE_JOB",
        f"Deleted job {job_id} ({job.get('pdf_name', '')})"
    )
    return {"message": "Job deleted"}


@router.post("/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload a PDF file. Requires upload_pdf permission."""
    check_permission(current_user, "upload_pdf")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files accepted")

    # Save to uploads directory
    config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
    import uuid
    safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    save_path = config.UPLOAD_FOLDER / safe_name

    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    file_size = save_path.stat().st_size
    pdf_hash = database.calculate_pdf_hash(str(save_path))

    # Mirror to remote storage provider if configured (S3/GCS). Non-fatal.
    _mirror_to_storage(save_path)

    # Check for duplicates
    existing = database.find_job_by_hash(pdf_hash)
    is_duplicate = existing is not None
    can_reprocess = False
    if is_duplicate:
        can_reprocess = (
            current_user["role"] == "admin"
            or existing.get("user_id") == current_user["id"]
        )

    return {
        "filename": file.filename,
        "saved_path": str(save_path),
        "file_size": file_size,
        "pdf_hash": pdf_hash,
        "is_duplicate": is_duplicate,
        "can_reprocess": can_reprocess,
        "existing_job": existing if is_duplicate else None,
    }


@router.post("/upload-batch")
async def upload_batch(
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload multiple PDF files. Returns array of upload info."""
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
    MAX_FILES = 10  # max 10 files per batch

    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Maximum {MAX_FILES} files per batch")

    results = []
    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            results.append({"filename": file.filename, "error": "Not a PDF"})
            continue

        config.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        import uuid
        safe_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
        save_path = config.UPLOAD_FOLDER / safe_name

        with open(save_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        file_size = save_path.stat().st_size
        pdf_hash = database.calculate_pdf_hash(str(save_path))

        # Mirror to remote storage provider if configured (S3/GCS). Non-fatal.
        _mirror_to_storage(save_path)

        existing = database.find_job_by_hash(pdf_hash)
        is_duplicate = existing is not None
        can_reprocess = False
        if is_duplicate:
            can_reprocess = (
                current_user["role"] == "admin"
                or existing.get("user_id") == current_user["id"]
            )

        results.append({
            "filename": file.filename,
            "saved_path": str(save_path),
            "file_size": file_size,
            "pdf_hash": pdf_hash,
            "is_duplicate": is_duplicate,
            "can_reprocess": can_reprocess,
            "existing_job": existing if is_duplicate else None,
        })

    return results


@router.get("/{job_id}/confidence")
async def get_job_confidence(job_id: str, current_user: dict = Depends(get_current_user)):
    """Get per-field confidence scores for a job. Recomputes from stored data."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    user_id = _user_scope(current_user)
    if user_id and job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Build declaration and items from stored data
    declarations = job.get("declarations", [])
    decl = {}
    if declarations:
        d = declarations[0]
        decl = {
            "Declaration No": d.get("declaration_no"),
            "Declaration Date": d.get("declaration_date"),
            "Importer (Name)": d.get("importer_name"),
            "Consignor (Name)": d.get("consignor_name"),
            "Invoice Number": d.get("invoice_number"),
            "Invoice Number (Customs Declaration)": d.get("invoice_number_customs_declaration"),
            "Invoice Number (Commercial Invoice)": d.get("invoice_number_commercial_invoice"),
            "Invoice Price": d.get("invoice_price"),
            "Currency": d.get("currency"),
            "Exchange Rate": d.get("exchange_rate"),
            "Currency.1": d.get("currency_2"),
            "Total Customs Value": d.get("total_customs_value"),
            "Import/Export Customs Duty": d.get("import_export_customs_duty"),
            "Commercial Tax (CT)": d.get("commercial_tax_ct"),
            "Advance Income Tax (AT)": d.get("advance_income_tax_at"),
            "Security Fee (SF)": d.get("security_fee_sf"),
            "MACCS Service Fee (MF)": d.get("maccs_service_fee_mf"),
            "Exemption/Reduction": d.get("exemption_reduction"),
        }

    items_raw = job.get("items", [])
    items = []
    for it in items_raw:
        items.append({
            "Item name": it.get("item_name"),
            "Customs duty rate": it.get("customs_duty_rate"),
            "Quantity (1)": it.get("quantity"),
            "Invoice unit price": it.get("invoice_unit_price"),
            "Commercial tax %": it.get("commercial_tax_percent"),
            "Exchange Rate (1)": it.get("exchange_rate"),
        })

    from pipeline.confidence import compute_field_confidence
    confidence = compute_field_confidence(declaration=decl, items=items)
    return confidence


@router.get("/{job_id}/pages")
async def get_job_pages(job_id: str, current_user: dict = Depends(get_current_user)):
    """Get v2 per-page extractions for a job."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    user_id = _user_scope(current_user)
    if user_id and job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    pages = database.get_page_extractions(job_id)
    return pages


@router.get("/{job_id}/page-image/{page_num}")
async def serve_page_image(job_id: str, page_num: int, token: str = Query(...)):
    """Render a single PDF page as PNG image. No scrolling — just that page."""
    from middleware import _try_keycloak, _try_local
    import fitz

    user = None
    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
    if not user:
        user = _try_local(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    scope = get_data_scope(user)
    if scope not in ("all_readonly", "all_full") and job.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    pdf_path = job.get("pdf_path", "")
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF not found")

    doc = fitz.open(pdf_path)
    if page_num < 1 or page_num > len(doc):
        doc.close()
        raise HTTPException(status_code=404, detail=f"Page {page_num} not found")

    page = doc[page_num - 1]
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img_bytes = pix.tobytes("png")
    doc.close()

    from starlette.responses import Response
    return Response(content=img_bytes, media_type="image/png")


@router.get("/{job_id}/annotated-pdf")
async def serve_annotated_pdf(job_id: str, token: str = Query(...)):
    """Generate and serve annotated PDF with highlighted extracted values."""
    from middleware import _try_keycloak, _try_local
    from agents.advanced import find_value_coordinates, annotate_pdf
    import json

    user = None
    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
    if not user:
        user = _try_local(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    pdf_path = job.get("pdf_path", "")
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF not found")

    # Collect all values to highlight
    annotations = []

    # Declaration values
    decl = job.get("declarations", [{}])[0] if job.get("declarations") else {}
    decl_fields = {
        "Declaration No": decl.get("declaration_no"),
        "Importer": decl.get("importer_name"),
        "Consignor": decl.get("consignor_name"),
        "Invoice No": decl.get("invoice_number"),
        "Invoice Number (Customs Declaration)": decl.get("invoice_number_customs_declaration"),
        "Invoice Number (Commercial Invoice)": decl.get("invoice_number_commercial_invoice"),
        "Invoice Price": decl.get("invoice_price"),
        "Exchange Rate": decl.get("exchange_rate"),
        "Customs Value": decl.get("total_customs_value"),
        "Duty": decl.get("import_export_customs_duty"),
        "Tax (CT)": decl.get("commercial_tax_ct"),
        "Income Tax": decl.get("advance_income_tax_at"),
    }

    for label, value in decl_fields.items():
        if value is None or str(value).strip() in ("", "0", "0.0"):
            continue
        coords = find_value_coordinates(pdf_path, str(value))
        if coords:
            c = coords[0]  # Take first match
            annotations.append({**c, "label": label, "color": "green"})

    # Item values
    for i, item in enumerate(job.get("items", [])):
        name = item.get("item_name", "")
        if name and len(name) > 5:
            # Search for first 30 chars of item name
            search_term = name[:30].strip()
            coords = find_value_coordinates(pdf_path, search_term)
            if coords:
                c = coords[0]
                annotations.append({**c, "label": f"Item {i+1}", "color": "yellow"})

    # Generate annotated PDF
    output_path = str(Path(config.RESULTS_DIR) / f"{job_id}_annotated.pdf")
    annotate_pdf(pdf_path, annotations, output_path)

    if not Path(output_path).exists():
        raise HTTPException(status_code=500, detail="Failed to generate annotated PDF")

    from starlette.responses import Response
    with open(output_path, "rb") as f:
        content = f.read()

    # Clean up
    try:
        Path(output_path).unlink()
    except Exception:
        pass

    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/{job_id}/pdf")
async def serve_pdf(job_id: str, token: str = Query(...)):
    """Serve the original PDF file for viewing in browser. Uses token query param for iframe compatibility."""
    import auth as auth_mod
    from middleware import _try_keycloak, _try_local

    # Verify token (supports both Keycloak and local)
    user = None
    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
    if not user:
        user = _try_local(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Check access scope
    scope = get_data_scope(user)
    if scope not in ("all_readonly", "all_full") and job.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")

    pdf_path = job.get("pdf_path", "")
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF file not found on disk")

    from starlette.responses import Response
    with open(pdf_path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/preview-pdf/{filename:path}")
async def preview_uploaded_pdf(filename: str, token: str = Query(...)):
    """Serve an uploaded PDF for preview before processing. Uses saved_path filename."""
    from middleware import _try_keycloak, _try_local

    user = None
    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
    if not user:
        user = _try_local(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Look for file in uploads directory
    pdf_path = config.UPLOAD_FOLDER / filename
    if not pdf_path.exists():
        # Try full path if it's an absolute path
        pdf_path = Path(filename)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF not found")

    # Security: only serve from uploads directory
    try:
        pdf_path.resolve().relative_to(config.UPLOAD_FOLDER.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    from starlette.responses import Response
    with open(pdf_path, "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


def _style_sheet(writer, df, sheet_name):
    """Apply styled headers matching UI (dark bg, white text, uppercase, auto-width)."""
    df.to_excel(writer, sheet_name=sheet_name, index=False, startrow=1, header=False)
    workbook = writer.book
    worksheet = writer.sheets[sheet_name]

    header_fmt = workbook.add_format({
        'bold': True, 'font_color': '#FEFFD6', 'bg_color': '#383832',
        'border': 1, 'border_color': '#383832', 'font_size': 10,
        'text_wrap': True, 'valign': 'vcenter',
    })

    for col_num, col_name in enumerate(df.columns):
        worksheet.write(0, col_num, col_name.upper(), header_fmt)
        max_len = max(len(str(col_name)), df[col_name].astype(str).str.len().max() if len(df) > 0 else 0)
        worksheet.set_column(col_num, col_num, min(max_len + 3, 45))

    worksheet.set_row(0, 28)


@router.get("/{job_id}/download")
async def download_job_excel(job_id: str, current_user: dict = Depends(get_current_user)):
    """Download job results as styled Excel — all tables as separate sheets."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    user_id = _user_scope(current_user)
    if user_id and job.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    import pandas as pd
    import json

    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        # Sheet 1: Product Items (all 9 columns)
        items = job.get('items', [])
        items_data = [{
            'Job': job_id,
            'Item Name': item.get('item_name', ''),
            'Customs Duty Rate': item.get('customs_duty_rate', ''),
            'Quantity (1)': item.get('quantity', ''),
            'Invoice Unit Price': item.get('invoice_unit_price', ''),
            'CIF Unit Price': item.get('cif_unit_price', ''),
            'Currency': item.get('currency', job.get('declarations', [{}])[0].get('currency', '') if job.get('declarations') else ''),
            'Commercial Tax %': item.get('commercial_tax_percent', ''),
            'Exchange Rate (1)': item.get('exchange_rate', ''),
            'HS Code': item.get('hs_code', ''),
            'Origin Country': item.get('origin_country', ''),
            'Customs Value (MMK)': item.get('customs_value_mmk', ''),
            'Processed': item.get('created_at', ''),
        } for item in items]
        all_item_cols = ['Job', 'Item Name', 'Customs Duty Rate', 'Quantity (1)', 'Invoice Unit Price',
                         'CIF Unit Price', 'Currency', 'Commercial Tax %', 'Exchange Rate (1)',
                         'HS Code', 'Origin Country', 'Customs Value (MMK)', 'Processed']
        df_items = pd.DataFrame(items_data, columns=all_item_cols) if items_data else pd.DataFrame(columns=all_item_cols)
        _style_sheet(writer, df_items, 'Product Items')

        # Sheet 2: Declaration (incl. the CIF build-up: freight / insurance / adjustment)
        declarations = job.get('declarations', [])
        decl_data = []
        for decl in declarations:
            decl_data.append({
                'Job': job_id,
                'Declaration No': decl.get('declaration_no', ''),
                'Declaration Date': decl.get('declaration_date', ''),
                'Importer (Name)': decl.get('importer_name', ''),
                'Consignor (Name)': decl.get('consignor_name', ''),
                'Invoice Number': decl.get('invoice_number', ''),
                'Invoice Number (Customs Declaration)': decl.get('invoice_number_customs_declaration', ''),
                'Invoice Number (Commercial Invoice)': decl.get('invoice_number_commercial_invoice', ''),
                'Invoice Price': decl.get('invoice_price', ''),
                'Freight': decl.get('freight_value', ''),
                'Insurance': decl.get('insurance_value', ''),
                'Adjustment': decl.get('adjustment_value', ''),
                'Currency': decl.get('currency', ''),
                'Exchange Rate': decl.get('exchange_rate', ''),
                'Currency 2': decl.get('currency_2', ''),
                'Total Customs Value': decl.get('total_customs_value', ''),
                'Import/Export Customs Duty': decl.get('import_export_customs_duty', ''),
                'Commercial Tax (CT)': decl.get('commercial_tax_ct', ''),
                'Advance Income Tax (AT)': decl.get('advance_income_tax_at', ''),
                'Security Fee (SF)': decl.get('security_fee_sf', ''),
                'MACCS Service Fee (MF)': decl.get('maccs_service_fee_mf', ''),
                'Exemption/Reduction': decl.get('exemption_reduction', ''),
                'Processed': decl.get('created_at', ''),
            })
        all_decl_cols = ['Job', 'Declaration No', 'Declaration Date', 'Importer (Name)', 'Consignor (Name)',
                         'Invoice Number', 'Invoice Number (Customs Declaration)', 'Invoice Number (Commercial Invoice)',
                         'Invoice Price', 'Freight', 'Insurance', 'Adjustment',
                         'Currency', 'Exchange Rate', 'Currency 2',
                         'Total Customs Value', 'Import/Export Customs Duty', 'Commercial Tax (CT)',
                         'Advance Income Tax (AT)', 'Security Fee (SF)', 'MACCS Service Fee (MF)',
                         'Exemption/Reduction', 'Processed']
        df_decl = pd.DataFrame(decl_data, columns=all_decl_cols) if decl_data else pd.DataFrame(columns=all_decl_cols)
        _style_sheet(writer, df_decl, 'Declaration')

        # Sheet 3+: AI-discovered tables (auto-generated)
        additional_tables = []
        cv = job.get('cross_validation')
        if cv and isinstance(cv, dict):
            additional_tables = cv.get('additional_tables', [])

        for table in additional_tables:
            table_name = table.get('table_name', 'Unknown')
            cols = table.get('columns', [])
            rows = table.get('rows', [])
            if not cols and rows:
                cols = [k for k in rows[0].keys() if not k.startswith('_')]
            if not cols or not rows:
                continue

            # Clean rows (remove internal fields)
            clean_rows = [{c: r.get(c, '') for c in cols} for r in rows]
            df_extra = pd.DataFrame(clean_rows, columns=cols)

            # Sanitize sheet name (max 31 chars, no special chars)
            sheet_name = table_name.replace('/', '-').replace('\\', '-')[:31]
            _style_sheet(writer, df_extra, sheet_name)

    output.seek(0)
    filename = job.get('pdf_name', job_id).replace('.pdf', '') + '_extracted.xlsx'

    return StreamingResponse(
        output,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )
