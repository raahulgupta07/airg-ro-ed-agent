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
from v11.tools import reconcile as _reconcile
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


def _call_typed(typed_pdf: str, use_presto: bool = False,
                full_pdf: str = None, presto_pages=None) -> Dict:
    """Typed-page extraction with the V12 Presto fast-path + math-gated fallback.

    When `use_presto` (flag on AND every typed page is digital), try Presto first:
    read the text layer of the typed + attachment pages (`presto_pages` of the
    ORIGINAL `full_pdf`, so misrouted item pages the classifier dropped into
    ATTACHMENT are still seen) → one schema call. Keep it ONLY if the reconcile
    equation balances (Σ item customs values == declared total); otherwise fall
    back to V7 Veritas on the typed slice so accuracy never regresses.
    `use_presto=False` → behaves exactly like V7.
    """
    if use_presto:
        try:
            from v11.presto import run as presto_run
            from v11.tools import reconcile as _rc
            from v11.tools import self_correct as _sc
            res = presto_run(full_pdf or typed_pdf, pages=presto_pages)
            v = _rc.reconcile(res.get("declaration") or {}, res.get("items") or [])
            if v.get("checked") and v.get("balanced"):
                res["_engine"] = "presto"
                return res
            # Gate failed → try targeted self-correction (fix only the broken
            # header field) BEFORE any slow fallback.
            cor = _sc.correct(full_pdf or typed_pdf, res.get("declaration") or {},
                              res.get("items") or [], v, header_page=1)
            if cor.get("corrected") and cor["verdict"].get("balanced"):
                res["declaration"] = cor["declaration"]
                res["_engine"] = "presto+selfcorrect"
                res.setdefault("trace", []).append({"phase": "self_correct", "log": cor["log"]})
                return res
            print(f"[Presto] gap {v.get('gap_pct')}% — falling back to V7 Veritas")
        except Exception as e:
            print(f"[Presto] fast-path error, falling back to V7: {e}")
    res = _call_v7(typed_pdf)
    if isinstance(res, dict):
        res.setdefault("_engine", "v7")
    return res


