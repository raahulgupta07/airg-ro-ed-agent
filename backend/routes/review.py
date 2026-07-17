#!/usr/bin/env python3
"""Review/Approve route — V11 Maestro extraction QA workflow.

Powers the side-by-side review UI:
  - List pending_review queue
  - Patch declaration / item fields (with full audit trail in field_edits)
  - Approve / reject / save draft
  - Re-run extraction (creates a child job linked via parent_job_id)

Every PATCH:
  1. Reads the original value
  2. Writes new value to declarations/items
  3. Inserts row in field_edits
  4. Increments jobs.edits_count
  5. Mirrors to event_logger.log_event (action='FIELD_EDIT')
  6. For fee fields → updates importer fee_baseline (self-learning)
"""

import json
from typing import Optional, Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import database
import event_logger
from middleware import get_current_user

router = APIRouter()


# ─── Request models ─────────────────────────────────────────────

class FieldEditRequest(BaseModel):
    field: str
    value: Any = None
    reason: Optional[str] = None
    page_ref: Optional[int] = None


class ApproveRequest(BaseModel):
    notes: Optional[str] = None


class RejectRequest(BaseModel):
    notes: str


class DraftRequest(BaseModel):
    notes: Optional[str] = None


class RerunRequest(BaseModel):
    notes: Optional[str] = None


class ItemAddRequest(BaseModel):
    item: Optional[Dict[str, Any]] = None


class ItemReorderRequest(BaseModel):
    order: List[int]


class BulkApproveRequest(BaseModel):
    job_ids: List[str]
    notes: Optional[str] = None


class BulkRejectRequest(BaseModel):
    job_ids: List[str]
    notes: str


class AutoApproveRequest(BaseModel):
    enabled: bool
    threshold: float


# ─── Helpers ────────────────────────────────────────────────────

def _request_ctx(request: Request) -> dict:
    """Pull IP / User-Agent from request.state (set by EventContextMiddleware)."""
    state = getattr(request, "state", None)
    return {
        "ip": getattr(state, "client_ip", None) if state else None,
        "user_agent": getattr(state, "user_agent", None) if state else None,
    }


