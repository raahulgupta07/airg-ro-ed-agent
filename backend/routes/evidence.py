"""Checks — the per-field evidence surface.

Every ROVER run records, for each declaration field, what it read, where it read
it, which model read it, and what the challenger said instead. Until now that was
computed and discarded. This module turns it into a work queue: one row per field
that a human still has to settle, across every document, newest first.

Two rules the UI depends on and this module enforces:

1. **No confidence percentages.** The stored float is uncalibrated — a real run
   returned `confidence: 1.0` on a field simultaneously marked `suspect`. Status
   and plain words are shown; the number only sorts the queue.

2. **A box is never invented.** Coordinates exist only for fields the geometry
   tier actually measured (the text-layer CUSDECs). Everything else reports
   `located: "unknown"` and the crop falls back to the whole page. Drawing a
   plausible-looking rectangle around the wrong number is worse than drawing none.

Resolving a field goes through `review._apply_edit`, never around it, so the
`field_edits` audit row and the `bump_field_correction` learning signal both
still fire.
"""
import json
from jsonio import loads_maybe
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

import config
import database
from middleware import get_current_user, get_data_scope

# Bare router — main.py supplies the /api/evidence prefix, like every other route
# module here. Setting it in both places silently yields /api/evidence/api/evidence.
router = APIRouter()

# Statuses that mean "a human still has to look at this".
_NEEDS_HUMAN = {"suspect", "review"}

# Render DPI for the whole-page fallback, when no box was measured. Deliberately
# well below the crop DPI: it answers "which page", and the PDF viewer beside it
# answers everything else.
_FALLBACK_DPI = 110

# Field -> the words a customs clerk actually uses. The issues layer sets the
# precedent: no Σ, no CUSDEC jargon, no column names on screen.
_LABEL = {
    "declaration_no": "Declaration number",
    "declaration_no_official": "Declaration number (printed on form)",
    "declaration_date": "Declaration date",
    "arrival_date": "Arrival date",
    "release_order_date": "Release order date",
    "completion_date": "Completion date",
    "importer_name": "Importer",
    "invoice_number": "Invoice number",
    "currency": "Currency",
    "exchange_rate": "Exchange rate",
    "invoice_price_fc": "Invoice price (foreign currency)",
    "invoice_price_mmk": "Invoice price (MMK)",
    "freight_value": "Freight",
    "insurance_value": "Insurance",
    "adjustment_value": "Adjustment",
    "total_customs_value": "Total customs value",
    "import_export_customs_duty": "Customs duty",
    "commercial_tax_ct": "Commercial tax",
    "advance_income_tax_at": "Advance income tax",
    "security_fee_sf": "Security fee",
    "maccs_service_fee_mf": "MACCS service fee",
    "exemption_reduction": "Exemption / reduction",
}


class ResolveRequest(BaseModel):
    value: Any                       # the value the reviewer settled on
    choice: str = "manual"           # kept | alternate | manual — for the audit trail
    reason: Optional[str] = None


# ─── helpers ────────────────────────────────────────────────────

def _scope_user_id(current_user: dict) -> Optional[int]:
    """None = this user may see everything; otherwise restrict to their own jobs."""
    if get_data_scope(current_user) in ("all_readonly", "all_full"):
        return None
    return current_user.get("id")


def _assert_can_see(job: dict, user: dict) -> None:
    if get_data_scope(user) in ("all_readonly", "all_full"):
        return
    if job.get("user_id") != user.get("id"):
        raise HTTPException(403, "Access denied")


def _located(cell: dict) -> str:
    """How well we know WHERE this value came from.

    exact    — measured on the page by the geometry tier
    estimated— a model reported a box while reading a photographed page. It is a
               report of where it looked, not a measurement of printed text, so
               it is close rather than exact and must not claim otherwise.
    unknown  — read from a scan with no coordinates at all
    """
    if cell.get("bbox") and cell.get("page"):
        return "exact" if cell.get("model") == "geometry" else "estimated"
    return "unknown"


def _reason(field: str, cell: dict) -> str:
    """Why this is in the queue, in words a reviewer can act on."""
    # A settled cell reports who settled it — before any engine note, or the card
    # keeps showing "challenger disagrees on one digit" for a value a human
    # already resolved.
    if cell.get("resolved_by"):
        return f"Confirmed by {cell['resolved_by']}."
    note = (cell.get("note") or "").strip()
    if note:
        # engine notes are already written for humans; just tidy the joiner
        return note.replace("; ", " · ")
    alts = cell.get("alternates") or []
    if alts:
        return (f"A second read of the same page returned "
                f"{', '.join(str(a) for a in alts)} instead.")
    if cell.get("status") == "empty":
        return "Nothing was found on the form for this field."
    if cell.get("status") not in _NEEDS_HUMAN:
        # A clean cell still appears in the per-document `settled` list, and it
        # must not claim it was flagged — nobody flagged it and nobody confirmed
        # it. Say what actually happened: it was read and nothing disagreed.
        if cell.get("model") == "geometry":
            return "Measured directly off the form. Nothing disagreed."
        return "Read once. Nothing disagreed with it."
    return "Flagged for a human check."