def _call_scribe(pdf_path: str) -> Dict:
    """V14 Atlas handwriting engine — V13 Scribe (vision vote + math gates)."""
    from v13.scribe import run as run_scribe
    res = run_scribe(pdf_path)
    if isinstance(res, dict):
        res.setdefault("_engine", "scribe")
    return res


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
            # Reconciliation verdict (items_sum vs declared total) — the gate.
            # save_declarations reads the underscore-prefixed metadata key.
            "_cross_val_passed": 1 if out.get("cross_val_passed") else 0,
        }
        # Persist sanity_flags (item_count_mismatch, currency_rate, etc) for review UI
        _sflags = out.get("sanity_flags") or []
        if _sflags:
            import json as _json
            db_decl["_sanity_flags_json"] = _json.dumps(_sflags)
            db_decl["_document_format"] = out.get("document_format")
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

    # Compute accuracy from extraction signals:
    # - decl populated rate: how many of expected fields filled
    # - items populated rate: how many cells filled per item
    # - cross_val flag (V7 inside V11)
    try:
        _decl = out.get("declaration") or {}
        _items = out.get("items") or []
        _expected_decl = ['declaration_no','declaration_date','importer_name','consignor_name',
                          'invoice_number','currency','exchange_rate','invoice_price',
                          'total_customs_value','customs_duty','commercial_tax',
                          'advance_income_tax','maccs_service_fee']
        _decl_filled = sum(1 for f in _expected_decl if _decl.get(f) not in (None, '', 0))
        _decl_score = _decl_filled / len(_expected_decl)
        _expected_item = ['item_name','hs_code','quantity','invoice_unit_price','customs_value_mmk','origin']
        if _items:
            _item_filled = 0
            _item_total = 0
            for _it in _items:
                for _f in _expected_item:
                    _item_total += 1
                    if _it.get(_f) not in (None, '', 0):
                        _item_filled += 1
            _item_score = _item_filled / max(1, _item_total)
        else:
            _item_score = 0
        # Weighted: decl 60%, items 40%
        _accuracy_pct = round((_decl_score * 0.6 + _item_score * 0.4) * 100, 1)
    except Exception:
        _accuracy_pct = 0

    try:
        database.update_job_metrics(
            job_id,
            processing_time=out.get("duration_seconds", 0) or 0,
            cost=out.get("cost", 0) or 0,
            accuracy=_accuracy_pct,
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

    # Persist model_used + processed_at for UI display
    try:
        import database as _db
        _conn = _db._connect()
        _cur = _conn.cursor()
        _proc = out.get("processed_at") or None
        if _proc == "":
            _proc = None
        _cur.execute(
            "UPDATE jobs SET model_used = ?, processed_at = ? WHERE job_id = ?",
            (out.get("model_used"), _proc, job_id),
        )
        _conn.commit()
        _conn.close()
    except Exception as e:
        print(f"[V11 DB] update model/processed_at error: {e}")

    return job_id


def run(pdf_path: str, job_id: Optional[str] = None, engine: str = "auto") -> Dict:
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
        # V12 Presto eligibility: flag ON and every TYPED page is digital (has a
        # text layer). Flag off → identical to today's V7 path.
        from v11.config import PRESTO_ENABLED
        _typed_meta = [p for p in cls.get("pages", []) if (p.get("label") or "").upper() == "TYPED"]
        _typed_digital = bool(_typed_meta) and all(p.get("has_text_layer") for p in _typed_meta)
        # Per-job engine choice overrides the global flag:
        #   "presto" → force fast-path (only takes effect if typed pages digital)
        #   "classic" → force V7 Veritas
        #   "auto"   → follow PRESTO_ENABLED
        _eng = (engine or "auto").lower()
        if _eng in ("presto", "atlas"):
            _use_presto = _typed_digital
        elif _eng == "classic":
            _use_presto = False
        else:
            _use_presto = bool(PRESTO_ENABLED and _typed_digital)
        # V14 Atlas: also route handwritten pages to Scribe (V13) instead of V10 PRO.
        _use_scribe = (_eng == "atlas")
        # Presto reads typed + attachment pages of the ORIGINAL pdf so misrouted
        # item pages (classifier put them in ATTACHMENT) are still captured.
        _presto_pages = sorted(set((buckets.get("TYPED") or []) + (buckets.get("ATTACHMENT") or [])))
        if _use_presto:
            out["trace"].append({"phase": "route_typed", "engine": "presto",
                                  "pages": _presto_pages})

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {}
            if typed_pdf:
                v7_t0 = time.time()
                futs[ex.submit(_call_typed, typed_pdf, _use_presto, pdf_path, _presto_pages)] = "v7"
            if hw_pdf:
                v10_t0 = time.time()
                futs[ex.submit(_call_scribe if _use_scribe else _call_v10, hw_pdf)] = "v10"
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
            out["sanity_flags"] = merged.get("sanity_flags") or []
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

        # ─── Phase 4.25: Item-count mismatch fallback ───
        # If sanity flagged item_count_mismatch (extracted < PDF-declared total items),
        # re-run V7 on the FULL original PDF (forcing all pages through Veritas) and
        # replace items if recovery yields more. Catches PageClassifier misroute
        # (tail pages wrongly verdicted INKED/EXTRA → Veritas never saw them).
        try:
            _sflags = (merged or {}).get("sanity_flags") or []
            _mismatch = next((f for f in _sflags if f.startswith("item_count_mismatch")), None)
            _already_full = bool(fallback_to_full) or (typed_pdf == pdf_path and not hw_pdf)
            if _mismatch and not _already_full:
                out["trace"].append({"phase": "recover", "reason": _mismatch,
                                     "action": "rerun V7 on full PDF"})
                _emit(job_id, "STAGE_START", {
                    "pipeline": "V7_RECOVER",
                    "label": "Veritas recovery — full PDF",
                    "pages": list(range(1, (cls.get("n_pages") or 0) + 1)),
                })
                _r_t0 = time.time()
                try:
                    # Bound recovery to 600s to prevent worker hang on large PDFs
                    with ThreadPoolExecutor(max_workers=1) as _rex:
                        _rfut = _rex.submit(_call_v7, pdf_path)
                        full_res = _rfut.result(timeout=600)
                    _r_dt = round(time.time() - _r_t0, 2)
                    full_items_n = len(full_res.get("items") or [])
                    current_items_n = len(out.get("items") or [])
                    if full_items_n > current_items_n:
                        out["items"] = full_res.get("items", [])
                        # Track recovery in tokens + cost
                        out["trace"].append({"phase": "recover", "ok": True,
                                              "items_before": current_items_n,
                                              "items_after": full_items_n})
                        # Re-flag (count may now match)
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RECOVER",
                            "label": "Veritas recovery",
                            "duration_s": _r_dt,
                            "cost_usd": float(full_res.get("cost") or full_res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(full_res.get("tokens_in") or 0),
                            "tokens_out": int(full_res.get("tokens_out") or 0),
                            "items_recovered": full_items_n - current_items_n,
                        })
                        # Accumulate recovery cost/tokens (aggregated below)
                        v7_res = v7_res or {}
                        v7_res["tokens_in"] = int(v7_res.get("tokens_in", 0)) + int(full_res.get("tokens_in", 0))
                        v7_res["tokens_out"] = int(v7_res.get("tokens_out", 0)) + int(full_res.get("tokens_out", 0))
                        v7_res["cost"] = float(v7_res.get("cost", 0) or 0) + float(full_res.get("cost", 0) or 0)
                    else:
                        out["trace"].append({"phase": "recover", "ok": False,
                                              "items_before": current_items_n,
                                              "items_after": full_items_n,
                                              "reason": "no new items recovered"})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RECOVER",
                            "label": "Veritas recovery — no gain",
                            "duration_s": _r_dt,
                        })
                except Exception as _re:
                    out["trace"].append({"phase": "recover", "error": str(_re)})
                    _emit(job_id, "STAGE_DONE", {
                        "pipeline": "V7_RECOVER",
                        "label": "Veritas recovery",
                        "error": str(_re),
                    })
        except Exception as _e:
            out["trace"].append({"phase": "recover_guard", "error": str(_e)})

        # ─── Phase 4.3: Empty-declaration safety net ───
        # If the merged DECLARATION came back empty (no total + no importer), the
        # classifier misrouted the real declaration page (e.g. a bundled release
        # order where the customs declaration was tagged ATTACHMENT/"blank"). The
        # item-recovery paths above only restore ITEMS, never the header — so an
        # empty header would ship empty. Here we re-run Veritas (V7) on the FULL
        # PDF and adopt its declaration + items wholesale. Engine-agnostic; only
        # fires when there is genuinely nothing to lose.
        try:
            _decl_now = out.get("declaration") or {}
            _has_total = _reconcile._to_float(_decl_now.get("total_customs_value")
                                              or _decl_now.get("Total Customs Value"))
            _imp = _decl_now.get("importer_name") or _decl_now.get("Importer (Name)") or ""
            _has_importer = bool(str(_imp).strip())
            _decl_empty = not _has_total and not _has_importer
            _already_full2 = bool(fallback_to_full) or (typed_pdf == pdf_path and not hw_pdf)
            if _decl_empty and not _already_full2:
                out["trace"].append({"phase": "decl_rescue",
                                     "reason": "empty declaration after merge",
                                     "action": "rerun V7 on full PDF (adopt header+items)"})
                _emit(job_id, "STAGE_START", {
                    "pipeline": "V7_RESCUE",
                    "label": "Veritas rescue — empty declaration",
                    "pages": list(range(1, (cls.get("n_pages") or 0) + 1)),
                })
                _rs_t0 = time.time()
                try:
                    with ThreadPoolExecutor(max_workers=1) as _rsx:
                        _rsfut = _rsx.submit(_call_v7, pdf_path)
                        rescue_raw = _rsfut.result(timeout=600)
                    rescue_norm = merge_results(rescue_raw, None)
                    rdecl = rescue_norm.get("declaration") or {}
                    ritems = rescue_norm.get("items") or []
                    rtotal = _reconcile._to_float(rdecl.get("total_customs_value"))
                    if rtotal and rtotal > 0:
                        out["declaration"] = rdecl
                        if len(ritems) > len(out.get("items") or []):
                            out["items"] = ritems
                        out["v7_used"] = True   # surfaces in model_used label
                        out["trace"].append({"phase": "decl_rescue", "ok": True,
                                             "total_customs_value": rtotal,
                                             "items": len(out.get("items") or [])})
                        # Accumulate rescue cost/tokens
                        v7_res = v7_res or {}
                        v7_res["tokens_in"] = int(v7_res.get("tokens_in", 0)) + int(rescue_raw.get("tokens_in", 0) or 0)
                        v7_res["tokens_out"] = int(v7_res.get("tokens_out", 0)) + int(rescue_raw.get("tokens_out", 0) or 0)
                        v7_res["cost"] = float(v7_res.get("cost", 0) or 0) + float(rescue_raw.get("cost", 0) or rescue_raw.get("cost_usd", 0) or 0)
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RESCUE", "label": "Veritas rescue",
                            "duration_s": round(time.time() - _rs_t0, 2),
                            "total_customs_value": rtotal,
                            "items": len(out.get("items") or [])})
                    else:
                        out["trace"].append({"phase": "decl_rescue", "ok": False,
                                             "reason": "full V7 also produced no total"})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RESCUE", "label": "Veritas rescue — no header",
                            "duration_s": round(time.time() - _rs_t0, 2)})
                except Exception as _rse:
                    out["trace"].append({"phase": "decl_rescue", "error": str(_rse)})
                    _emit(job_id, "STAGE_DONE", {"pipeline": "V7_RESCUE",
                                                 "label": "Veritas rescue", "error": str(_rse)})
        except Exception as _e:
            out["trace"].append({"phase": "decl_rescue_guard", "error": str(_e)})

        # ─── Phase 4.4: Reconciliation gate (the common invariant) ───
        # One guard, one chokepoint: the declared customs total must equal the
        # sum of item customs values. Any upstream leak — misclassified page,
        # split bug, V7/V10 item miss — surfaces here as a broken equation.
        # When it breaks and ATTACHMENT pages exist, try to RECOVER (those pages
        # are often misrouted item pages). If still off, FLAG for human review.
        current_stage = "reconcile"
        try:
            verdict = _reconcile.reconcile(out.get("declaration") or {}, out.get("items") or [])
            recovery = {"attempted": False, "added_items": 0, "from_pages": []}

            if (not verdict["balanced"]
                    and _reconcile.recovery_enabled()
                    and splits.get("ATTACHMENT")):
                recovery["attempted"] = True
                recovery["from_pages"] = list(buckets.get("ATTACHMENT") or [])
                attach_pdf = splits.get("ATTACHMENT")
                out["trace"].append({"phase": "reconcile_recover",
                                     "gap_pct": verdict["gap_pct"],
                                     "attachment_pages": recovery["from_pages"]})
                try:
                    # Re-extract the dropped slice with Veritas, normalize via merger.
                    rec_raw = _call_v7(attach_pdf)
                    rec_norm = merge_results(rec_raw, None)
                    rec_items = rec_norm.get("items") or []
                    before_n = len(out["items"])
                    candidate = _reconcile.merge_recovered_items(out["items"], rec_items)
                    cand_verdict = _reconcile.reconcile(out.get("declaration") or {}, candidate)
                    # Keep recovered items only if they move us toward balance.
                    if abs(cand_verdict["gap_value"]) <= abs(verdict["gap_value"]):
                        out["items"] = candidate
                        recovery["added_items"] = len(out["items"]) - before_n
                        verdict = cand_verdict
                        # Stash recovered usage; applied after token aggregation
                        # below (which would otherwise overwrite tokens_in/out).
                        recovery["cost"] = float(rec_raw.get("cost") or rec_raw.get("cost_usd") or 0)
                        recovery["tokens_in"] = int(rec_raw.get("tokens_in") or 0)
                        recovery["tokens_out"] = int(rec_raw.get("tokens_out") or 0)
                        recovery["cost_breakdown"] = [{**e, "branch": "v7_recover"}
                                                      for e in (rec_raw.get("cost_breakdown") or [])]
                except Exception as _re:
                    out["trace"].append({"phase": "reconcile_recover", "error": str(_re)})

            # Verdict drives the human-review gate. Unbalanced OR un-checkable
            # (no trustworthy anchor) → force review. Never ship a silent gap.
            out["cross_val_passed"] = bool(verdict["checked"] and verdict["balanced"])
            out["reconcile"] = {**verdict, "recovery": recovery}
            if not out["cross_val_passed"]:
                out["needs_review"] = True

            # Per-row math gate: a suspect individual item (value ≠ qty×price×rate)
            # → force review even if the total balances.
            if verdict.get("rows_checked") and not verdict.get("rows_ok"):
                out["needs_review"] = True
                out["bad_rows"] = verdict.get("bad_rows")

            # JUDGE — confidence score → auto-ok vs review (additive, advisory).
            try:
                from v11.tools import judge as _judge
                jv = _judge.judge(out, verdict)
                out["confidence"] = jv
                if jv.get("needs_review"):
                    out["needs_review"] = True
            except Exception:
                pass

            # LEARNER priors — advisory cross-check (e.g. exchange rate out of the
            # importer's learned range). Warnings only; never blocks. Safe on empty DB.
            try:
                from v11.learn import priors as _priors
                warns = _priors.check_against_priors(out.get("declaration") or {})
                if warns:
                    out["prior_warnings"] = warns
                    out["needs_review"] = True
            except Exception:
                pass

            out["trace"].append({"phase": "reconcile",
                                 "balanced": verdict["balanced"],
                                 "checked": verdict["checked"],
                                 "gap_pct": verdict["gap_pct"],
                                 "items_sum": verdict["items_sum"],
                                 "declared_total": verdict["declared_total"],
                                 "recovered": recovery["added_items"]})
            _emit(job_id, "RECONCILE", {
                "balanced": verdict["balanced"],
                "checked": verdict["checked"],
                "gap_pct": verdict["gap_pct"],
                "gap_value": verdict["gap_value"],
                "items_sum": verdict["items_sum"],
                "declared_total": verdict["declared_total"],
                "anchor": verdict["anchor"],
                "recovered_items": recovery["added_items"],
                "recovered_from_pages": recovery["from_pages"],
                "needs_review": out.get("needs_review", False),
            })
            _log_event("RECONCILE", out["reconcile"], job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "reconcile", "error": str(e)})

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
        # Fold in recovery pass usage (reconciliation re-extraction), if any.
        _rec = out.get("reconcile", {}).get("recovery", {}) if isinstance(out.get("reconcile"), dict) else {}
        if _rec.get("added_items"):
            out["tokens_in"] = int(out.get("tokens_in") or 0) + int(_rec.get("tokens_in") or 0)
            out["tokens_out"] = int(out.get("tokens_out") or 0) + int(_rec.get("tokens_out") or 0)
            out["cost"] = round(float(out.get("cost") or 0) + float(_rec.get("cost") or 0), 6)
            breakdown += _rec.get("cost_breakdown") or []
        out["cost_breakdown"] = breakdown

        # ─── Phase 4.5: Compute field bboxes (best-effort, fitz text search) ───
        try:
            out["field_bboxes"] = _compute_field_bboxes(
                pdf_path, out.get("declaration") or {}, out.get("items") or []
            )
        except Exception as _bbe:
            out["field_bboxes"] = {}
            out["trace"].append({"phase": "bbox", "error": str(_bbe)})

        # Derive model_used label BEFORE save (DB stores it; History reads it).
        _v7_used = bool(out.get("v7_used"))
        _v10_used = bool(out.get("v10_used"))
        _eng_choice = (engine or "auto").lower()
        # Atlas version scheme: V14 = unified; V14-1 = Swift (typed),
        # V14-2 = Vision (handwriting). Legacy paths keep their Gen-1 names.
        _ran = []
        if locals().get("_use_presto"):
            _ran.append("V14-1 Swift")
        elif _v7_used:
            _ran.append("Atlas Classic")
        if locals().get("_use_scribe"):
            _ran.append("V14-2 Vision")
        elif _v10_used:
            _ran.append("Atlas Heritage")
        _ran_str = (" (" + " + ".join(_ran) + ")") if _ran else ""
        if _eng_choice == "atlas":
            out["model_used"] = "Atlas V14" + _ran_str
        elif _eng_choice == "presto":
            out["model_used"] = "Atlas V14-1 Swift" + _ran_str
        elif _eng_choice == "classic":
            out["model_used"] = "Atlas Classic" + _ran_str
        else:
            out["model_used"] = "Atlas V14 Core" + _ran_str

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

        # (model_used label already computed before save above.)
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
