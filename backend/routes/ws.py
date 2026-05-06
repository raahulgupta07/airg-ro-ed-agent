#!/usr/bin/env python3
"""WebSocket route for RO-ED pipeline — streaming logs"""

import time
import json
import asyncio
from pathlib import Path
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import config
import database
import auth


def _verify_ws_token(token: str):
    kc_config = config.get_keycloak_config()
    if kc_config and kc_config.get("realm_url"):
        try:
            payload = auth.verify_keycloak_token(token)
            user_info = auth.extract_user_info(payload)
            user = database.upsert_keycloak_user(
                keycloak_id=user_info["sub"], username=user_info["username"],
                display_name=user_info["display_name"], email=user_info["email"],
                role=user_info["role"],
            )
            return user["id"], user["username"]
        except Exception:
            pass
    token_data = auth.verify_token(token)
    if not token_data:
        raise ValueError("Invalid token")
    return token_data.user_id, token_data.username


router = APIRouter()


@router.websocket("/batch")
async def run_batch(ws: WebSocket):
    await ws.accept()
    ws_connected = True

    async def send(data):
        nonlocal ws_connected
        if not ws_connected: return
        try: await ws.send_json(data)
        except Exception: ws_connected = False

    all_logs = []  # Collect all detailed logs for DB save

    async def log(text, type="detail"):
        await send({"log": text, "log_type": type})
        await asyncio.sleep(0)  # Force WebSocket flush — makes logs stream in real-time
        all_logs.append({"text": text, "type": type})

    async def step(num, name, status, detail=""):
        await send({"file_index": file_idx, "step": num, "name": name, "status": status,
                     "detail": detail, "filename": filename})
        await asyncio.sleep(0)  # Force flush
        if status == "done":
            log_text = detail + "\n" + "\n".join(l["text"] for l in all_logs[-50:])
            try:
                database.log_processing_step(job_id, num, name, status, log_text[:5000], 0)
            except Exception:
                pass

    try:
        init_msg = await ws.receive_json()
        token = init_msg.get("token", "")
        files = init_msg.get("files", [])

        try:
            user_id, username = _verify_ws_token(token)
        except Exception:
            await send({"error": "Invalid token"}); await ws.close(); return

        total_files = len(files)
        completed = 0; failed = 0; total_cost = 0.0; total_items = 0; accuracies = []; job_ids = []

        for file_idx, file_info in enumerate(files):
            pdf_path = Path(file_info.get("path", ""))
            filename = file_info.get("filename", "unknown.pdf")

            if not pdf_path.exists():
                await send({"file_index": file_idx, "file_complete": True, "status": "error",
                             "error": "File not found", "filename": filename})
                failed += 1; continue

            # Per-job cost tracking (not global)
            try:
                import cost_tracker; cost_tracker.reset()
            except Exception: pass

            job_id = database.create_job(filename, str(pdf_path), pdf_path.stat().st_size,
                                          0, 0, 0, user_id=user_id, username=username)
            await send({"file_index": file_idx, "job_created": True, "job_id": job_id, "filename": filename})

            file_start = time.time()

            try:
                # ════════════════════════════════════════════
                # STEP 1: HD SPLITTER
                # ════════════════════════════════════════════
                await step(1, "HD_SPLITTER", "running")
                file_size_mb = pdf_path.stat().st_size / (1024 * 1024)
                await log(f"Hey! I received your file: {filename} ({file_size_mb:.1f} MB)")
                await log(f"Starting RO-ED AI Agent...", "header")
                await log(f"Opening PDF and checking structure...")
                await log(f"Rendering each page at 300 DPI for maximum text clarity...")

                from pipeline.splitter import split_pdf
                pages = await asyncio.to_thread(split_pdf, str(pdf_path))
                total_pages = len(pages)

                await log(f"Found {total_pages} pages in this document")
                await log("Enhancing images: sharpen + auto-contrast for better reading...")
                total_kb = 0
                for p in pages:
                    kb = len(p.get('image_b64', '')) * 3 // 4 // 1024
                    total_kb += kb
                    await log(f"  Page {p['page_number']:2d}: {p['width']}x{p['height']}px | {kb}KB | HD ready")
                await log(f"Total image data: {total_kb/1024:.1f}MB for {total_pages} pages")

                try:
                    conn = database._connect()
                    conn.execute("UPDATE jobs SET pipeline_version='ro-ed', total_pages=? WHERE job_id=?", (total_pages, job_id))
                    conn.commit()
                except Exception:
                    pass
                finally:
                    try: conn.close()
                    except Exception: pass

                await step(1, "HD_SPLITTER", "done", f"{total_pages} HD pages at 300 DPI")

                # ════════════════════════════════════════════
                # STEP 2: VISION EXTRACTION
                # ════════════════════════════════════════════
                await step(2, "VISION_EXTRACT", "running")
                workers = min(8, total_pages)
                await log(f"STEP 2: Reading Each Page", "header")
                await log(f"Deploying {workers} parallel AI vision workers...")
                await log(f"Sending {total_pages} HD page images to AI for analysis...")
                await log("Each page will be analyzed independently — looking for fields, tables, amounts, document type...")

                from pipeline.vision import extract_page
                import queue, threading

                page_results = []
                result_queue = queue.Queue()
                t2 = time.time()

                # Run extractions in background threads, stream results as they arrive
                def _extract_and_queue(p):
                    r = extract_page(p)
                    result_queue.put(r)

                threads = []
                for p in pages:
                    t = threading.Thread(target=_extract_and_queue, args=(p,))
                    t.start()
                    threads.append(t)
                    # Limit concurrent threads
                    if len([t for t in threads if t.is_alive()]) >= 8:
                        await asyncio.sleep(0.1)

                # Stream results as they complete
                received = 0
                while received < total_pages:
                    try:
                        r = result_queue.get(timeout=0.5)
                        page_results.append(r)
                        received += 1
                        pn = r["page_number"]; pt = r.get("page_type", "?"); conf = r.get("confidence", 0)
                        nf = len(r.get("parsed", {}).get("fields", {}))
                        nt = len(r.get("parsed", {}).get("tables", []))
                        na = len(r.get("parsed", {}).get("amounts", []))
                        expl = r.get("explanation", "")[:70]
                        await log(f"  Page {pn:2d} -> {pt} (confidence: {conf})")
                        await log(f"     {nf} fields, {nt} tables, {na} amounts", "data")
                        if expl:
                            await log(f"     \"{expl}\"", "data")
                        await log(f"     [{received}/{total_pages} pages done]", "data")
                    except queue.Empty:
                        await asyncio.sleep(0.3)  # Yield to event loop, let WS send pending msgs

                for t in threads:
                    t.join(timeout=1)

                page_results.sort(key=lambda x: x["page_number"])
                ok = sum(1 for x in page_results if x["status"] == "ok")
                dur2 = time.time() - t2
                tf = sum(len(x.get("parsed", {}).get("fields", {})) for x in page_results)
                tt = sum(len(x.get("parsed", {}).get("tables", [])) for x in page_results)

                await log(f"Vision complete: {ok}/{total_pages} pages analyzed in {dur2:.1f}s", "success")
                await log(f"   Total extracted: {tf} fields, {tt} tables across all pages", "success")
                await step(2, "VISION_EXTRACT", "done", f"{ok}/{total_pages} pages, {tf} fields")

                if ok == 0: raise Exception("All pages failed")

                # ════════════════════════════════════════════
                # STEP 3: AI ASSEMBLER
                # ════════════════════════════════════════════
                await step(3, "AI_ASSEMBLER", "running")
                await log("STEP 3: AI Assembling Declaration + Items", "header")
                await log("Sending all page data to AI to build structured tables...")
                await log("AI understands any document format, language, currency...")
                await log("Building 16 declaration fields + product items table...")

                t3 = time.time()
                from pipeline.assembler import assemble
                assembled = await asyncio.to_thread(assemble, page_results)
                declaration = assembled.get("declaration", {})
                items = assembled.get("items", [])
                document_format = assembled.get("document_format", "MACCS")
                sanity_flags = list(assembled.get("sanity_flags", []) or [])
                dur3 = time.time() - t3
                await log(f"Document format: {document_format}", "data")

                filled = sum(1 for v in declaration.values() if v is not None)
                await log(f"Declaration assembled: {filled}/16 fields in {dur3:.1f}s", "success")

                if items:
                    await log(f"Found {len(items)} product items:", "success")
                    for i, item in enumerate(items):
                        n = str(item.get('Item name', '?'))[:45]
                        await log(f"  Item #{i+1}: {n}", "data")
                        await log(f"     Qty: {item.get('Quantity (1)','?')} | Price: {item.get('Invoice unit price','?')}", "data")
                        await log(f"     HS: {item.get('HS Code','?')} | Origin: {item.get('Origin Country','?')}", "data")
                else:
                    await log(f"No items found in {dur3:.1f}s", "warning")

                for k in ["Declaration No", "Declaration Date", "Importer (Name)", "Consignor (Name)",
                           "Invoice Number", "Invoice Number (Customs Declaration)",
                           "Invoice Number (Commercial Invoice)",
                           "Invoice Price", "Currency", "Exchange Rate",
                           "Total Customs Value", "Import/Export Customs Duty",
                           "Commercial Tax (CT)", "Advance Income Tax (AT)",
                           "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]:
                    v = declaration.get(k)
                    if v is not None:
                        display = f"{v:,.2f}" if isinstance(v, float) and v > 100 else str(v)
                        await log(f"  {k}: {display}", "data")
                    else:
                        await log(f"  {k}: not found on document", "warning")

                await step(3, "AI_ASSEMBLER", "done", f"{filled}/16 fields, {len(items)} items ({dur3:.1f}s)")

                # ════════════════════════════════════════════
                # STEP 4: AI VERIFIER
                # ════════════════════════════════════════════
                await step(4, "AI_VERIFIER", "running")
                await log("STEP 4: AI Verifying Results Against Source", "header")
                await log("Sending extracted results + original page images back to AI...")
                await log("AI will cross-check every value against what's actually printed...")

                t4 = time.time()
                from pipeline.verifier import verify
                verified_result = await asyncio.to_thread(verify, declaration, items, pages, None, document_format)
                declaration = verified_result.get("declaration", declaration)
                items = verified_result.get("items", items)
                corrections = verified_result.get("corrections", [])
                verified_flag = bool(verified_result.get("verified", False))
                dur4 = time.time() - t4

                if corrections:
                    await log(f"Verifier found {len(corrections)} corrections in {dur4:.1f}s:", "warning")
                    for c in corrections:
                        await log(f"  {c.get('field')}: {c.get('original')} -> {c.get('corrected')}", "warning")
                        await log(f"     Reason: {c.get('reason')}", "data")
                else:
                    await log(f"All values verified correct in {dur4:.1f}s", "success")

                filled = sum(1 for v in declaration.values() if v is not None)
                await step(4, "AI_VERIFIER", "done", f"{filled}/16 fields verified ({dur4:.1f}s)")

                # Free image memory after verifier (fee verify uses text, not images)
                for p in pages:
                    p.pop("image_b64", None)

                # Fee verification — text LLM (primary) + deterministic (fallback)
                await log("Verifying fee field mapping against raw page data...", "detail")
                try:
                    from pipeline.assembler import verify_fees_with_llm, _fix_fee_shift, _build_page_summary
                    fee_result = await asyncio.to_thread(verify_fees_with_llm, declaration, page_results)

                    fee_fields = ["Commercial Tax (CT)", "Advance Income Tax (AT)",
                                  "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]
                    fee_confidence = float(fee_result.get("confidence", 0)) if fee_result else 0

                    if fee_result and fee_confidence >= 0.7:
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
                        if changes:
                            await log(f"Fee verifier corrected {len(changes)} fields (conf={fee_confidence:.2f}):", "warning")
                            for field, old_val, new_val in changes:
                                await log(f"  {field}: {old_val} → {new_val}", "warning")
                        else:
                            await log(f"Fee values confirmed correct (conf={fee_confidence:.2f})", "success")
                    else:
                        await log("Fee verifier low confidence — using deterministic fallback", "detail")
                        ps = _build_page_summary(page_results)
                        declaration = _fix_fee_shift(declaration, ps, "", None, job_id=job_id)
                except Exception as e:
                    await log(f"Fee verification failed ({e}) — using deterministic fallback", "detail")
                    try:
                        ps = _build_page_summary(page_results)
                        declaration = _fix_fee_shift(declaration, ps, "", None, job_id=job_id)
                    except Exception:
                        pass

                # ════════════════════════════════════════════
                # STEP 5: VALIDATE + SAVE
                # ════════════════════════════════════════════
                await step(5, "VALIDATE_SAVE", "running")
                await log("STEP 5: Verify + Save", "header")
                await log("Computing per-field confidence scores...")

                try:
                    from v2.confidence import compute_field_confidence
                    conf = compute_field_confidence(declaration=declaration, items=items, page_results=page_results)
                    if conf and conf.get("summary"):
                        s = conf["summary"]
                        await log(f"Confidence: avg={s['average_confidence']:.2f} | high={s['high']} medium={s['medium']} low={s['low']}")
                except Exception: pass

                await log("Validating extraction results...")

                # Cross-validation hard flag
                cross_val_passed = True
                try:
                    decl_total_xv = float(declaration.get("Total Customs Value", 0) or 0)
                    items_sum_xv = 0.0
                    for it in items:
                        cv = it.get("Customs Value (MMK)")
                        if cv is not None:
                            try:
                                items_sum_xv += float(cv)
                            except (ValueError, TypeError):
                                pass
                    if decl_total_xv > 0 and items_sum_xv > 0:
                        diff_ratio = abs(items_sum_xv - decl_total_xv) / decl_total_xv
                        if diff_ratio > 0.05:
                            cross_val_passed = False
                            sanity_flags.append(
                                f"cross_val:items_sum={items_sum_xv:.0f},decl={decl_total_xv:.0f},diff={diff_ratio*100:.1f}%"
                            )
                            await log(f"Cross-validation FAILED: items vs decl diff = {diff_ratio*100:.1f}%", "warning")
                except Exception:
                    pass

                # Attach metadata onto declaration for save_declarations to pick up
                declaration["_document_format"] = document_format
                declaration["_sanity_flags_json"] = json.dumps(sanity_flags) if sanity_flags else None
                declaration["_cross_val_passed"] = cross_val_passed
                declaration["_verified"] = verified_flag

                from v2.step4_validate import validate
                merged = {"declaration": declaration, "items": items, "page_map": [], "page_groups": {}, "cross_checks": []}
                validation = validate(merged)
                accuracy = validation.get("overall_accuracy", 0)

                file_duration = time.time() - file_start
                file_cost = cost_tracker.get_total_cost()

                await log("Saving results to database...")

                from v2.step5_report import save_results
                await asyncio.to_thread(save_results, job_id, merged, validation,
                                         page_results, file_duration, file_cost,
                                         user_id, username, "ro_ed", conf if conf else None)

                await log("═══════════════════════════════════════", "success")
                await log(f"  EXTRACTION COMPLETE", "success")
                await log(f"  Items: {len(items)} | Declaration: {filled}/16", "success")
                await log(f"  Accuracy: {accuracy:.1f}%", "success")
                await log(f"  Cost: ${file_cost:.4f} | Time: {file_duration:.1f}s", "success")
                await log(f"  Pages: {total_pages} | Fields: {tf} | Tables: {tt}", "success")
                if corrections:
                    await log(f"  Corrections: {len(corrections)} values fixed by verifier", "success")
                await log("═══════════════════════════════════════", "success")

                await step(5, "VALIDATE_SAVE", "done", f"{accuracy:.1f}% accuracy")

                completed += 1; total_cost += file_cost; total_items += len(items)
                accuracies.append(accuracy); job_ids.append(job_id)

                # Send job result data inline to avoid separate HTTP fetch
                try:
                    job_data = database.get_job_details(job_id)
                    # Remove non-serializable / unnecessary fields
                    if job_data:
                        job_data.pop('pdf_path', None)
                        job_data.pop('cross_validation_json', None)
                        job_data.pop('pipeline_version', None)
                except Exception:
                    job_data = None

                await send({"file_index": file_idx, "file_complete": True, "status": "done",
                             "job_id": job_id, "filename": filename,
                             "accuracy": accuracy, "items_count": len(items),
                             "duration": round(file_duration, 1), "cost": round(file_cost, 4),
                             "gate_log": [f"ro-ed: {accuracy:.1f}%, {len(items)} items"],
                             "pipeline_mode": "ro_ed",
                             "job_data": job_data})

            except Exception as e:
                failed += 1
                err = str(e)[:200]
                database.update_job_status(job_id, "FAILED", err)
                await log(f"ERROR: {err}", "error")
                await send({"file_index": file_idx, "file_complete": True, "status": "error",
                             "filename": filename, "error": err})

        avg = sum(accuracies) / len(accuracies) if accuracies else 0
        await send({"batch_complete": True, "summary": {
            "total": total_files, "completed": completed, "failed": failed, "stopped": 0,
            "avg_accuracy": round(avg, 1), "total_cost": round(total_cost, 4),
            "total_items": total_items, "job_ids": job_ids}})

    except WebSocketDisconnect:
        try:
            for j in database.get_all_jobs(limit=50):
                if j.get("status") == "PROCESSING":
                    database.update_job_status(j["job_id"], "FAILED", "Client disconnected")
        except Exception: pass
    except Exception as e:
        try: await send({"error": str(e)})
        except Exception: pass
