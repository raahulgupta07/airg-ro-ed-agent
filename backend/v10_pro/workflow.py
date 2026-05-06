"""V10 — Handwriting Specialist Extraction Pipeline.
Extends V9 with HW detection, multi-DPI 3-model vote, importer memory."""
import time
from pathlib import Path
from typing import Dict

from v10_pro.agents.filter import filter_pages
from v10_pro.agents.reader import read_pdf, flatten
from v10_pro.agents.verifier import verify, apply_corrections
from v10_pro.agents.hw_detector import detect as detect_hw
from v10_pro.agents.multi_dpi_vote import vote_field
from v10_pro.tools.pdf_tools import slice_pdf as slice_pdf_tool
from v10_pro.tools.math_tools import deterministic_postfix
from v10_pro.tools.db_tools import consignor_canonical, hs_lookup
from v10_pro.tools.memory_tools import get_importer_profile, build_memory_prompt_block

try:
    import event_logger
except Exception:
    event_logger = None

CRITICAL_HW_FIELDS = [
    "declaration_no", "exchange_rate", "invoice_price",
    "total_customs_value", "customs_duty",
]


def _direct_slice(pdf_path: str, pages: list) -> str:
    """Bypass Agno tool wrapper to call the slice helper directly."""
    import os, fitz
    src = fitz.open(pdf_path)
    dst = fitz.open()
    for p in sorted(set(pages)):
        if 1 <= p <= len(src):
            dst.insert_pdf(src, from_page=p - 1, to_page=p - 1)
    out_dir = os.environ.get("V9_TMP_DIR", "/tmp/v9_results")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"_sliced_{Path(pdf_path).stem}.pdf")
    dst.save(out)
    dst.close()
    src.close()
    return out


def _direct_postfix(decl: dict) -> dict:
    """Pure-rule application without going through Agno tool."""
    def _f(x):
        if x in (None, ""): return 0.0
        try: return float(str(x).replace(",", ""))
        except Exception: return 0.0
    notes = []
    cv = _f(decl.get("total_customs_value"))
    duty = _f(decl.get("customs_duty"))
    if cv > 0:
        mf = _f(decl.get("maccs_service_fee"))
        if mf == 0:
            decl["maccs_service_fee"] = round(cv * 0.02)
            notes.append(f"MACCS = 2% × {int(cv)} = {round(cv*0.02)}")
        ct = _f(decl.get("commercial_tax"))
        if ct == 0:
            decl["commercial_tax"] = round((cv + duty) * 0.05)
            notes.append("CT = 5% × (CV+Duty)")
    return notes


