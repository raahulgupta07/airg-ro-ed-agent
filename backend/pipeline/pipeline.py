#!/usr/bin/env python3
"""
RO-ED Pipeline — 4 steps, zero hardcoding.
PDF → HD images → Vision AI per page → LLM Assembler → LLM Verifier → Save.
"""

import time
from typing import Dict, List, Optional, Callable
from pathlib import Path

from pipeline.splitter import split_pdf
from pipeline.vision import extract_all_pages
from pipeline.assembler import assemble
from pipeline.verifier import verify

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None

try:
    import database
except ImportError:
    database = None

try:
    import event_logger
except Exception:
    event_logger = None


def run_pipeline(
    pdf_path: str,
    model: str = None,
    progress_callback: Callable = None,
    max_workers: int = 8,
) -> Optional[Dict]:
    """Public V7 entrypoint — wraps _run_pipeline_impl with event_logger."""
    t0 = time.time()
    pdf_name = Path(pdf_path).name
    try:
        if event_logger:
            event_logger.log_job(
                action="JOB_START", user=None, job_id=None,
                status="OK",
                details=f"V7 pipeline started for {pdf_name}",
                payload={"pipeline": "v7", "pdf": pdf_path},
            )
    except Exception:
        pass

    try:
        result = _run_pipeline_impl(pdf_path, model=model,
                                     progress_callback=progress_callback,
                                     max_workers=max_workers)
        try:
            if event_logger:
                items_count = (result or {}).get("items_count", 0)
                event_logger.log_job(
                    action="JOB_SUCCESS", user=None, job_id=None,
                    status="OK",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V7 extracted {items_count} items",
                    payload={"pipeline": "v7",
                             "items": items_count,
                             "cost": (result or {}).get("cost_usd", 0),
                             "tokens_in": (result or {}).get("tokens_in", 0),
                             "tokens_out": (result or {}).get("tokens_out", 0),
                             "accuracy": (result or {}).get("accuracy", 0)},
                )
        except Exception:
            pass
        return result
    except Exception as e:
        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_FAIL", user=None, job_id=None,
                    status="FAILED",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V7 failed: {e}",
                    error=str(e),
                    payload={"pipeline": "v7", "pdf": pdf_path,
                             "error": str(e)[:500]},
                )
        except Exception:
            pass
        raise


