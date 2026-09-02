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


def relocate_boxes(job_id: str) -> dict:
    """Recompute where each stored value sits on the page. No model call.

    Boxes are computed once, during extraction, and written to
    `jobs.field_bboxes_json`. Reading a job replays those coordinates — it never
    searches the PDF again. So every improvement to the locator reaches only
    documents extracted AFTER it, and a job read yesterday keeps yesterday's
    gaps forever.

    That is not acceptable as the only option: telling a reviewer "re-extract it"
    costs a model call and twenty seconds to fix something that is pure
    arithmetic on text already in the file. This re-runs the locator against the
    values already in the database.

    Deliberately does NOT touch the values. Only where they were found.
    """
    from v11.tools.field_bbox import compute_field_bboxes
    from v11.triage import declaration_pages, _locate_cusdec_page

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    pdf_path = job.get("pdf_path") or ""
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(404, "PDF not on disk — nothing to search")

    decls = database.get_job_declarations(job_id) or []
    decl = dict(decls[0]) if decls else {}
    items = [dict(i) for i in (database.get_job_items(job_id) or [])]

    # Columns that describe the row, not the document. Searching for a job id or
    # a timestamp finds nothing useful and can match a page number.
    for k in ("id", "job_id", "created_at", "updated_at", "sanity_flags_json",
              "evidence_json", "cross_val_passed", "verified", "is_valid"):
        decl.pop(k, None)
    for it in items:
        for k in ("id", "job_id", "created_at", "is_deleted", "display_order"):
            it.pop(k, None)

    anchor, _digital = _locate_cusdec_page(pdf_path)
    pages = declaration_pages(pdf_path, anchor, decl.get("declaration_no"))

    stored = (job.get("field_bboxes") or {}).get("declaration") or {}
    before = len(stored)
    bboxes = compute_field_bboxes(pdf_path, decl, items, pages=pages)

    # Boxes the model reported while reading a photographed declaration cannot
    # be recomputed here — there is no text to search for, which is the whole
    # reason they exist. Re-running the locator on such a job would otherwise
    # replace them with nothing and quietly undo the only positions it has.
    # Text-layer hits still win: they are a measurement, not a report.
    kept = 0
    dst = bboxes.setdefault("declaration", {})
    for field, bb in stored.items():
        if isinstance(bb, dict) and bb.get("source") == "vision" and field not in dst:
            dst[field] = bb
            kept += 1

    database.update_job_field_bboxes(job_id, bboxes)
    after = len(bboxes.get("declaration") or {})

    return {"job_id": job_id, "pages": pages,
            "declaration_boxes_before": before, "declaration_boxes_after": after,
            "vision_boxes_kept": kept,
            "item_rows_located": len(bboxes.get("items") or {})}