def run(pdf_path: str) -> Dict:
    """End-to-end extraction. Returns ExtractionResult-shaped dict."""
    from v10_pro import _cost_tracker
    _cost_tracker.reset()
    t0 = time.time()
    job_id = f"v10pro_{int(t0)}"
    try:
        if event_logger:
            event_logger.log_job(
                action="JOB_START", user=None, job_id=job_id,
                status="OK",
                details=f"V10 PRO started for {Path(pdf_path).name}",
                payload={"pipeline": "v10_pro", "pdf": pdf_path},
            )
    except Exception:
        pass

    try:
        out = {
            "pdf": pdf_path,
            "document_format": None,
            "declaration": {},
            "items": [],
            "pages_kept": [],
            "anomalies": [],
            "postfix_notes": [],
            "duration_seconds": 0,
            "cost_usd": 0,  # TODO wire in cost from agent responses
            "trace": [],
        }

        # ─── Phase 0: HW detector ───
        try:
            hw_info = detect_hw(pdf_path, use_visual=True)
            out["trace"].append({"phase": "hw_detect", **hw_info})
            out["is_handwritten"] = hw_info.get("is_hw", False)
        except Exception as e:
            out["trace"].append({"phase": "hw_detect", "error": str(e)})
            out["is_handwritten"] = False

        # ─── Phase 1: page filter ───
        try:
            kept, meta = filter_pages(pdf_path)
            out["pages_kept"] = kept
            out["trace"].append({"phase": "filter", "kept": kept, "meta": meta})
            if len(kept) < meta.get("n_pages", 0):
                sliced = _direct_slice(pdf_path, kept)
                work_pdf = sliced
            else:
                work_pdf = pdf_path
        except Exception as e:
            out["trace"].append({"phase": "filter", "error": str(e)})
            work_pdf = pdf_path

        # ─── Phase 2: holistic read ───
        raw = read_pdf(work_pdf)
        if not raw:
            out["trace"].append({"phase": "reader", "error": "no_result"})
            out["duration_seconds"] = round(time.time() - t0, 1)
            try:
                if event_logger:
                    event_logger.log_job(
                        action="JOB_SUCCESS", user=None, job_id=job_id,
                        status="OK",
                        duration_ms=int((time.time() - t0) * 1000),
                        details=f"V10 PRO no result for {Path(pdf_path).name}",
                        payload={"pipeline": "v10_pro", "items": 0, "cost": 0},
                    )
            except Exception:
                pass
            return out
        out["trace"].append({"phase": "reader", "ok": True,
                              "uncertain_fields": raw.get("uncertain_fields", [])})

        # ─── Phase 3: verifier corrections ───
        verifier_out = verify(work_pdf, raw)
        if verifier_out:
            raw = apply_corrections(raw, verifier_out)
            out["trace"].append({"phase": "verifier", "ok": True,
                                  "n_corrections": len(verifier_out.get("corrections") or {})})
        else:
            out["trace"].append({"phase": "verifier", "skipped": True})

        # ─── Phase 4: flatten ───
        flat = flatten(raw)
        out["document_format"] = flat.get("document_format")
        out["declaration"] = flat.get("declaration") or {}
        out["items"] = flat.get("items") or []
        out["anomalies"] = flat.get("anomalies") or []

        # ─── Phase 4a: HW Multi-DPI 3-Model Vote (only if handwritten) ───
        needs_review = bool(out.get("is_handwritten"))
        if out.get("is_handwritten"):
            for fld in CRITICAL_HW_FIELDS:
                current = out["declaration"].get(fld) or ""
                try:
                    vote = vote_field(work_pdf, page=1, field_name=fld,
                                       current_value=str(current))
                    consensus = vote.get("consensus")
                    winner = vote.get("winner")
                    if winner and winner != current and consensus in ("unanimous", "majority"):
                        out["declaration"][fld] = winner
                        out["trace"].append({"phase": "hw_vote", "field": fld,
                                              "old": str(current), "new": winner,
                                              "consensus": consensus,
                                              "votes": vote.get("votes")})
                    else:
                        out["trace"].append({"phase": "hw_vote", "field": fld,
                                              "consensus": consensus,
                                              "winner": winner})
                    if vote.get("needs_review"):
                        needs_review = True
                except Exception as e:
                    out["trace"].append({"phase": "hw_vote", "field": fld, "error": str(e)})

        # ─── Phase 4b: decl-no zoom (400 DPI Opus re-read) ───
        try:
            from v10_pro.agents.decl_zoom import zoom_declaration_no, maybe_override
            zoom_res = zoom_declaration_no(work_pdf, out["declaration"].get("declaration_no") or "")
            if zoom_res:
                out["declaration"], override_note = maybe_override(out["declaration"], zoom_res)
                out["trace"].append({"phase": "decl_zoom", "ok": True,
                                      "matches_draft": zoom_res.get("matches_draft"),
                                      "zoom_value": zoom_res.get("value"),
                                      "confidence": zoom_res.get("confidence"),
                                      "override": override_note})
            else:
                out["trace"].append({"phase": "decl_zoom", "skipped": True})
        except Exception as e:
            out["trace"].append({"phase": "decl_zoom", "error": str(e)})

        # ─── Phase 5: deterministic post-fix ───
        notes = _direct_postfix(out["declaration"])
        # If decl_zoom overrode, append note
        for t in out["trace"]:
            if t.get("phase") == "decl_zoom" and t.get("override"):
                notes.append(t["override"])
        # consignor canonical
        cn = out["declaration"].get("consignor_name") or ""
        imp = out["declaration"].get("importer_name") or ""
        if cn and imp:
            try:
                r = consignor_canonical.entrypoint(name=cn, importer=imp) \
                    if hasattr(consignor_canonical, "entrypoint") \
                    else None
            except Exception:
                r = None
            # Best-effort fallback: read JSON directly
            if not r:
                import json
                from pathlib import Path as _P
                p = _P(__file__).parent / "knowledge" / "consignors.json"
                try:
                    seeds = json.loads(p.read_text())
                    imp_key = next((k for k in seeds if k in imp.upper()), None)
                    if imp_key:
                        first = cn.lower().split()[0] if cn.split() else ""
                        for c in seeds[imp_key]:
                            cf = c.lower().split()[0] if c.split() else ""
                            if cf and first and cf == first and c != cn:
                                out["declaration"]["consignor_name"] = c
                                notes.append(f"consignor: '{cn}' → '{c}'")
                                break
                except Exception:
                    pass
        out["postfix_notes"] = notes
        out["needs_review"] = needs_review or out.get("is_handwritten", False)
        out["duration_seconds"] = round(time.time() - t0, 1)
        out["cost_usd"] = _cost_tracker.total()
        breakdown = _cost_tracker.get_entries()
        out["cost_breakdown"] = breakdown
        out["tokens_in"] = sum(int(e.get("prompt_tokens") or 0) for e in breakdown)
        out["tokens_out"] = sum(int(e.get("completion_tokens") or 0) for e in breakdown)

        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_SUCCESS", user=None, job_id=job_id,
                    status="OK",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V10 PRO extracted {len(out['items'])} items, ${out.get('cost_usd', 0):.4f}",
                    payload={"pipeline": "v10_pro",
                             "items": len(out["items"]),
                             "cost": out.get("cost_usd", 0),
                             "tokens_in": out.get("tokens_in", 0),
                             "tokens_out": out.get("tokens_out", 0),
                             "is_handwritten": out.get("is_handwritten"),
                             "needs_review": out.get("needs_review")},
                )
        except Exception:
            pass
        return out
    except Exception as e:
        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_FAIL", user=None, job_id=job_id,
                    status="FAILED",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V10 PRO failed: {e}",
                    error=str(e),
                    payload={"pipeline": "v10_pro", "pdf": pdf_path,
                             "error": str(e)[:500]},
                )
        except Exception:
            pass
        raise


# CLI entry: python -m v9.workflow PDF_PATH
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) < 2:
        print("Usage: python -m v9.workflow <pdf_path>")
        sys.exit(1)
    result = run(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