def _flagged(evidence: dict) -> List[str]:
    """Fields still awaiting a human. Resolved cells drop out permanently."""
    out = []
    for field, cell in (evidence or {}).items():
        if not isinstance(cell, dict):
            continue
        if cell.get("resolved_by"):
            continue
        if cell.get("status") in _NEEDS_HUMAN or cell.get("alternates"):
            out.append(field)
    return out


def _row(field: str, cell: dict) -> dict:
    """One queue row / one card. Note what is NOT here: `confidence`."""
    return {
        "field": field,
        "label": _LABEL.get(field, field.replace("_", " ").capitalize()),
        "value": cell.get("value"),
        "alternates": cell.get("alternates") or [],
        "source": cell.get("source") or "",
        "model": cell.get("model") or "",
        "status": cell.get("status") or "ok",
        "reason": _reason(field, cell),
        "located": _located(cell),
        "page": cell.get("page"),
    }


def _evidence_of(job_id: str) -> dict:
    decls = database.get_job_details(job_id) or {}
    raw = ((decls.get("declarations") or [{}])[0] or {}).get("evidence_json")
    if not raw:
        return {}
    try:
        ev = json.loads(raw) if isinstance(raw, str) else raw
        return ev if isinstance(ev, dict) else {}
    except Exception:
        return {}


# ─── endpoints ──────────────────────────────────────────────────

@router.get("/queue")
async def evidence_queue(limit: int = Query(300, le=1000),
                         importer: Optional[str] = None,
                         field: Optional[str] = None,
                         current_user: dict = Depends(get_current_user)):
    """Every unresolved field across every document, newest document first.

    Grouped by document so a reviewer settles a whole form in one pass rather
    than bouncing between PDFs.
    """
    rows = database.list_jobs_with_evidence(_scope_user_id(current_user), limit)
    docs, total = [], 0
    for r in rows:
        if importer and (r.get("importer_name") or "") != importer:
            continue
        try:
            ev = loads_maybe(r.get("evidence_json"))
        except Exception:
            continue
        fields = _flagged(ev)
        if field:
            fields = [f for f in fields if f == field]
        if not fields:
            continue
        checks = [_row(f, ev[f]) for f in fields]
        total += len(checks)
        docs.append({
            "job_id": r.get("job_id"),
            "pdf_name": r.get("pdf_name"),
            "declaration_no": r.get("declaration_no"),
            "importer_name": r.get("importer_name"),
            "created_at": r.get("created_at"),
            "review_status": r.get("review_status"),
            "checks": checks,
        })
    return {"documents": docs, "total_checks": total,
            "total_documents": len(docs)}


@router.get("/count")
async def evidence_count(current_user: dict = Depends(get_current_user)):
    """Just the badge number for the nav tab."""
    rows = database.list_jobs_with_evidence(_scope_user_id(current_user), 300)
    n = 0
    for r in rows:
        try:
            n += len(_flagged(loads_maybe(r.get("evidence_json"))))
        except Exception:
            pass
    return {"count": n}


@router.get("/{job_id}")
async def evidence_for_job(job_id: str,
                           current_user: dict = Depends(get_current_user)):
    """Every field's evidence for one document — flagged first, then the rest.

    The clean fields are included deliberately: "why did you accept THIS one?"
    is as reasonable a question as "why did you flag that one?".
    """
    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    _assert_can_see(job, current_user)
    ev = _evidence_of(job_id)
    flagged = set(_flagged(ev))
    checks = [_row(f, ev[f]) for f in flagged]
    settled = [dict(_row(f, c), resolved_by=c.get("resolved_by"),
                    resolved_at=c.get("resolved_at"))
               for f, c in ev.items()
               if isinstance(c, dict) and f not in flagged]
    return {"job_id": job_id, "pdf_name": job.get("pdf_name"),
            "declaration_no": (job.get("declarations") or [{}])[0].get("declaration_no"),
            "checks": checks, "settled": settled,
            "has_evidence": bool(ev)}


