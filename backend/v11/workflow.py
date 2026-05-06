"""V11 — Master Router workflow.
Per-page classifier → split PDF → V7 (typed) + V10 (HW) parallel → merge."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from v11.agents.page_classifier import classify_pages
from v11.tools.pdf_split import split_pdf_by_labels
from v11.agents.merger import merge_results
try:
    from v11.tools.field_bbox import compute_field_bboxes as _compute_field_bboxes
except Exception:
    def _compute_field_bboxes(_pdf, _decl, _items):
        return {}

try:
    import event_logger
except Exception:
    event_logger = None

# Event bus (built in parallel as backend/v11/event_bus.py).
# Wrap import in try/except so workflow still runs if bus missing.
try:
    from v11.event_bus import emit as _bus_emit, close as _bus_close
except Exception:
    def _bus_emit(*_args, **_kwargs):
        return None

    def _bus_close(*_args, **_kwargs):
        return None


def _emit(job_id: Optional[str], action: str, data: Dict) -> None:
    """Emit to event bus; never raise."""
    try:
        _bus_emit(job_id, action, data)
    except Exception:
        pass


def _close(job_id: Optional[str]) -> None:
    try:
        _bus_close(job_id)
    except Exception:
        pass


def _conf_to_float(c) -> float:
    """Map classifier confidence ('high|med|low') or numeric → float 0.0–1.0."""
    if isinstance(c, (int, float)):
        try:
            f = float(c)
            return max(0.0, min(1.0, f))
        except Exception:
            return 0.5
    s = str(c or "").lower().strip()
    return {"high": 0.95, "med": 0.7, "medium": 0.7, "low": 0.4}.get(s, 0.5)


def _label_to_verdict(label: str) -> str:
    # Frontend display verdicts (V11 router observability rename):
    #   typed        → PRINTED
    #   handwritten  → INKED
    #   attachment   → EXTRA
    # Backend internal labels (TYPED/HANDWRITTEN/ATTACHMENT) are unchanged.
    return {
        "TYPED": "PRINTED",
        "HANDWRITTEN": "INKED",
        "ATTACHMENT": "EXTRA",
    }.get((label or "").upper(), "PRINTED")


# Pipeline → fancy display label (used for `label` field on STAGE_* events).
PIPELINE_LABEL = {
    "V7": "Veritas",
    "V10_PRO": "Scrivener",
    "V11": "Maestro",
}


def _log_event(action: str, data: Dict, job_id=None) -> None:
    """Mirror to event_logger if available."""
    if not event_logger:
        return
    try:
        event_logger.log_event(
            action=action,
            user=None,
            job_id=job_id,
            status="OK",
            details=json.dumps(data, default=str)[:2000],
        )
    except Exception:
        pass


def _call_v7(pdf_path: str) -> Dict:
    from pipeline.pipeline import run_pipeline
    return run_pipeline(pdf_path)


def _call_v10(pdf_path: str) -> Dict:
    """Use V10 PRO (shape-validated, memory-aware, cost-tracked)."""
    from v10_pro.workflow import run as run_v10_pro
    return run_v10_pro(pdf_path)


def _save_to_db(out: Dict, pdf_path: str) -> str:
    """Save merged V11 result to DB. Returns job_id."""
    import database
    from pathlib import Path
    p = Path(pdf_path)
    pdf_size = p.stat().st_size if p.exists() else 0
    cls = out.get("page_classification", {}) or {}
    n_pages = cls.get("n_pages") or 0
    summary = cls.get("summary", {}) or {}
    typed_pages = summary.get("TYPED", 0)
    hw_pages = summary.get("HANDWRITTEN", 0)

    job_id = database.create_job(
        pdf_name=p.name,
        pdf_path=str(p),
        pdf_size=pdf_size,
        total_pages=n_pages,
        text_pages=typed_pages,
        image_pages=hw_pages,
    )

    decl = out.get("declaration", {}) or {}
    if decl:
        # Map snake_case → DB column names matching V7 schema
        db_decl = {
            "declaration_no": decl.get("declaration_no") or decl.get("Declaration No"),
            "declaration_date": decl.get("declaration_date") or decl.get("Declaration Date"),
            "importer_name": decl.get("importer_name") or decl.get("Importer (Name)"),
            "consignor_name": decl.get("consignor_name") or decl.get("Consignor (Name)"),
            "invoice_number": decl.get("invoice_number") or decl.get("Invoice Number"),
            "invoice_number_customs_declaration": decl.get("invoice_number_customs"),
            "invoice_number_commercial_invoice": decl.get("invoice_number_commercial"),
            "currency": decl.get("currency") or decl.get("Currency"),
            "currency_2": decl.get("currency_2") or decl.get("Currency 2"),
            "exchange_rate": decl.get("exchange_rate") or decl.get("Exchange Rate"),
            "invoice_price": decl.get("invoice_price") or decl.get("Invoice Price"),
            "total_customs_value": decl.get("total_customs_value") or decl.get("Total Customs Value"),
            "import_export_customs_duty": decl.get("customs_duty") or decl.get("Import/Export Customs Duty"),
            "commercial_tax_ct": decl.get("commercial_tax") or decl.get("Commercial Tax (CT)"),
            "advance_income_tax_at": decl.get("advance_income_tax") or decl.get("Advance Income Tax (AT)"),
            "security_fee_sf": decl.get("security_fee") or decl.get("Security Fee (SF)"),
            "maccs_service_fee_mf": decl.get("maccs_service_fee") or decl.get("MACCS Service Fee (MF)"),
            "exemption_reduction": decl.get("exemption") or decl.get("Exemption/Reduction"),
            "document_format": out.get("document_format"),
        }
        try:
            database.save_declarations(job_id, [db_decl])
        except Exception as e:
            print(f"[V11 DB] save_declarations error: {e}")

    items = out.get("items") or []
    if items:
        db_items = []
        for it in items:
            db_items.append({
                "item_name": it.get("item_name") or it.get("Item name"),
                "customs_duty_rate": it.get("customs_duty_rate") or it.get("Customs duty rate"),
                "quantity": it.get("quantity") or it.get("Quantity (1)"),
                "invoice_unit_price": it.get("invoice_unit_price") or it.get("Invoice unit price"),
                "cif_unit_price": it.get("cif_unit_price") or it.get("CIF unit price"),
                "currency": it.get("currency") or it.get("Currency"),
                "commercial_tax_percent": it.get("commercial_tax_pct") or it.get("Commercial tax %"),
                "exchange_rate": it.get("exchange_rate") or it.get("Exchange Rate (1)"),
                "hs_code": it.get("hs_code") or it.get("HS Code"),
                "origin_country": it.get("origin") or it.get("Origin Country"),
                "customs_value_mmk": it.get("customs_value_mmk") or it.get("Customs Value (MMK)"),
            })
        try:
            database.save_items(job_id, db_items)
        except Exception as e:
            print(f"[V11 DB] save_items error: {e}")

    try:
        database.update_job_metrics(
            job_id,
            processing_time=out.get("duration_seconds", 0) or 0,
            cost=out.get("cost", 0) or 0,
            accuracy=0,
        )
        database.update_job_status(job_id, "COMPLETED")
    except Exception as e:
        print(f"[V11 DB] update_job error: {e}")

    # Mark for human review (V11 → side-by-side approve UI)
    try:
        database.update_review_status(job_id, "pending_review")
    except Exception as e:
        print(f"[V11 DB] update_review_status error: {e}")

    # Persist field bboxes (PDF↔form highlight)
    try:
        bb = out.get("field_bboxes") or {}
        if bb:
            database.update_job_field_bboxes(job_id, bb)
    except Exception as e:
        print(f"[V11 DB] update_job_field_bboxes error: {e}")

    # Compute document_type from page_classification summary
    cls = out.get("page_classification", {}) or {}
    summary = cls.get("summary", {}) or {}
    typed = summary.get("TYPED", 0)
    hw = summary.get("HANDWRITTEN", 0)
    if typed > 0 and hw > 0:
        doc_type = "MIXED"
    elif hw > 0:
        doc_type = "HANDWRITTEN"
    elif typed > 0:
        doc_type = "TYPED"
    else:
        doc_type = "UNKNOWN"

    try:
        database.update_job_usage(
            job_id=job_id,
            tokens_in=out.get("tokens_in", 0),
            tokens_out=out.get("tokens_out", 0),
            document_type=doc_type,
            pipeline_mode="v11",
        )
    except Exception as e:
        print(f"[V11 DB] update_job_usage error: {e}")

    return job_id


def run(pdf_path: str, job_id: Optional[str] = None) -> Dict:
    """End-to-end V11 dispatch. Returns merged result + trace.

    Args:
        pdf_path: source PDF.
        job_id: optional live-event job identifier (threaded through every stage).
                If not supplied, a uuid is auto-generated so events can stream.
    """
    if not job_id:
        job_id = f"v11-{uuid.uuid4().hex[:12]}"

    t0 = time.time()
    filename = Path(pdf_path).name
    current_stage = "init"

    # ─── Phase 0: JOB_START ───
    try:
        # n_pages not yet known until classifier; use fitz quickly if possible
        n_pages_est = None
        try:
            import fitz
            _d = fitz.open(str(pdf_path))
            n_pages_est = len(_d)
            _d.close()
        except Exception:
            n_pages_est = None
        # File size (best-effort).
        size_mb = None
        try:
            _bytes = Path(pdf_path).stat().st_size
            size_mb = round(_bytes / (1024 * 1024), 2)
        except Exception:
            size_mb = None
        _emit(job_id, "JOB_START", {
            "file": filename,
            "pages": n_pages_est,
            "size_mb": size_mb,
            "pipeline": "V11",
            "label": PIPELINE_LABEL["V11"],
        })
        if event_logger:
            try:
                event_logger.log_job(
                    action="JOB_START", user=None, job_id=job_id,
                    status="OK",
                    details=f"V11 routing started for {filename}",
                    payload={"pipeline": "v11", "pdf": pdf_path,
                             "pages": n_pages_est, "job_id": job_id},
                )
            except Exception:
                pass
    except Exception:
        pass

    try:
        out = {
            "pdf": pdf_path,
            "job_id_live": job_id,
            "declaration": {},
            "items": [],
            "document_format": None,
            "cost": 0,
            "duration_seconds": 0,
            "trace": [],
            "total_pages": n_pages_est or 0,
            "text_pages": 0,
            "image_pages": 0,
            "attachment_pages": 0,
            "pipeline_version": "v11",
            "model_used": "V11 Maestro",
            "processed_at": "",
        }

        # ─── Phase 1: Page classifier ───
        current_stage = "classifier"
        try:
            cls = classify_pages(pdf_path)
            out["page_classification"] = cls
            try:
                _summary = cls.get("summary", {}) or {}
                out["total_pages"] = cls.get("n_pages") or n_pages_est or 0
                out["text_pages"] = int(_summary.get("TYPED", 0) or 0)
                out["image_pages"] = int(_summary.get("HANDWRITTEN", 0) or 0)
                out["attachment_pages"] = int(_summary.get("ATTACHMENT", 0) or 0)
            except Exception:
                pass
            out["trace"].append({"phase": "classifier",
                                  "n_pages": cls.get("n_pages"),
                                  "summary": cls.get("summary"),
                                  "pages": [{"page": p["page"], "label": p["label"]}
                                            for p in cls.get("pages", [])]})

            # Per-page CLASSIFY events.
            # NOTE: classifier exposes label + qualitative confidence + reason,
            # but does NOT compute pen_ratio / typed_ratio / box_count features.
            # Those keys are emitted as None placeholders.
            for p in cls.get("pages", []) or []:
                verdict = _label_to_verdict(p.get("label"))
                conf = _conf_to_float(p.get("confidence"))
                payload = {
                    "page": p.get("page"),
                    "verdict": verdict,
                    "conf": conf,
                    "features": {
                        "pen_ratio": None,
                        "typed_ratio": None,
                        "box_count": None,
                    },
                    "reason": p.get("reason", ""),
                    # Evidence = classifier's free-text justification.
                    # Pass-through of `reason` under a stable, frontend-friendly key.
                    "evidence": p.get("reason", "") or "",
                }
                _emit(job_id, "CLASSIFY", payload)
                _log_event("PAGE_CLASSIFY", payload, job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "classifier", "error": str(e)})
            cls = {"pages": [{"page": 1, "label": "TYPED"}], "n_pages": 1,
                   "summary": {"TYPED": 1, "HANDWRITTEN": 0, "ATTACHMENT": 0}}

        # ─── Phase 2: Split PDF ───
        current_stage = "split"
        try:
            splits = split_pdf_by_labels(pdf_path, cls.get("pages", []))
            out["trace"].append({"phase": "split",
                                  "buckets": splits.get("page_buckets")})
        except Exception as e:
            out["trace"].append({"phase": "split", "error": str(e)})
            splits = {"TYPED": pdf_path, "HANDWRITTEN": None, "ATTACHMENT": None,
                      "page_buckets": {"TYPED": [], "HANDWRITTEN": [], "ATTACHMENT": []}}

        typed_pdf = splits.get("TYPED")
        hw_pdf = splits.get("HANDWRITTEN")

        # If neither typed nor HW (rare), default to V7 on full PDF
        fallback_to_full = False
        if not typed_pdf and not hw_pdf:
            typed_pdf = pdf_path
            fallback_to_full = True
            out["trace"].append({"phase": "fallback", "reason": "no typed or HW pages — full doc → V7"})

        # ─── ROUTE event ───
        buckets = (splits.get("page_buckets") or
                   {"TYPED": [], "HANDWRITTEN": [], "ATTACHMENT": []})
        v7_pages = list(buckets.get("TYPED") or [])
        v10p_pages = list(buckets.get("HANDWRITTEN") or [])
        dropped = list(buckets.get("ATTACHMENT") or [])
        if fallback_to_full and not v7_pages:
            # Whole-doc fallback → all known pages routed to V7
            try:
                v7_pages = [p.get("page") for p in cls.get("pages", []) if p.get("page")]
            except Exception:
                v7_pages = []
        route_payload = {
            "v7_pages": v7_pages,
            "v10_pro_pages": v10p_pages,
            "dropped": dropped,
        }
        _emit(job_id, "ROUTE", route_payload)
        _log_event("ROUTE", route_payload, job_id=job_id)

        # ─── Phase 3: Parallel V7 + V10 dispatch ───
        current_stage = "dispatch"
        v7_res = None
        v10_res = None
        v7_t0 = v10_t0 = None
        v7_dt = v10_dt = 0.0

        # STAGE_START events fired just before submit.
        # NOTE: ROUTE has already been emitted above with populated buckets,
        # so the frontend always sees ROUTE → STAGE_START in that order.
        if typed_pdf:
            _emit(job_id, "STAGE_START", {
                "pipeline": "V7",
                "label": PIPELINE_LABEL["V7"],
                "pages": v7_pages,
            })
        if hw_pdf:
            _emit(job_id, "STAGE_START", {
                "pipeline": "V10_PRO",
                "label": PIPELINE_LABEL["V10_PRO"],
                "pages": v10p_pages,
            })

        # TODO(STAGE_DETAIL): emit fine-grained mid-pipeline progress events
        # (vision / declaration_assembler / items_assembler / verifier / fee_verifier
        #  for V7; hw_detect / dpi_vote / reader / shape_valid / post_fix for V10_PRO).
        # Skipped here because V7 (pipeline.pipeline.run_pipeline) and V10_PRO
        # (v10_pro.workflow.run) are invoked as opaque blocking calls inside a
        # ThreadPoolExecutor — neither exposes a callback/hook interface, and the
        # spec forbids modifying their internals. To wire this up later, either:
        #   (a) thread `job_id` + an emit callback into both workflows, or
        #   (b) tail their structured logs and re-emit as STAGE_DETAIL.
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {}
            if typed_pdf:
                v7_t0 = time.time()
                futs[ex.submit(_call_v7, typed_pdf)] = "v7"
            if hw_pdf:
                v10_t0 = time.time()
                futs[ex.submit(_call_v10, hw_pdf)] = "v10"
            for f in futs:
                label = futs[f]
                try:
                    res = f.result(timeout=900)
                    if label == "v7":
                        v7_res = res
                        v7_dt = round(time.time() - (v7_t0 or time.time()), 2)
                        out["trace"].append({"phase": "v7", "ok": True,
                                              "items": len(res.get("items", []))})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7",
                            "label": PIPELINE_LABEL["V7"],
                            "duration_s": v7_dt,
                            "cost_usd": float(res.get("cost") or res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(res.get("tokens_in") or 0),
                            "tokens_out": int(res.get("tokens_out") or 0),
                        })
                    else:
                        v10_res = res
                        v10_dt = round(time.time() - (v10_t0 or time.time()), 2)
                        out["trace"].append({"phase": "v10", "ok": True,
                                              "items": len(res.get("items", []))})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V10_PRO",
                            "label": PIPELINE_LABEL["V10_PRO"],
                            "duration_s": v10_dt,
                            "cost_usd": float(res.get("cost") or res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(res.get("tokens_in") or 0),
                            "tokens_out": int(res.get("tokens_out") or 0),
                        })
                except Exception as e:
                    out["trace"].append({"phase": label, "error": str(e)})
                    _pl = "V7" if label == "v7" else "V10_PRO"
                    _emit(job_id, "STAGE_DONE", {
                        "pipeline": _pl,
                        "label": PIPELINE_LABEL[_pl],
                        "duration_s": None,
                        "cost_usd": None,
                        "tokens_in": None,
                        "tokens_out": None,
                        "error": str(e),
                    })

        # ─── Phase 4: Merge ───
        current_stage = "merge"
        try:
            merged = merge_results(v7_res, v10_res)
            out["declaration"] = merged.get("declaration", {})
            # Alias keys for UI compatibility (UI expects V7 schema names)
            _d = out["declaration"]
            _decl_aliases = {
                "invoice_number_customs": "invoice_number_customs_declaration",
                "invoice_number_commercial": "invoice_number_commercial_invoice",
                "customs_duty": "import_export_customs_duty",
                "commercial_tax": "commercial_tax_ct",
                "advance_income_tax": "advance_income_tax_at",
                "security_fee": "security_fee_sf",
                "maccs_service_fee": "maccs_service_fee_mf",
                "exemption": "exemption_reduction",
                "country_origin": "origin_country",
            }
            for src, dst in _decl_aliases.items():
                if src in _d and _d.get(src) not in (None, "") and not _d.get(dst):
                    _d[dst] = _d[src]
            out["items"] = merged.get("items", [])
            # Item aliases too
            _item_aliases = {
                "commercial_tax_pct": "commercial_tax_percent",
                "origin": "origin_country",
            }
            _decl_currency = _d.get("currency") or _d.get("currency_2")
            _item_review_flags = []
            for idx, it in enumerate(out["items"]):
                for src, dst in _item_aliases.items():
                    if src in it and it.get(src) not in (None, "") and not it.get(dst):
                        it[dst] = it[src]
                # Inherit currency from declaration when item-level missing
                if not it.get("currency") and _decl_currency:
                    it["currency"] = _decl_currency
                # Flag missing duty rate (None) only — 0 is often genuine (Form D / FTA / ASEAN preferential)
                _rate = it.get("customs_duty_rate")
                if _rate is None or _rate == "":
                    _item_review_flags.append({
                        "item_index": idx,
                        "field": "customs_duty_rate",
                        "value": _rate,
                        "note": "Missing duty rate — verify against tariff",
                    })
            if _item_review_flags:
                out["item_review_flags"] = _item_review_flags
            out["document_format"] = merged.get("document_format")
            out["cost"] = merged.get("cost", 0)
            out["needs_review"] = merged.get("needs_review", False)
            out["v7_used"] = merged.get("v7_result", {}).get("used", False)
            out["v10_used"] = merged.get("v10_result", {}).get("used", False)

            # Best-effort merge diagnostics. Current merger.py doesn't expose
            # a per-field conflict log, so we reconstruct it locally by
            # comparing V7-normalized vs V10 declaration fields.
            try:
                from v11.agents.merger import _normalize_v7_decl  # type: ignore
                v7_decl_norm = _normalize_v7_decl((v7_res or {}).get("declaration") or {})
            except Exception:
                v7_decl_norm = (v7_res or {}).get("declaration") or {}
            v10_decl = (v10_res or {}).get("declaration") or {}

            conflict_fields = []
            resolved_by = set()
            keys = set(v7_decl_norm.keys()) | set(v10_decl.keys())
            for k in keys:
                a = v7_decl_norm.get(k)
                b = v10_decl.get(k)
                a_present = a not in (None, "", "None")
                b_present = b not in (None, "", "None")
                if a_present and b_present and str(a).strip() != str(b).strip():
                    # V7 wins per merger strategy
                    conflict_fields.append({
                        "name": k,
                        "v7": a,
                        "v10p": b,
                        "winner": "v7",
                        "reason": "v7_typed_priority",
                    })
                    resolved_by.add("v7_typed_priority")
                elif b_present and not a_present:
                    resolved_by.add("v10_pro_fill")
                elif a_present and not b_present:
                    resolved_by.add("v7_only")

            merge_payload = {
                "conflicts": len(conflict_fields),
                "resolved_by": sorted(resolved_by),
                "fields": conflict_fields,
            }
            _emit(job_id, "MERGE", merge_payload)
            _log_event("MERGE", merge_payload, job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "merge", "error": str(e)})

        out["duration_seconds"] = round(time.time() - t0, 1)

        # Aggregate tokens from V7 + V10 PRO
        out["tokens_in"] = (v7_res.get("tokens_in", 0) if v7_res else 0) + \
                           (v10_res.get("tokens_in", 0) if v10_res else 0)
        out["tokens_out"] = (v7_res.get("tokens_out", 0) if v7_res else 0) + \
                            (v10_res.get("tokens_out", 0) if v10_res else 0)
        breakdown = []
        if v7_res:
            breakdown += [{**e, "branch": "v7"} for e in (v7_res.get("cost_breakdown") or [])]
        if v10_res:
            breakdown += [{**e, "branch": "v10_pro"} for e in (v10_res.get("cost_breakdown") or [])]
        out["cost_breakdown"] = breakdown

        # ─── Phase 4.5: Compute field bboxes (best-effort, fitz text search) ───
        try:
            out["field_bboxes"] = _compute_field_bboxes(
                pdf_path, out.get("declaration") or {}, out.get("items") or []
            )
        except Exception as _bbe:
            out["field_bboxes"] = {}
            out["trace"].append({"phase": "bbox", "error": str(_bbe)})

        # ─── Phase 5: Save merged result to DB ───
        current_stage = "db_save"
        db_job_id = None
        try:
            # Retry on transient SQLite "database is locked" errors (event_logger contention).
            _save_err = None
            for _attempt in range(5):
                try:
                    db_job_id = _save_to_db(out, pdf_path)
                    _save_err = None
                    break
                except Exception as _se:
                    _save_err = _se
                    _msg = str(_se).lower()
                    if "locked" in _msg or "busy" in _msg:
                        time.sleep(0.5 * (_attempt + 1))
                        continue
                    raise
            if _save_err is not None:
                raise _save_err
            out["job_id"] = db_job_id
            out["trace"].append({"phase": "db_save", "ok": True, "job_id": db_job_id})
            _emit(job_id, "DB_SAVE", {
                "job_id": db_job_id,
                "decls": 1 if out.get("declaration") else 0,
                "items": len(out.get("items") or []),
            })
        except Exception as e:
            out["trace"].append({"phase": "db_save", "error": str(e)})
            _emit(job_id, "DB_SAVE", {
                "job_id": None,
                "decls": 0,
                "items": len(out.get("items") or []),
                "error": str(e),
            })

        # ─── Final: DONE ───
        total_s = round(time.time() - t0, 2)

        # Derive model_used label from which branches actually ran.
        _v7_used = bool(out.get("v7_used"))
        _v10_used = bool(out.get("v10_used"))
        if _v7_used and _v10_used:
            out["model_used"] = "V11 Maestro (Veritas + Scrivener)"
        elif _v7_used:
            out["model_used"] = "V11 Maestro (Veritas)"
        elif _v10_used:
            out["model_used"] = "V11 Maestro (Scrivener)"
        else:
            out["model_used"] = "V11 Maestro"
        out["processed_at"] = datetime.utcnow().isoformat() + "Z"

        done_payload = {
            "total_s": total_s,
            "total_cost": float(out.get("cost") or 0),
            "total_tokens_in": int(out.get("tokens_in") or 0),
            "total_tokens_out": int(out.get("tokens_out") or 0),
        }
        _emit(job_id, "DONE", done_payload)

        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_SUCCESS", user=None, job_id=db_job_id or job_id,
                    status="OK",
                    duration_ms=int(total_s * 1000),
                    details=f"V11 extracted {len(out['items'])} items, ${out.get('cost', 0):.4f}",
                    payload={"pipeline": "v11",
                             "live_job_id": job_id,
                             "items": len(out["items"]),
                             "cost": out.get("cost", 0),
                             "tokens_in": out.get("tokens_in", 0),
                             "tokens_out": out.get("tokens_out", 0),
                             "v7_used": out.get("v7_used"),
                             "v10_used": out.get("v10_used"),
                             "needs_review": out.get("needs_review")},
                )
        except Exception:
            pass

        _close(job_id)
        return out
    except Exception as e:
        # ─── FAIL ───
        try:
            _emit(job_id, "FAIL", {"stage": current_stage, "error": str(e)})
        except Exception:
            pass
        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_FAIL", user=None, job_id=job_id,
                    status="FAILED",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V11 failed: {e}",
                    error=str(e),
                    payload={"pipeline": "v11", "pdf": pdf_path,
                             "stage": current_stage,
                             "error": str(e)[:500]},
                )
        except Exception:
            pass
        _close(job_id)
        raise


# CLI
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) < 2:
        print("Usage: python -m v11.workflow <pdf_path> [job_id]")
        sys.exit(1)
    _job = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(run(sys.argv[1], job_id=_job), indent=2, ensure_ascii=False, default=str))