@router.post("/{job_id}/relocate-boxes")
async def relocate_boxes_route(job_id: str,
                               current_user: dict = Depends(get_current_user)):
    """Re-find every stored value on the page. Free, ~1s, never re-extracts."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    scope = get_data_scope(current_user)
    if scope not in ("all_readonly", "all_full") and job.get("user_id") != current_user["id"]:
        raise HTTPException(403, "Access denied")
    if scope == "all_readonly":
        raise HTTPException(403, "Read-only access")
    return relocate_boxes(job_id)


#: Shown to a reviewer, in their words, when a job can never have a marked PDF.
#: A photographed declaration has no text layer, so there is nothing to search
#: for and no coordinate to record — saying "no data" would read as a bug.
SCANNED_REASON = ("The customs declaration in this document is a photograph, so "
                  "no positions could be measured and nothing can be marked on it.")
NO_PDF_REASON = "The source PDF is no longer on disk, so it cannot be marked."
STALE_REASON = ("No positions are stored for this job. It was extracted before the "
                "locator ran; re-run 'relocate boxes' to measure them.")


def _declaration_page_is_digital(pdf_path: str) -> bool:
    """True when the CUSTOMS DECLARATION page itself carries a text layer.

    Used only to explain an ABSENCE. A run that produced no coordinates is
    either a photographed declaration (permanent, nothing to fix) or an old job
    from before the locator (fixable in a second, no model call). Those two need
    different sentences, and guessing wrong tells a reviewer to retry something
    that can never work.

    The question has to be asked about the DECLARATION, not about the bundle.
    Measured on job `…_10030692266`: 20 of its 28 pages carry a text layer — the
    invoice, the packing list, the delivery order — while the CUSDEC itself is a
    photograph. A "does this PDF have any text?" probe called that document
    digital and told the reviewer to re-run the locator, which then found
    exactly nothing, twice. `triage._locate_cusdec_page` answers the real
    question: it returns the declaration page only when it can read markers out
    of that page's own text.
    """
    try:
        from v11.triage import _locate_cusdec_page
        _page, is_digital = _locate_cusdec_page(pdf_path)
        return bool(is_digital)
    except Exception:
        # Import failure, not a document judgement. "Stale" suggests an action
        # that is free and harmless; "photograph" tells someone to stop trying.
        return True


def iter_marks(job: dict):
    """Every highlight this job's marked PDF will carry, as (kind, box, label).

    `kind` is "declaration" or "item" and picks the colour. `label` is the
    annotation popup text.

    The one rule worth stating: **a box is only a mark if the value is still
    there.** Boxes are keyed by field name, and the Phase-4 merge alias map
    renames what the engines emit — Presto/Scribe write `customs_duty`,
    `security_fee`, `commercial_tax`, the declarations table stores
    `import_export_customs_duty`, `security_fee_sf`, `commercial_tax_ct`. Both
    spellings get located during extraction, so a job carries a box under a name
    the table has no column for. Measured on job `…_10031969976`: 7 of its 28
    stored declaration boxes resolve to nothing, and the marked PDF drew 7
    highlights whose popup read `customs_duty = None` — sitting on top of the
    identical figure already correctly marked under its DB name. A highlight
    that asserts a field is None while pointing at a printed number is the one
    thing a marked PDF must never do.

    A reviewer can also clear a field, which lands in exactly the same place: the
    box remains, the value does not, and the mark has nothing left to claim.

    Counting and drawing both go through here so the number on the button is the
    number of highlights in the file.
    """
    bboxes = job.get("field_bboxes") or {}
    decl = (job.get("declarations") or [{}])[0] or {}
    items = job.get("items") or []

    for field, bb in (bboxes.get("declaration") or {}).items():
        val = decl.get(field)
        if val in (None, ""):
            continue
        yield "declaration", bb, f"{field} = {val}"

    for idx, row in (bboxes.get("items") or {}).items():
        try:
            item = items[int(idx)]
        except (ValueError, IndexError, TypeError):
            item = {}
        for field, bb in (row or {}).items():
            val = item.get(field)
            if val in (None, ""):
                continue
            try:
                label = f"Item {int(idx) + 1} · {field} = {val}"
            except (ValueError, TypeError):
                label = f"Item {idx} · {field} = {val}"
            yield "item", bb, label


def marked_pdf_status(job: dict) -> dict:
    """How many values can be marked on this job's PDF, and why not if none.

    Kept separate from the PDF route so the UI can decide whether to OFFER the
    download without fetching two megabytes to find out. Before this existed the
    only way to learn a job had no marks was to open the link and be handed a
    404 body inside the PDF viewer, which looks like the feature is broken
    rather than like the document being a photograph.
    """
    decl_marks = item_marks = 0
    for kind, _bb, _label in iter_marks(job):
        if kind == "declaration":
            decl_marks += 1
        else:
            item_marks += 1
    total = decl_marks + item_marks

    reason = None
    if not total:
        pdf_path = job.get("pdf_path") or ""
        if not pdf_path or not Path(pdf_path).exists():
            reason = NO_PDF_REASON
        elif not _declaration_page_is_digital(pdf_path):
            reason = SCANNED_REASON
        else:
            reason = STALE_REASON

    return {
        "job_id": job.get("job_id"),
        "available": total > 0,
        "marks": total,
        "declaration_marks": decl_marks,
        "item_marks": item_marks,
        "reason": reason,
    }


@router.get("/{job_id}/marks")
async def get_marked_pdf_status(job_id: str,
                                current_user: dict = Depends(get_current_user)):
    """Whether a marked PDF exists for this job, and how many values it marks."""
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    scope = get_data_scope(current_user)
    if scope not in ("all_readonly", "all_full") and job.get("user_id") != current_user["id"]:
        raise HTTPException(403, "Access denied")
    return marked_pdf_status(job)


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
            "Invoice Price (FC)": d.get("invoice_price_fc"),
            "Invoice Price": d.get("invoice_price"),
            "Invoice Price (MMK)": d.get("invoice_price_mmk"),
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
async def serve_page_image(job_id: str, page_num: int, token: str = Query(...),
                           dpi: int = Query(150, ge=60, le=400)):
    """Render a single PDF page as an image. No scrolling — just that page.

    This is the review viewer's page source, so it is fetched on every page jump
    and every field hover. Two things had to change before it could carry that:

    * It rendered at `Matrix(2, 2)` — roughly 144 DPI with no way to ask for
      less. `dpi` is now a parameter so a thumbnail strip can ask for something
      small and the main view for something readable.

    * It always returned PNG. Half these declarations are photographs, and PNG is
      lossless, so it stored photographic noise faithfully at ~7 MB a page.
      A scanned page now comes back as JPEG. A page with a text layer stays PNG:
      that one is line art and small already, and JPEG would blur the digits a
      reviewer is checking.
    """
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
    # A page with extractable text is a rendered form: sharp edges, few colours,
    # small as PNG. A page without one is a photograph of paper.
    has_text = False
    try:
        has_text = len((page.get_text() or "").strip()) > 40
    except Exception:
        pass
    pix = page.get_pixmap(dpi=dpi)
    if has_text:
        img_bytes, media = pix.tobytes("png"), "image/png"
    else:
        img_bytes, media = pix.tobytes("jpeg", jpg_quality=72), "image/jpeg"
    doc.close()

    from starlette.responses import Response
    # Immutable for the session: a rendered page of a stored PDF cannot change,
    # and without this every hover re-fetches it.
    return Response(content=img_bytes, media_type=media,
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{job_id}/annotated-pdf")
async def serve_annotated_pdf(job_id: str, token: str = Query(...)):
    """The original PDF with every extracted value marked on it.

    Uses the coordinates already stored on the job (`jobs.field_bboxes_json`) —
    the same ones the review screen draws. It used to re-search the document
    from scratch on every request, which meant it carried every fault the stored
    boxes no longer have: first hit anywhere in the bundle (so a customs figure
    was marked on the invoice), no thousands-separator or date variants (so no
    money row and no date was ever marked), and no page scoping.

    Reusing the stored boxes also makes this fast and consistent: what you see on
    screen is what you get in the file, and a `relocate-boxes` improvement lands
    here without touching this code.
    """
    from middleware import _try_keycloak, _try_local
    from starlette.responses import Response
    import fitz

    user = None
    if config.get_keycloak_config():
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

    marks = list(iter_marks(job))
    if not marks:
        # Same sentence the UI got from /marks when it decided whether to offer
        # the download. Two wordings for one condition is how a reviewer ends up
        # being told the document is scanned in one place and stale in another.
        raise HTTPException(404, marked_pdf_status(job)["reason"] or SCANNED_REASON)

    decl = (job.get("declarations") or [{}])[0] or {}

    # Header amber, items blue. Two colours because a reviewer checking the tax
    # block should not have to work out which marks belong to the item rows.
    HEADER = (0.98, 0.75, 0.14)
    ITEM = (0.15, 0.39, 0.92)

    doc = fitz.open(pdf_path)
    try:
        marked = 0
        for kind, bb, label in marks:
            pno = int(bb.get("page") or 0) - 1
            if not (0 <= pno < doc.page_count):
                continue
            page = doc[pno]
            r = fitz.Rect(bb["x"] - 1.5, bb["y"] - 1.5,
                          bb["x"] + bb["w"] + 1.5, bb["y"] + bb["h"] + 1.5)
            # A real highlight annotation, not a drawn rectangle: it stays
            # selectable, it carries the field name in its popup, and any PDF
            # reader can list them all in its annotations panel.
            a = page.add_highlight_annot(r)
            a.set_colors(stroke=HEADER if kind == "declaration" else ITEM)
            a.set_info(title="ATLAS V14", content=label)
            a.update()
            marked += 1

        if not marked:
            raise HTTPException(404, "Stored locations do not fall on any page "
                                     "of this PDF")
        content = doc.tobytes(garbage=3, deflate=True)
    finally:
        doc.close()

    name = (decl.get("declaration_no") or job_id)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}_marked.pdf"',
                 "X-Marked-Values": str(marked)},
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

    # Download filename = <declaration number>_<job id>.pdf so a saved file is
    # traceable to both the customs doc and the extraction job.
    decl_no = ""
    try:
        decls = job.get("declarations") or []
        decl_no = str((decls[0] or {}).get("declaration_no") or "").strip()
    except Exception:
        pass
    if not decl_no:
        stem = Path(job.get("pdf_name") or "document.pdf").stem
        decl_no = stem.split("_")[-1] if "_" in stem else stem
    dl_name = f"{decl_no}_{job_id}.pdf"

    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{dl_name}"'},
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
        # The workbook is exactly two sheets — Product Items, then Declaration — matching the
        # layout the team supplied. An "Issues" sheet used to lead; it was removed on request
        # because the file has to arrive in their own format with nothing extra. The issues
        # themselves are unchanged and still surface in the review UI via GET /api/review/{id}.

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
                'Release Order Date': decl.get('release_order_date', ''),
                'Arrival Date': decl.get('arrival_date', ''),
                'Completion Date': decl.get('completion_date', ''),
                'Importer (Name)': decl.get('importer_name', ''),
                'Consignor (Name)': decl.get('consignor_name', ''),
                'Invoice Number': decl.get('invoice_number', ''),
                'Invoice Number (Customs Declaration)': decl.get('invoice_number_customs_declaration', ''),
                'Invoice Number (Commercial Invoice)': decl.get('invoice_number_commercial_invoice', ''),
                'Invoice Price (FC)': decl.get('invoice_price_fc', ''),
                'Invoice Price': decl.get('invoice_price', ''),
                'Invoice Price (MMK)': decl.get('invoice_price_mmk', ''),
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
        # Exactly the team's workbook layout — 23 columns, their order. The
        # lifecycle dates and the FC/MMK invoice split are still extracted,
        # stored and editable in review; they are simply not shown here.
        # `columns=` below DROPS any key missing from this list, so this is the
        # single place that decides the sheet.
        all_decl_cols = ['Job', 'Declaration No', 'Declaration Date',
                         'Importer (Name)', 'Consignor (Name)',
                         'Invoice Number', 'Invoice Number (Customs Declaration)',
                         'Invoice Number (Commercial Invoice)',
                         'Invoice Price', 'Freight', 'Insurance', 'Adjustment',
                         'Currency', 'Exchange Rate', 'Currency 2',
                         'Total Customs Value', 'Import/Export Customs Duty',
                         'Commercial Tax (CT)', 'Advance Income Tax (AT)',
                         'Security Fee (SF)', 'MACCS Service Fee (MF)',
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