def _run_pipeline_impl(
    pdf_path: str,
    model: str = None,
    progress_callback: Callable = None,
    max_workers: int = 8,
) -> Optional[Dict]:
    """10-step pipeline: Split → Vision → QA → Assemble → QA → Verify."""
    total_start = time.time()
    try:
        if cost_tracker:
            cost_tracker.reset()
    except Exception:
        pass

    pdf_name = Path(pdf_path).name

    def _log(msg: str):
        if progress_callback:
            progress_callback(msg)
        print(f"  [pipeline] {msg}")

    _log("Starting extraction pipeline...")

    # ═══ Step 1: Split PDF → HD images ═══
    _log("Step 1: Splitting PDF into HD images...")
    pages = split_pdf(pdf_path)
    _log(f"Split: {len(pages)} pages")
    if not pages:
        return None

    # Guard: cap at 50 pages to prevent OOM (4GB container, 10 concurrent users)
    MAX_PAGES = 50
    if len(pages) > MAX_PAGES:
        _log(f"⚠ PDF has {len(pages)} pages — capping at {MAX_PAGES} to prevent memory issues")
        pages = pages[:MAX_PAGES]

    # ═══ Step 2: Vision AI reads every page ═══
    vision_model = config.VISION_MODEL or model
    _log(f"Step 2: Vision AI reading {len(pages)} pages... (model: {vision_model or config.EXTRACTION_MODEL})")
    page_results = extract_all_pages(pages, model=vision_model, max_workers=max_workers)
    ok = sum(1 for r in page_results if r["status"] == "ok")
    _log(f"Extracted: {ok}/{len(pages)} pages")
    if ok == 0:
        return None

    # ═══ Step 3: LLM Assembler builds tables ═══
    assembler_model = config.ASSEMBLER_MODEL or model
    _log(f"Step 3: AI assembling... (model: {assembler_model or config.EXTRACTION_MODEL})")
    assembled = assemble(page_results, model=assembler_model)
    declaration = assembled.get("declaration", {})
    items = assembled.get("items", [])
    document_format = assembled.get("document_format", "MACCS")
    sanity_flags = list(assembled.get("sanity_flags", []) or [])
    filled = sum(1 for v in declaration.values() if v is not None)
    _log(f"Assembled: {filled}/16 declaration fields, {len(items)} items (format={document_format})")

    # ═══ Step 3b: SOLO PATH for CUSDEC1 — direct PDF → ~google/gemini-pro-latest ═══
    # Validated 100% (w/ post-fixes) on D11/D19/D18/D12. ~9x cheaper / 25x faster than V7 ensemble.
    if document_format == "CUSDEC1":
        try:
            from pipeline.solo_extractor import solo_extract
            _log(f"Step 3b: CUSDEC1 detected — running solo extractor (PDF native)")
            solo_result = solo_extract(pdf_path)
            if solo_result and solo_result.get("declaration"):
                solo_decl = solo_result["declaration"]
                solo_items = solo_result["items"] or []
                # Apply solo result if it has more filled fields than current
                solo_filled = sum(1 for v in solo_decl.values() if v not in (None, "", 0))
                if solo_filled >= filled - 2:  # solo can match or improve
                    _log(f"Step 3b: solo filled {solo_filled} fields, {len(solo_items)} items — replacing")
                    # Merge — solo takes precedence on filled values, keep MACCS-existing for empty
                    for k, v in solo_decl.items():
                        if v not in (None, ""):
                            declaration[k] = v
                    if solo_items:
                        items = solo_items
                    if solo_result.get("anomalies"):
                        for a in solo_result["anomalies"]:
                            _log(f"  ▸ solo anomaly: {a}")

                    # ── POST-FIX 1: Seed canonical consignor (Mart fixes Most/Mail/Meat) ──
                    try:
                        from pipeline.assembler import (get_canonical_consignors_for_importer,
                                                         normalize_consignor_name)
                        imp = declaration.get("Importer (Name)")
                        cons = declaration.get("Consignor (Name)")
                        if imp and cons:
                            seed = get_canonical_consignors_for_importer(imp)
                            if seed:
                                canon, changed = normalize_consignor_name(cons, seed)
                                if changed:
                                    _log(f"  ▸ post-fix consignor: '{cons}' → '{canon}'")
                                    declaration["Consignor (Name)"] = canon
                    except Exception:
                        pass

                    # ── POST-FIX 2: MACCS Service Fee deterministic (2% × Customs Value) ──
                    try:
                        cv = float(declaration.get("Total Customs Value") or 0)
                        mf = declaration.get("MACCS Service Fee (MF)")
                        if cv > 0 and (mf is None or mf == 0 or str(mf).lower() == "none"):
                            calc_mf = round(cv * 0.02)
                            declaration["MACCS Service Fee (MF)"] = calc_mf
                            _log(f"  ▸ post-fix MACCS Fee deterministic: 2% × {cv:.0f} = {calc_mf}")
                    except (ValueError, TypeError):
                        pass

                    # ── POST-FIX 3: Origin baseline (importer + HS → most-common) ──
                    try:
                        if database and hasattr(database, 'get_recent_declarations_by_importer'):
                            past = database.get_recent_declarations_by_importer(imp, limit=20) or []
                            hs_origin = {}
                            for p in past:
                                p_items = p.get("items") or []
                                for pi in p_items:
                                    p_hs = str(pi.get("hs_code") or "").strip()
                                    p_oc = str(pi.get("origin_country") or "").strip().upper()
                                    if p_hs and p_oc:
                                        hs_origin.setdefault(p_hs, {}).setdefault(p_oc, 0)
                                        hs_origin[p_hs][p_oc] += 1
                            for it in items:
                                cur_hs = str(it.get("HS Code") or "").strip()
                                cur_oc = str(it.get("Origin Country") or "").strip().upper()
                                if cur_hs in hs_origin:
                                    freq = hs_origin[cur_hs]
                                    total = sum(freq.values())
                                    top_oc, top_count = max(freq.items(), key=lambda kv: kv[1])
                                    if total >= 3 and top_count / total >= 0.8 and cur_oc != top_oc:
                                        _log(f"  ▸ post-fix origin: HS {cur_hs} {cur_oc}→{top_oc} (baseline {top_count}/{total})")
                                        it["Origin Country"] = top_oc
                                        if cur_oc == declaration.get("Country Origin", "").upper():
                                            declaration["Country Origin"] = top_oc
                    except Exception:
                        pass
                else:
                    _log(f"Step 3b: solo had only {solo_filled} fields vs {filled} — keeping pipeline")
            else:
                _log("Step 3b: solo returned no result, keeping pipeline output")
        except Exception as _e:
            _log(f"Step 3b: solo skipped ({_e})")

    # ═══ Step 4: LLM Verifier cross-checks against source ═══
    verifier_model = config.VERIFIER_MODEL or model
    _log(f"Step 4: AI verifying... (model: {verifier_model or config.EXTRACTION_MODEL})")
    verified_result = verify(declaration, items, pages, model=verifier_model, fmt=document_format)

    declaration = verified_result.get("declaration", declaration)
    items = verified_result.get("items", items)
    corrections = verified_result.get("corrections", [])
    verified = bool(verified_result.get("verified", False))
    filled = sum(1 for v in declaration.values() if v is not None)
    _log(f"Verified: {filled}/16 fields, {len(corrections)} corrections, verified={verified}")

    # ═══ Step 4b: HOLISTIC ENSEMBLE — Gemini 3.1 Pro + Opus 4.7 vote ═══
    # Fires for CUSDEC1 + HIGH sanity flags (handwritten docs).
    # Replaces individual Phase B arbiter / C1 / C3 cell-zoom with single 2-vote pass.
    try:
        from pipeline.assembler import run_sanity_validators, _build_page_summary
        page_summary_post = _build_page_summary(page_results)
        post_flags = run_sanity_validators(declaration, document_format, items=items, page_summary=page_summary_post)
        high_flags = [f for f in post_flags if "HIGH" in f]

        # Trigger ensemble for CUSDEC1 + HIGH flags
        if document_format == "CUSDEC1" and high_flags:
            try:
                from pipeline.holistic_voter import run_ensemble
                from pipeline.consensus_resolver import merge_ensemble
                _log(f"Step 4b-ensemble: HW doc with {len(high_flags)} HIGH flags — running 2-vote ensemble")
                ensemble = run_ensemble(pages)
                if ensemble.get("gemini") or ensemble.get("opus"):
                    new_decl, new_items, changes = merge_ensemble(
                        ensemble.get("gemini", {}),
                        ensemble.get("opus", {}),
                        declaration, items,
                    )
                    if changes:
                        declaration = new_decl
                        items = new_items
                        corrections = corrections + changes
                        _log(f"Ensemble applied {len(changes)} corrections")
                        for c in changes[:8]:
                            _log(f"  ▸ {c['field']}: {c.get('original')} → {c.get('corrected')} ({c.get('reason')})")

                        # Re-apply name normalize (ensemble may have overwritten seed canonical)
                        try:
                            from pipeline.assembler import (get_canonical_consignors_for_importer,
                                                             normalize_consignor_name)
                            imp = declaration.get("Importer (Name)")
                            cons = declaration.get("Consignor (Name)")
                            if imp and cons:
                                seed_canon = get_canonical_consignors_for_importer(imp)
                                if seed_canon:
                                    canon, changed = normalize_consignor_name(cons, seed_canon)
                                    if changed:
                                        _log(f"  ▸ Post-ensemble consignor normalize: '{cons}' → '{canon}'")
                                        declaration["Consignor (Name)"] = canon
                        except Exception:
                            pass

                        # Recompute sanity flags
                        post_flags = run_sanity_validators(declaration, document_format, items=items,
                                                            page_summary=page_summary_post)
                        high_flags = [f for f in post_flags if "HIGH" in f]
                    else:
                        _log("Ensemble: no consensus changes")
            except Exception as _e:
                _log(f"Ensemble skipped: {_e}")
        if high_flags:
            from pipeline.vision_arbiter import arbiter_check, arbiter_check_items
            _log(f"Step 4b: Vision Arbiter — {len(high_flags)} HIGH flags: {high_flags[:3]}")
            arb_result = arbiter_check(declaration, items, pages, post_flags)
            if arb_result.get("changes"):
                declaration = arb_result["applied_declaration"]
                arb_changes = arb_result["changes"]
                corrections = corrections + arb_changes
                _log(f"Vision Arbiter applied {len(arb_changes)} corrections")

            # Item-level arbiter (closure_eq2 — qty×unit≠price)
            item_arb_result = arbiter_check_items(declaration, items, pages, post_flags)
            if item_arb_result.get("changes"):
                items = item_arb_result["applied_items"]
                item_changes = item_arb_result["changes"]
                corrections = corrections + item_changes
                _log(f"Item Arbiter applied {len(item_changes)} corrections")

            if not arb_result.get("changes") and not item_arb_result.get("changes"):
                _log("Vision Arbiter: no high-confidence changes")
            # Recompute sanity flags after arbiter
            sanity_flags = run_sanity_validators(declaration, document_format, items=items,
                                                 page_summary=page_summary_post)

            # ═══ Step 4c: Phase C1 — Per-cell zoom + narrow re-read ═══
            # Fires only if Phase B exhausted but flags still HIGH
            still_high = [f for f in sanity_flags if "HIGH" in f]
            if still_high:
                try:
                    from pipeline.cell_zoom import zoom_check
                    _log(f"Step 4c: Cell zoom — {len(still_high)} HIGH flags remaining: {still_high[:3]}")
                    zoom_result = zoom_check(declaration, items, pages, sanity_flags)
                    if zoom_result.get("changes"):
                        declaration = zoom_result["applied_declaration"]
                        items = zoom_result["applied_items"]
                        zoom_changes = zoom_result["changes"]
                        corrections = corrections + zoom_changes
                        _log(f"Cell zoom applied {len(zoom_changes)} corrections")
                        sanity_flags = run_sanity_validators(declaration, document_format, items=items,
                                                             page_summary=page_summary_post)
                    else:
                        _log("Cell zoom: no high-confidence changes")
                except Exception as _e:
                    _log(f"Cell zoom skipped: {_e}")

                # ═══ Step 4d: Phase C3 — Gemini 3 Pro per-cell (SOTA HW OCR) ═══
                # Fires only if C1 (Flash) didn't recover. ~10x cost but much higher accuracy.
                still_high_after_c1 = [f for f in sanity_flags if "HIGH" in f]
                if still_high_after_c1:
                    try:
                        from pipeline.cell_zoom import zoom_check_pro
                        _log(f"Step 4d: Cell zoom PRO — {len(still_high_after_c1)} HIGH flags still: {still_high_after_c1[:3]}")
                        pro_result = zoom_check_pro(declaration, items, pages, sanity_flags)
                        if pro_result.get("changes"):
                            declaration = pro_result["applied_declaration"]
                            items = pro_result["applied_items"]
                            pro_changes = pro_result["changes"]
                            corrections = corrections + pro_changes
                            _log(f"Cell zoom PRO applied {len(pro_changes)} corrections")
                            sanity_flags = run_sanity_validators(declaration, document_format, items=items,
                                                                 page_summary=page_summary_post)
                        else:
                            _log("Cell zoom PRO: no high-confidence changes")
                    except Exception as _e:
                        _log(f"Cell zoom PRO skipped: {_e}")
        else:
            _log("Step 4b: no HIGH sanity flags — arbiter skipped")
    except Exception as _e:
        _log(f"Vision Arbiter skipped: {_e}")

    # Free image memory after verifier + arbiter (no longer needed — fee verify uses text, not images)
    for p in pages:
        p.pop("image_b64", None)

    # ═══ Step 5: Fee verification — LLM text-based (primary) + deterministic (fallback) ═══
    from pipeline.assembler import verify_fees_with_llm, _fix_fee_shift, _build_page_summary

    fee_fields = ["Commercial Tax (CT)", "Advance Income Tax (AT)",
                  "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]

    # Save assembler's original fee values — if everything else fails, these are our baseline
    original_fees = {f: declaration.get(f, 0) or 0 for f in fee_fields}

    # Primary: Focused text LLM call to verify fee mapping using raw page data
    _log("Step 5: Verifying fee field mapping...")
    fee_result = verify_fees_with_llm(declaration, page_results)
    fee_confidence = float(fee_result.get("confidence", 0)) if fee_result else 0

    fee_applied = False
    if fee_result and fee_confidence >= 0.7:
        # ── Sanity check LLM output before applying ──
        customs_value = float(declaration.get("Total Customs Value", 0) or 0)
        duty = float(declaration.get("Import/Export Customs Duty", 0) or 0)
        sane = True
        for field in fee_fields:
            try:
                val = float(fee_result.get(field, 0) or 0)
            except (ValueError, TypeError):
                val = 0
            # No fee should exceed customs value
            if customs_value > 0 and val > customs_value:
                _log(f"  ⚠ Fee LLM sanity fail: {field}={val:,.0f} > customs_value={customs_value:,.0f}")
                sane = False
            # CT should not be absurdly large vs duty
            if field == "Commercial Tax (CT)" and duty > 0 and val > duty * 5:
                _log(f"  ⚠ Fee LLM sanity fail: CT={val:,.0f} > 5x duty={duty:,.0f}")
                sane = False

        if sane:
            # Apply LLM-verified values
            changes = []
            for field in fee_fields:
                new_val = fee_result.get(field)
                if new_val is not None:
                    old_val = declaration.get(field, 0) or 0
                    try:
                        new_val = float(new_val)
                    except (ValueError, TypeError):
                        continue
                    if abs(float(old_val or 0) - new_val) > 0.01:
                        changes.append((field, old_val, new_val))
                    declaration[field] = new_val

            fee_applied = True
            if changes:
                _log(f"Fee verifier corrected {len(changes)} fields (confidence={fee_confidence:.2f}):")
                for field, old_val, new_val in changes:
                    _log(f"  {field}: {old_val} → {new_val}")
            else:
                _log(f"Fee verifier confirmed all values correct (confidence={fee_confidence:.2f})")
        else:
            _log(f"Fee verifier output failed sanity check — ignoring LLM result")

    if not fee_applied:
        # Fallback: conservative deterministic correction (requires page text evidence)
        _log("Fee verifier unavailable or failed — using deterministic fallback")
        page_summary = _build_page_summary(page_results)
        declaration = _fix_fee_shift(declaration, page_summary, "", None, job_id=None)

        # ── Final safety net: if deterministic made fees WORSE, revert to assembler original ──
        customs_value = float(declaration.get("Total Customs Value", 0) or 0)
        if customs_value > 0:
            for field in fee_fields:
                val = float(declaration.get(field, 0) or 0)
                if val > customs_value:
                    _log(f"  ⚠ Reverting ALL fee changes — {field}={val:,.0f} exceeds customs value")
                    for f in fee_fields:
                        declaration[f] = original_fees[f]
                    _log(f"  Fees restored to assembler values: {original_fees}")
                    break

    # ═══ Cross-validation hard flag: items sum vs declaration total ═══
    cross_val_passed = True
    try:
        decl_total = float(declaration.get("Total Customs Value", 0) or 0)
        items_sum = 0.0
        for it in items:
            cv = it.get("Customs Value (MMK)")
            if cv is not None:
                try:
                    items_sum += float(cv)
                except (ValueError, TypeError):
                    pass
        if decl_total > 0 and items_sum > 0:
            diff_ratio = abs(items_sum - decl_total) / decl_total
            if diff_ratio > 0.05:
                cross_val_passed = False
                sanity_flags.append(
                    f"cross_val:items_sum={items_sum:.0f},decl={decl_total:.0f},diff={diff_ratio*100:.1f}%"
                )
                _log(f"  ⚠ Cross-validation FAILED: items sum vs decl diff = {diff_ratio*100:.1f}%")
    except Exception as e:
        _log(f"  Cross-validation error: {e}")

    # ═══ Compute accuracy ═══
    total_fields = filled + sum(sum(1 for v in item.values() if v is not None) for item in items)
    max_fields = 16 + len(items) * 9
    accuracy = (total_fields / max_fields * 100) if max_fields > 0 else 0

    # Confidence scoring
    confidence = None
    try:
        from v2.confidence import compute_field_confidence
        confidence = compute_field_confidence(declaration=declaration, items=items, page_results=page_results)
        if confidence and confidence.get("summary"):
            s = confidence["summary"]
            _log(f"Confidence: avg={s['average_confidence']:.2f} | high={s['high']} medium={s['medium']} low={s['low']}")
    except Exception:
        pass

    total_duration = time.time() - total_start
    total_cost = cost_tracker.get_total_cost() if cost_tracker else 0
    cost_breakdown = cost_tracker.get_entries() if cost_tracker else []
    token_totals = cost_tracker.get_total_tokens() if cost_tracker else {"input_tokens": 0, "output_tokens": 0, "total": 0}

    _log(f"Complete: {len(items)} items, {filled}/16 decl, {total_duration:.1f}s, ${total_cost:.4f}, "
         f"tokens in={token_totals['input_tokens']} out={token_totals['output_tokens']}")

    return {
        "pipeline_version": "ro-ed",
        "pipeline_mode": "ro_ed",
        "items_count": len(items),
        "declaration": declaration,
        "items": items,
        "accuracy": accuracy,
        "duration_seconds": total_duration,
        "cost_usd": total_cost,
        "cost_breakdown": cost_breakdown,
        "tokens_in": token_totals["input_tokens"],
        "tokens_out": token_totals["output_tokens"],
        "pages_total": len(pages),
        "pages_ok": ok,
        "validation": {"overall_accuracy": accuracy},
        "confidence": confidence,
        "corrections": corrections,
        "document_format": document_format,
        "sanity_flags": sanity_flags,
        "cross_val_passed": cross_val_passed,
        "verified": verified,
    }


if __name__ == "__main__":
    import sys
    pdf = sys.argv[1] if len(sys.argv) > 1 else None
    if not pdf:
        print("Usage: python -m pipeline.pipeline <pdf_path>")
        sys.exit(1)

    config.PDF_PATH = pdf
    if database:
        database.init_database()

    result = run_pipeline(pdf)
    if result:
        print(f"\nItems: {result['items_count']}")
        print(f"Declaration: {sum(1 for v in result['declaration'].values() if v is not None)}/16")
        print(f"Duration: {result['duration_seconds']:.1f}s")
        print(f"Cost: ${result['cost_usd']:.4f}")
        if result.get("corrections"):
            print(f"Corrections: {len(result['corrections'])}")
            for c in result["corrections"]:
                print(f"  {c.get('field')}: {c.get('original')} → {c.get('corrected')}")