@router.get("/{job_id}/{field}.png")
async def evidence_crop(job_id: str, field: str, token: str = Query(...),
                        dpi: int = Query(220, le=400)):
    """The actual patch of paper the value was read from.

    Token comes as a query param because this is loaded by an <img> tag, which
    cannot set an Authorization header — same reason serve_pdf does it.

    When the field has no measured box we return the whole page rather than a
    guess. A reviewer seeing the full page understands "we could not pinpoint
    this"; a reviewer seeing a tight box around the wrong number does not.
    """
    from middleware import _try_keycloak, _try_local
    from starlette.responses import Response
    from pathlib import Path
    import fitz

    user = None
    if config.get_keycloak_config():
        user = _try_keycloak(token)
    if not user:
        user = _try_local(token)
    if not user:
        raise HTTPException(401, "Invalid token")

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    _assert_can_see(job, user)

    pdf_path = job.get("pdf_path") or ""
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(404, "PDF not found on disk")

    cell = (_evidence_of(job_id) or {}).get(field) or {}
    bb = (job.get("field_bboxes") or {}).get("declaration", {}).get(field) or {}
    page_no = int(bb.get("page") or cell.get("page") or 1)

    doc = fitz.open(pdf_path)
    try:
        if page_no < 1 or page_no > len(doc):
            page_no = 1
        page = doc[page_no - 1]
        clip = None
        if bb:
            # The row (label + value) is what reads as evidence; the value alone
            # is a number with no context. geometry clips the row to its OWN
            # label, so this cannot spill into the neighbouring column.
            r = bb.get("row") or bb
            # A model-reported box is accurate to a few thousandths of a page,
            # not to the glyph — crop it as tightly as a measured one and the
            # first or last digit can fall outside the picture, which is a
            # worse read than no crop at all.
            pad = 14 if bb.get("source") == "vision" else 5
            clip = fitz.Rect(r["x"] - pad, r["y"] - pad,
                             r["x"] + r["w"] + pad, r["y"] + r["h"] + pad)
            box = fitz.Rect(bb["x"] - 1.5, bb["y"] - 1.5,
                            bb["x"] + bb["w"] + 1.5, bb["y"] + bb["h"] + 1.5)
            page.draw_rect(box, color=(0.745, 0.176, 0.024), width=1.1)
            clip = clip & page.rect
        if clip is None:
            # No measured box — the whole-page fallback, which every scanned
            # declaration takes. A full page came back at 3-5 MB, so a reviewer
            # working through twelve flagged fields on one document pulled tens
            # of megabytes.
            #
            # Lowering the DPI barely helped: the page IS a photograph, and PNG
            # is lossless, so it stores photographic noise faithfully and at
            # 110 DPI still weighed 4.8 MB. JPEG is the right format for a
            # photograph. A measured crop stays PNG — those are small, and they
            # are text and ruled lines, where lossless actually matters.
            dpi = min(dpi, _FALLBACK_DPI)
            pix = page.get_pixmap(dpi=dpi)
            return Response(content=pix.tobytes("jpeg", jpg_quality=70),
                            media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        png = pix.tobytes("png")
    finally:
        doc.close()

    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


def _differs(field: str, stored, incoming) -> bool:
    """Is the reviewer's value actually different from what the column holds?

    Compared on the numeric value, not the text — the column holds 3262921.0 while
    the reviewer is confirming the string "3,262,921". A plain string compare makes
    every "Keep this value" look like an edit and writes a phantom audit row.
    """
    col = database.DECLARATION_FIELD_MAP.get(field)
    if col in database.NUMERIC_DECLARATION_COLUMNS:
        try:
            return database.coerce_numeric(stored) != database.coerce_numeric(incoming)
        except ValueError:
            return True  # unreadable → let _apply_edit raise its 400
    return str(stored) != str(incoming)


@router.post("/{job_id}/{field}/resolve")
async def resolve_check(job_id: str, field: str, body: ResolveRequest,
                        request: Request,
                        current_user: dict = Depends(get_current_user)):
    """Settle one field: write the value, stamp the cell, drop it from the queue.

    Goes through review._apply_edit so this is audited and fed back into learning
    exactly like an inline edit — a correction made here must not be invisible to
    the rest of the system.
    """
    from routes.review import _apply_edit, FieldEditRequest, _track_correction

    job = database.get_job_details(job_id)
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    _assert_can_see(job, current_user)
    if get_data_scope(current_user) == "all_readonly":
        raise HTTPException(403, "Read-only access")

    ev = _evidence_of(job_id)
    cell = ev.get(field)
    if not isinstance(cell, dict):
        raise HTTPException(404, f"No evidence recorded for field: {field}")

    # Compare against what the DECLARATION actually holds, not against the
    # evidence cell. The two can legitimately differ — evidence_json is TEXT and
    # keeps 46,487,178.29 while the declaration column is a single-precision
    # `real` that rounded it to 46,487,180 — and comparing to the cell would make
    # "Keep this value" a no-op that silently leaves the wrong number in the DB.
    editable = field in database.DECLARATION_FIELD_MAP
    stored = database.get_declaration_field(job_id, field) if editable else None
    changed = editable and _differs(field, stored, body.value)
    if changed:
        _apply_edit(job_id, "declaration", 0,
                    FieldEditRequest(field=field, value=body.value,
                                     reason=body.reason or f"checks:{body.choice}"),
                    current_user, request)
        _track_correction(job_id, field)

    cell["value"] = body.value
    cell["status"] = "ok"
    cell["resolved_by"] = current_user.get("username")
    cell["resolved_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
    cell["resolution"] = body.choice
    ev[field] = cell
    database.update_declaration_evidence(job_id, ev)

    return {"status": "ok", "field": field, "value": body.value,
            "changed": changed, "editable": editable,
            "previous": stored, "remaining": len(_flagged(ev))}