def _autosave_fee_baseline(job_id: str) -> None:
    """When a fee field is edited, refresh importer_profiles.fee_baseline_json
    from the current declaration row (which now reflects the edit). Mirrors
    the pattern from routes/corrections.py.
    """
    try:
        conn = database._connect()
        row = conn.execute(
            "SELECT importer_name, commercial_tax_ct, advance_income_tax_at, "
            "security_fee_sf, maccs_service_fee_mf, exemption_reduction "
            "FROM declarations WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        if not row or not row[0]:
            return
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


def _job_importer(job_id: str) -> Optional[str]:
    """Importer name for a job (from its declaration). None on any error."""
    try:
        conn = database._connect()
        row = conn.execute(
            "SELECT importer_name FROM declarations WHERE job_id = ?", (job_id,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _track_correction(job_id: str, field_name: str) -> None:
    """P5 measurement: record a human correction against (importer, field) so the
    weakspots error-rate becomes real (not just edit-frequency). Never raises."""
    try:
        imp = _job_importer(job_id)
        if imp and field_name:
            database.bump_field_correction(imp, field_name)
    except Exception:
        pass


def _auto_build_priors(job_id: str) -> None:
    """P2: on approval, (re)build this importer's priors so the drift-warning
    read-path (priors.check_against_priors, wired in workflow) goes live.
    Flag-gated LEARN_AUTO_PRIORS; deterministic SQL, fast; never raises."""
    import os
    if str(os.getenv("LEARN_AUTO_PRIORS", "0")).strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        imp = _job_importer(job_id)
        if not imp:
            return
        from v11.learn import priors
        priors.build_priors(imp)
    except Exception:
        pass


def _apply_edit(job_id: str, entity_type: str, entity_index: int,
                req: FieldEditRequest, current_user: dict, request: Request) -> dict:
    """Apply a field edit (declaration or item). Returns audit dict."""
    if entity_type == "declaration":
        col = database.DECLARATION_FIELD_MAP.get(req.field)
        if not col:
            raise HTTPException(400, f"Unknown declaration field: {req.field}")
        original = database.get_declaration_field(job_id, req.field)
        ok = database.update_declaration_field(job_id, req.field, req.value)
    elif entity_type == "item":
        col = database.ITEM_FIELD_MAP.get(req.field)
        if not col:
            raise HTTPException(400, f"Unknown item field: {req.field}")
        original = database.get_item_field(job_id, entity_index, req.field)
        ok = database.update_item_field(job_id, entity_index, req.field, req.value)
    else:
        raise HTTPException(400, f"Bad entity_type: {entity_type}")

    if not ok:
        raise HTTPException(404, f"Could not update {entity_type}[{entity_index}].{req.field} for job {job_id}")

    edit_id = database.log_field_edit(
        job_id=job_id,
        entity_type=entity_type,
        entity_index=entity_index,
        field_name=req.field,
        original=original,
        corrected=req.value,
        edited_by=current_user.get("username"),
        page_ref=req.page_ref,
        reason=req.reason,
    )
    database.increment_edits_count(job_id)

    # Mirror to event log
    ctx = _request_ctx(request)
    event_logger.log_event(
        action="FIELD_EDIT",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"{entity_type}[{entity_index}].{req.field}: '{original}' → '{req.value}'",
        payload={
            "edit_id": edit_id,
            "entity_type": entity_type,
            "entity_index": entity_index,
            "field": req.field,
            "original": original,
            "corrected": req.value,
            "reason": req.reason,
            "page_ref": req.page_ref,
        },
    )

    # Self-learning: fee field edits update importer baseline
    if entity_type == "declaration" and col in database.FEE_FIELD_KEYS:
        _autosave_fee_baseline(job_id)

    # P5: record this correction against (importer, field) for weakspots accuracy.
    _track_correction(job_id, req.field)

    return {
        "edit_id": edit_id,
        "entity_type": entity_type,
        "entity_index": entity_index,
        "field": req.field,
        "original_value": original,
        "corrected_value": req.value,
        "reason": req.reason,
        "page_ref": req.page_ref,
    }


def _ensure_job(job_id: str) -> dict:
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job


# ─── Endpoints ──────────────────────────────────────────────────

@router.get("/queue")
async def review_queue(status: str = "pending_review", limit: int = 200,
                       importer: Optional[str] = None,
                       date_from: Optional[str] = None,
                       date_to: Optional[str] = None,
                       min_edits: Optional[int] = None,
                       max_edits: Optional[int] = None,
                       current_user: dict = Depends(get_current_user)):
    """List jobs awaiting (or matching) the given review status.

    Filters: importer (substring), date_from/date_to (ISO YYYY-MM-DD),
    min_edits/max_edits (jobs.edits_count bounds).
    """
    if status not in ("pending_review", "approved", "rejected", "draft"):
        raise HTTPException(400, "Invalid status filter")
    jobs = database.list_review_queue(
        status=status, limit=limit,
        importer=importer, date_from=date_from, date_to=date_to,
        min_edits=min_edits, max_edits=max_edits,
    )
    return {"status": status, "count": len(jobs), "jobs": jobs}


@router.get("/stats")
async def review_stats(current_user: dict = Depends(get_current_user)):
    """KPI counts for the review queue header strip."""
    return database.get_review_stats()


@router.post("/bulk/approve")
async def bulk_approve(body: BulkApproveRequest, request: Request,
                       current_user: dict = Depends(get_current_user)):
    """Approve a list of jobs in one call. Returns per-job success/failure.

    One UPDATE flips every row; one query pulls all edits for the fee-baseline
    check — so 100 jobs cost ~2 queries instead of ~500."""
    approved = database.bulk_update_review_status(
        body.job_ids, "approved",
        reviewed_by=current_user.get("username"), notes=body.notes,
    )
    approved_set = set(approved)
    failed: List[Dict[str, str]] = [
        {"job_id": jid, "error": "not found"} for jid in body.job_ids if jid not in approved_set
    ]

    edits_by_job = database.list_field_edits_for_jobs(approved)
    ctx = _request_ctx(request)
    for jid in approved:
        edits = edits_by_job.get(jid, [])
        fee_edited = any(
            e.get("entity_type") == "declaration"
            and database.DECLARATION_FIELD_MAP.get(e.get("field_name")) in database.FEE_FIELD_KEYS
            for e in edits
        )
        if fee_edited:
            _autosave_fee_baseline(jid)
        _auto_build_priors(jid)  # P2
        event_logger.log_event(
            action="JOB_APPROVED",
            user=current_user.get("username"),
            status="OK",
            resource=jid,
            ip=ctx["ip"], user_agent=ctx["user_agent"],
            details=f"Bulk approved with {len(edits)} edit(s)",
            payload={"notes": body.notes, "edits_count": len(edits),
                     "fee_edited": fee_edited, "bulk": True},
        )
    return {"approved": approved, "failed": failed}


@router.post("/bulk/reject")
async def bulk_reject(body: BulkRejectRequest, request: Request,
                      current_user: dict = Depends(get_current_user)):
    """Reject a list of jobs with shared notes."""
    if not body.notes or not body.notes.strip():
        raise HTTPException(400, "Rejection notes are required")
    rejected = database.bulk_update_review_status(
        body.job_ids, "rejected",
        reviewed_by=current_user.get("username"), notes=body.notes,
    )
    rejected_set = set(rejected)
    failed: List[Dict[str, str]] = [
        {"job_id": jid, "error": "not found"} for jid in body.job_ids if jid not in rejected_set
    ]
    ctx = _request_ctx(request)
    for jid in rejected:
        event_logger.log_event(
            action="JOB_REJECTED",
            user=current_user.get("username"),
            status="OK",
            resource=jid,
            ip=ctx["ip"], user_agent=ctx["user_agent"],
            details=body.notes,
            payload={"notes": body.notes, "bulk": True},
        )
    return {"rejected": rejected, "failed": failed}


@router.get("/{job_id}")
async def get_review_detail(job_id: str, current_user: dict = Depends(get_current_user)):
    """Full job + declaration + items + edits + flags for the side-by-side UI."""
    job = _ensure_job(job_id)
    edits = database.list_field_edits(job_id)
    return {
        "job": {
            "job_id": job.get("job_id"),
            "pdf_name": job.get("pdf_name"),
            "pdf_path": job.get("pdf_path"),
            "status": job.get("status"),
            "review_status": job.get("review_status") or "pending_review",
            "reviewed_by": job.get("reviewed_by"),
            "reviewed_at": job.get("reviewed_at"),
            "review_notes": job.get("review_notes"),
            "edits_count": job.get("edits_count") or 0,
            "parent_job_id": job.get("parent_job_id"),
            "created_at": job.get("created_at"),
            "completed_at": job.get("completed_at"),
            "pipeline_mode": job.get("pipeline_mode"),
            "document_type": job.get("document_type"),
            "accuracy_percent": job.get("accuracy_percent"),
            "total_pages": job.get("total_pages"),
        },
        "declarations": job.get("declarations", []),
        "items": job.get("items", []),
        "edits": edits,
        "flags": {
            "cross_validation": job.get("cross_validation"),
        },
    }


@router.patch("/{job_id}/declaration")
async def patch_declaration(job_id: str, body: FieldEditRequest, request: Request,
                            current_user: dict = Depends(get_current_user)):
    """Edit a single declaration field (audited)."""
    _ensure_job(job_id)
    audit = _apply_edit(job_id, "declaration", 0, body, current_user, request)
    return {"status": "ok", "edit": audit}


@router.patch("/{job_id}/items/{item_index}")
async def patch_item(job_id: str, item_index: int, body: FieldEditRequest, request: Request,
                     current_user: dict = Depends(get_current_user)):
    """Edit a single item field by 0-based index (audited)."""
    if item_index < 0:
        raise HTTPException(400, "item_index must be >= 0")
    _ensure_job(job_id)
    audit = _apply_edit(job_id, "item", item_index, body, current_user, request)
    return {"status": "ok", "edit": audit}


@router.post("/{job_id}/approve")
async def approve_job(job_id: str, body: ApproveRequest, request: Request,
                      current_user: dict = Depends(get_current_user)):
    """Mark job approved. If any fee edits exist, persist baseline to importer_profiles."""
    _ensure_job(job_id)
    ok = database.update_review_status(
        job_id, "approved",
        reviewed_by=current_user.get("username"),
        notes=body.notes,
    )
    if not ok:
        raise HTTPException(500, "Failed to update review status")

    # If there were any fee edits during the review, refresh the importer baseline
    edits = database.list_field_edits(job_id)
    fee_edited = any(
        e.get("entity_type") == "declaration"
        and database.DECLARATION_FIELD_MAP.get(e.get("field_name")) in database.FEE_FIELD_KEYS
        for e in edits
    )
    if fee_edited:
        _autosave_fee_baseline(job_id)

    # P2: refresh importer priors from the now-approved ground truth.
    _auto_build_priors(job_id)

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="JOB_APPROVED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"Approved with {len(edits)} edit(s)",
        payload={"notes": body.notes, "edits_count": len(edits), "fee_edited": fee_edited},
    )

    job = database.get_job_details(job_id)
    return {"status": "ok", "job": {
        "job_id": job.get("job_id"),
        "review_status": job.get("review_status"),
        "reviewed_by": job.get("reviewed_by"),
        "reviewed_at": job.get("reviewed_at"),
        "review_notes": job.get("review_notes"),
        "edits_count": job.get("edits_count") or 0,
    }}


@router.post("/{job_id}/reject")
async def reject_job(job_id: str, body: RejectRequest, request: Request,
                     current_user: dict = Depends(get_current_user)):
    """Mark job rejected. Notes required."""
    if not body.notes or not body.notes.strip():
        raise HTTPException(400, "Rejection notes are required")
    _ensure_job(job_id)
    ok = database.update_review_status(
        job_id, "rejected",
        reviewed_by=current_user.get("username"),
        notes=body.notes,
    )
    if not ok:
        raise HTTPException(500, "Failed to update review status")

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="JOB_REJECTED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=body.notes,
        payload={"notes": body.notes},
    )

    # Force severity warn (event_logger only auto-warns for security set)
    job = database.get_job_details(job_id)
    return {"status": "ok", "job": {
        "job_id": job.get("job_id"),
        "review_status": job.get("review_status"),
        "reviewed_by": job.get("reviewed_by"),
        "reviewed_at": job.get("reviewed_at"),
        "review_notes": job.get("review_notes"),
    }}


@router.post("/{job_id}/draft")
async def save_draft(job_id: str, body: DraftRequest, request: Request,
                     current_user: dict = Depends(get_current_user)):
    """Save reviewer notes as draft — does not approve/reject."""
    _ensure_job(job_id)
    ok = database.update_review_status(job_id, "draft", notes=body.notes)
    if not ok:
        raise HTTPException(500, "Failed to save draft")
    ctx = _request_ctx(request)
    event_logger.log_event(
        action="JOB_DRAFT_SAVED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=body.notes or "draft saved",
    )
    return {"status": "ok", "review_status": "draft"}


@router.get("/{job_id}/edits")
async def get_edits(job_id: str, current_user: dict = Depends(get_current_user)):
    """Return audit trail of every field edit on this job."""
    _ensure_job(job_id)
    return {"job_id": job_id, "edits": database.list_field_edits(job_id)}


@router.post("/{job_id}/rerun")
async def rerun_extraction(job_id: str, body: RerunRequest, request: Request,
                           current_user: dict = Depends(get_current_user)):
    """Trigger V11 re-extraction. Creates a NEW job linked to the original via parent_job_id.

    The actual extraction runs in a background thread so the request returns quickly.
    """
    parent = _ensure_job(job_id)
    pdf_path = parent.get("pdf_path")
    if not pdf_path:
        raise HTTPException(400, "Original PDF path missing — cannot re-run")

    import asyncio
    from pathlib import Path
    if not Path(pdf_path).exists():
        raise HTTPException(404, f"PDF no longer on disk: {pdf_path}")

    try:
        from v11.workflow import run as run_v11
    except Exception as e:
        raise HTTPException(500, f"V11 pipeline unavailable: {e}")

    # Run synchronously off the event loop. Could be backgrounded with BackgroundTasks
    # but caller wants the new job_id back.
    try:
        result = await asyncio.to_thread(run_v11, pdf_path, None)
    except Exception as e:
        raise HTTPException(500, f"Re-extract failed: {e}")

    new_job_id = result.get("job_id") if isinstance(result, dict) else None
    if not new_job_id:
        raise HTTPException(500, "Re-extract did not return a job_id")

    # Link child → parent
    try:
        conn = database._connect()
        conn.execute(
            "UPDATE jobs SET parent_job_id = ?, review_status = 'pending_review' WHERE job_id = ?",
            (job_id, new_job_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="JOB_RERUN",
        user=current_user.get("username"),
        status="OK",
        resource=new_job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"Re-ran extraction of {job_id} → {new_job_id}",
        payload={"parent_job_id": job_id, "notes": body.notes},
    )

    return {
        "status": "ok",
        "parent_job_id": job_id,
        "new_job_id": new_job_id,
        "review_status": "pending_review",
    }


# ─── Item add / delete / reorder (V11 review UI) ────────────────────

@router.post("/{job_id}/items")
async def add_item_endpoint(job_id: str, body: ItemAddRequest, request: Request,
                            current_user: dict = Depends(get_current_user)):
    """Append a new item row to a job. Body: { item: { item_name?, hs_code?, ... } }."""
    _ensure_job(job_id)
    incoming = body.item or {}
    new_id = database.add_item(job_id, incoming)

    items_after = database.list_items(job_id)
    new_index = next(
        (i for i, it in enumerate(items_after) if it.get("id") == new_id),
        len(items_after) - 1,
    )
    new_row = items_after[new_index] if items_after else {}

    # Audit: synthetic field_edit row
    try:
        database.log_field_edit(
            job_id=job_id,
            entity_type="item",
            entity_index=new_index,
            field_name="__added__",
            original=None,
            corrected=json.dumps(new_row, default=str),
            edited_by=current_user.get("username"),
        )
    except Exception:
        pass
    database.increment_edits_count(job_id)

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="ITEM_ADDED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"Added item at index {new_index}",
        payload={"item_index": new_index, "item": new_row},
    )

    return {
        "status": "ok",
        "item": {**new_row, "_index": new_index},
        "total_items": len(items_after),
    }


@router.delete("/{job_id}/items/{item_index}")
async def delete_item_endpoint(job_id: str, item_index: int, request: Request,
                               current_user: dict = Depends(get_current_user)):
    """Soft-delete the item at item_index (0-based, display-order)."""
    if item_index < 0:
        raise HTTPException(400, "item_index must be >= 0")
    _ensure_job(job_id)

    removed = database.delete_item(job_id, item_index)
    if removed is None:
        raise HTTPException(404, f"Item index {item_index} not found for job {job_id}")

    try:
        database.log_field_edit(
            job_id=job_id,
            entity_type="item",
            entity_index=item_index,
            field_name="__deleted__",
            original=json.dumps(removed, default=str),
            corrected=None,
            edited_by=current_user.get("username"),
        )
    except Exception:
        pass
    database.increment_edits_count(job_id)

    items_after = database.list_items(job_id)

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="ITEM_DELETED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"Deleted item at index {item_index}",
        payload={"item_index": item_index, "removed": removed},
    )

    return {
        "status": "ok",
        "deleted": True,
        "total_items": len(items_after),
    }


@router.post("/{job_id}/items/reorder")
async def reorder_items_endpoint(job_id: str, body: ItemReorderRequest, request: Request,
                                 current_user: dict = Depends(get_current_user)):
    """Reorder items. Body: { order: [old_index, ...] } — a permutation."""
    _ensure_job(job_id)
    try:
        items_after = database.reorder_items(job_id, body.order)
    except ValueError as e:
        raise HTTPException(400, str(e))

    ctx = _request_ctx(request)
    event_logger.log_event(
        action="ITEMS_REORDERED",
        user=current_user.get("username"),
        status="OK",
        resource=job_id,
        ip=ctx["ip"], user_agent=ctx["user_agent"],
        details=f"Reordered {len(body.order)} items",
        payload={"order": body.order},
    )

    return {"status": "ok", "items": items_after}
