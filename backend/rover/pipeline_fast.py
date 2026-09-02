"""Fleet v2 (fast/cheap) — L1 page routing + L2 single vision call.
Same tiers as pipeline.py, minus the 4× image waste.

  Tier 0 deterministic -> route pages -> ONE vision call (all columns)
  -> deterministic supervisor -> challenger on suspect only.

run(pdf_path, use_challenger=True) -> dict (same shape as pipeline.run).
"""
import os
import time
from typing import Dict

from . import context, deterministic, geometry, supervisor, router, single_agent, llm
from . import products, item_text, recovery, handwriting
from . import pipeline as v1pipeline
from .pipeline import _run_challenger, _agree, _cost
from .schema import Cell, COLUMNS

# Native-PDF primary read: send the raw PDF to a PDF-capable model (Gemini/Claude)
# instead of downscaled page-JPEGs. Proven on the 16-doc set: 100% verified accuracy,
# ~4x cheaper, fixes the derived-exchange-rate bug. Requires ROVER_PRIMARY_MODEL to be
# a PDF-capable model. grok is image-only, so the challenger always runs on images.
PDF_NATIVE = os.environ.get("ROVER_PDF_NATIVE") == "1"


def _bboxes_from_record(rec: Dict[str, Cell]) -> dict:
    """Cells that know their own coordinates -> the v11 field_bbox contract:
    {"declaration": {field: {page, x, y, w, h, row}}, "items": {}}.

    Only geometry-bound cells carry a rect today, so this is the 5 text-layer
    CUSDECs in the corpus; the other 11 are scanned and return nothing here. That
    asymmetry is deliberate — an unmeasured field gets NO entry, so the UI can say
    "location unknown" instead of drawing a box someone would trust.
    `row` is the full printed row (label + value), which is what a readable crop
    needs; `x/y/w/h` is the value token alone, for the highlight.
    """
    out = {"declaration": {}, "items": {}}
    for col, cell in (rec or {}).items():
        box = getattr(cell, "bbox", None)
        page = getattr(cell, "page", None)
        if not box or not page or len(box) != 4:
            continue
        entry = {"page": int(page), "x": box[0], "y": box[1],
                 "w": box[2] - box[0], "h": box[3] - box[1]}
        rb = getattr(cell, "row_bbox", None)
        if rb and len(rb) == 4:
            entry["row"] = {"x": rb[0], "y": rb[1],
                            "w": rb[2] - rb[0], "h": rb[3] - rb[1]}
        out["declaration"][col] = entry
    return out


def run(pdf_path: str, use_challenger: bool = True, use_rescue: bool = True,
        on_log=None) -> dict:
    def log(msg, level="info"):
        if on_log:
            try:
                on_log({"msg": msg, "level": level})
            except Exception:
                pass

    t0 = time.time()
    ctx = context.load(pdf_path)
    log(f"Loaded {len(ctx.pages)} pages ({sum(p.is_image_page for p in ctx.pages)} scanned)")
    cost = 0.0
    # Token accounting — accumulated alongside cost from every LLM usage block
    # (OpenRouter returns OpenAI-style prompt_tokens/completion_tokens).
    tok = {"in": 0, "out": 0}

    def _acc(u):
        u = u or {}
        tok["in"] += int(u.get("prompt_tokens") or u.get("tokens_prompt") or 0)
        tok["out"] += int(u.get("completion_tokens") or u.get("tokens_completion") or 0)
        return _cost(u)

    # Tier 0
    det = deterministic.extract(ctx.all_lines)
    log(f"Deterministic read · decl_no={det.get('declaration_no').value if det.get('declaration_no') else '—'}")

    # Tier 0.5 — geometry-anchored money block. On a CUSDEC page with a text layer
    # this binds label->value by row coordinates, which the flat line scan cannot do
    # (scrambled columns). $0, exact, and it beats vision at merge time via its
    # confidence. Empty on scanned CUSDECs, so vision keeps those unchanged.
    geo = geometry.read(pdf_path)
    if geo:
        det.update(geo)
        log(f"Geometry read · {len(geo)} field(s) bound from the text layer at $0", 'ok')

    # Bundle detection: >1 customs declaration id in one file means the money block
    # we extract may belong to a different declaration than the one we file it under.
    decl_ids = geometry.declaration_ids(pdf_path)
    if len(decl_ids) > 1:
        log(f"Bundle · file carries {len(decl_ids)} declarations {decl_ids} — identity flagged", 'warn')

    # L1 — pick what the primary reader sees. Native-PDF = whole doc, one file block
    # (text layer, no OCR loss); else route to the field-bearing pages as JPEGs.
    if PDF_NATIVE:
        pages = ctx.pages
        primary_content = llm.pdf_content(pdf_path)
        log(f"Native-PDF · sending whole document ({len(ctx.pages)} pages) to {llm.PRIMARY}")
    else:
        pages = router.select(ctx, max_pages=2)
        primary_content = router.image_content(pages)
        log(f"Routed to pages {[p.number for p in pages]} of {len(ctx.pages)}")

    # Image set for the challenger + recovery (grok is image-only — never feed it the
    # PDF block). In native mode this is a small routed-page render, built once.
    imgs = router.image_content(router.select(ctx, max_pages=2)) if PDF_NATIVE else primary_content

    # L2 — single vision call, all columns
    log(f"Reading fields with {llm.PRIMARY} …")
    vis_res = single_agent.run(primary_content)
    cost += _acc(vis_res.pop("_usage", {}))
    log(f"Fields read · ${round(cost,4)} so far", 'ok')
    vis_res.pop("_err", None)
    routed_items = vis_res.pop("_items", [])
    vis = {k: v for k, v in vis_res.items() if isinstance(v, Cell)}

    # PRODUCT LANE — the header router only sends 1–2 pages, so items on later pages
    # of a multi-page bundle are missed. Find ALL item pages, extract every product,
    # de-dup bundle repeats, fill quantities from the text layer, and cross-check the
    # count against the doc's declared 'Total items'. Fall back to the routed-page
    # items if the lane finds nothing.
    prod = products.run(ctx)
    cost += _acc(prod.get("usage", {}))
    items = prod.get("items") or routed_items
    # Fill quantities from the text layer — but DISCARD a fill that stamps the same
    # number on every item (parser confusion on a scrambled scan): an honest null
    # beats a wrong quantity.
    filled = item_text.fill_quantities(items, ctx)
    _qs = [i.get("quantity") for i in filled if i.get("quantity") is not None]
    if not (len(_qs) > 1 and len(set(_qs)) == 1):
        items = filled
    # Cross-check count only when the doc's declared 'Total items' is plausible
    # (a mis-parse can read a stray large number). Implausible -> don't false-flag.
    declared_count = prod.get("declared_count")
    items_incomplete = bool(declared_count and 1 < declared_count <= 50
                            and len(items) < declared_count)
    log(f"Products · found {len(items)} line item(s)")

    # Supervisor
    rec, suspect, notes, needs_review = supervisor.compile(det, vis, decl_ids)
    log(f"Math-verify · {len(suspect)} field(s) flagged", 'warn' if suspect else 'ok')

    # D — handwriting boost. A mostly-scanned doc with a WEAK base read (missing
    # items, or an empty value block) gets one hi-res image re-read under a
    # handwriting-focused prompt. FILLS empty fields + ADDS unseen items only —
    # never overwrites a value the base read already produced. Flag-gated + fail-safe.
    if handwriting.HW_BOOST and handwriting.is_handwritten(ctx) and \
            (items_incomplete or len(items) == 0 or supervisor.value_block_empty(rec)):
        log("Handwriting boost · hi-res re-read of scanned pages …")
        hb = handwriting.boost(pdf_path)
        cost += _acc(hb.get("usage", {}))
        for k, cell in hb.get("cells", {}).items():
            if cell.value not in (None, "") and vis.get(k, Cell(column=k)).value in (None, ""):
                vis[k] = cell
        if hb.get("items") and len(hb["items"]) > len(items):
            items = hb["items"]
            items_incomplete = bool(declared_count and 1 < declared_count <= 50
                                    and len(items) < declared_count)
        rec, suspect, notes, needs_review = supervisor.compile(det, vis, decl_ids)
        log(f"Handwriting boost · {len(items)} item(s), {len(suspect)} flagged", 'ok')

    # Fix B — adaptive escalation. If the whole value/tax block came back empty, the
    # router missed the CUSDEC page on this (scanned) doc. Re-read on more pages once
    # and fill the gaps. Bounded: single extra call, only when genuinely empty.
    escalated = False
    if supervisor.value_block_empty(rec):
        escalated = True
        log("Value block empty → escalating to more pages")
        more = router.select(ctx, max_pages=6)
        if len(more) <= len(pages):                # router gave no new pages → send front 6
            more = ctx.pages[:6]
        vis2 = single_agent.run(router.image_content(more))
        cost += _acc(vis2.pop("_usage", {}))
        vis2.pop("_err", None); vis2.pop("_items", None)
        for k, cell in vis2.items():
            if isinstance(cell, Cell) and cell.value not in (None, "") \
                    and vis.get(k, Cell(column=k)).value in (None, ""):
                vis[k] = cell
        rec, suspect, notes, needs_review = supervisor.compile(det, vis, decl_ids)
        notes.append(f"escalated to pages {[p.number for p in more]}")

    # RECOVERY AGENT — bounded cell-zoom re-read of suspect fields. Cheap, targeted,
    # and the deterministic supervisor stays the judge (a field only clears review if
    # the math accepts the zoomed value). Runs before the challenger/rescue.
    recovery_report = {}
    if use_challenger and suspect:
        log(f"Recovery · zoom re-read {suspect}")
        rc = recovery.recover(ctx, rec, suspect)
        for u in rc.get("report", {}).get("cost_usage", []):
            cost += _acc(u)
        rec, suspect, needs_review = rc["rec"], rc["suspect"], rc["needs_review"]
        recovery_report = rc.get("report", {})
        if recovery_report.get("recovered"):
            notes.append(f"recovered by zoom: {recovery_report['recovered']}")

    # Tier 2 — challenger on suspect columns only (still full doc for a second look).
    # B (cost) — SKIP declaration_no here: the challenger is grok (image-only), a poor
    # digit reader (it flipped decl_no digits on the scanned set), so a grok call for it
    # is pure cost with no accuracy gain. decl_no still gets the cheap recovery-zoom above
    # and, if uncorroborated, stays flagged for a human — the fail-closed gate is intact.
    challenger_report = {}
    chal_cols = [c for c in suspect if c != "declaration_no"]
    if use_challenger and chal_cols:
        log(f"Second model checking {chal_cols} …")
        ch = _run_challenger(ctx, chal_cols, rec, images=imgs)
        cost += _acc(ch.pop("_usage", {}))
        for col in chal_cols:
            prim, chc = rec.get(col), ch.get(col)
            if not chc or chc.value in (None, ""):
                challenger_report[col] = "challenger-empty"; continue
            if _agree(prim.value, chc.value):
                rec[col].status = "ok"; rec[col].note += " | challenger-agrees"
                challenger_report[col] = f"agree ({chc.value})"
            else:
                rec[col].status = "review"
                rec[col].note += f" | challenger-DISAGREES ({chc.value})"
                rec[col].alternates.append(chc.value)
                challenger_report[col] = f"DISAGREE prim={prim.value} chal={chc.value}"
        needs_review = any(c.status in ("suspect", "review")
                           for c in rec.values() if isinstance(c, Cell))

    result = {
        "pdf": pdf_path.split("/")[-1],
        "variant": "fast (routed + single-call)",
        "pages_sent": [p.number for p in pages],
        "pages_total": len(ctx.pages),
        "escalated": escalated,
        "rescued_by": None,
        "sec": round(time.time() - t0, 1),
        "cost": round(cost, 4),
        "needs_review": needs_review,
        "suspect": suspect,
        "notes": notes,
        "challenger": challenger_report,
        "recovery": recovery_report,
        "items": items,
        "n_items": len(items),
        "declared_count": declared_count,
        "items_incomplete": items_incomplete,
        "item_pages": prod.get("item_pages"),
        "record": {c: rec[c].as_dict() for c in COLUMNS if c in rec},
        "values": {c: (rec[c].value if c in rec else None) for c in COLUMNS},
    }

    # FULL-DOC RESCUE — if the cheap routed pass still flags review, the router likely
    # missed the field page on a many-page scan. Re-read the WHOLE document in ONE
    # vision call (not the old 4-family v1, which cost ~4×). Fill/replace the suspect
    # fields with these direct reads and let the deterministic supervisor re-judge; a
    # clean result auto-passes (direct reads, not derivations). Pages capped for the
    # 30MB request limit.
    # B (cost) — the full-doc rescue is the big spend. It exists to recover MISSING
    # value/tax fields on a scan the router under-read. It CANNOT resolve an
    # uncorroborated declaration_no (that's an identity-confirm, not a re-read), so if
    # decl_no is the ONLY thing flagged we skip the rescue entirely and leave the doc
    # for a human glance — the common case on clean docs, and the whole cost saving.
    rescue_worth = needs_review and (
        any(s != "declaration_no" for s in suspect) or supervisor.value_block_empty(rec))
    if use_rescue and rescue_worth:
        # C — native-PDF rescue when available: re-read the whole doc as the raw PDF
        # (text layer, cheaper + more accurate) instead of 12 page-JPEGs. Image-only
        # models fall back to the capped page render.
        if PDF_NATIVE:
            log("Full-document rescue (native PDF, single pass) …")
            rescue_content = llm.pdf_content(pdf_path)
        else:
            log("Full-document rescue (single pass) …")
            rescue_content = router.image_content(ctx.pages[:12])
        rres = single_agent.run(rescue_content)
        cost += _acc(rres.pop("_usage", {}))
        rres.pop("_err", None)
        ritems = rres.pop("_items", []) or []
        for k, c in rres.items():
            if isinstance(c, Cell) and c.value not in (None, ""):
                vis[k] = c
        rec, suspect, notes2, needs_review = supervisor.compile(det, vis, decl_ids)
        notes.extend(notes2)
        result["cost"] = round(cost, 4)
        result["sec"] = round(time.time() - t0, 1)
        if not needs_review:
            result.update({
                "rescued_by": "full-pass",
                "needs_review": False,
                "suspect": [],
                "record": {c: rec[c].as_dict() for c in COLUMNS if c in rec},
                "values": {c: (rec[c].value if c in rec else None) for c in COLUMNS},
            })
            if len(ritems) > result["n_items"]:
                result["items"] = ritems; result["n_items"] = len(ritems)
            result["notes"].append("rescued by single full-doc pass")
            log("Rescued (single full-doc pass)", 'ok')
        else:
            result["rescued_by"] = "rescue-attempted-still-review"
            result["suspect"] = suspect
            result["notes"].append("rescue attempted, still needs review")

    # Product completeness is independent of the header review/rescue: if we captured
    # fewer items than the doc declares, flag for a human — never silently under-report.
    if items_incomplete:
        result["needs_review"] = True
        if "products" not in result["suspect"]:
            result["suspect"].append("products")
        result["notes"].append(
            f"products incomplete: captured {result['n_items']} of {declared_count} declared")

    needs_review = result["needs_review"]
    # Final token/page accounting (set here so the rescue pass is included).
    result["tokens_in"] = tok["in"]
    result["tokens_out"] = tok["out"]
    result["pages_scanned"] = sum(p.is_image_page for p in ctx.pages)
    # Where each value physically sits on the page, for the ones we actually
    # measured. Built from the FINAL record so a rescue that replaced a cell also
    # replaces (or drops) its box — a stale rect pointing at a number we no longer
    # report would be worse than no rect at all. Same contract as
    # v11/tools/field_bbox.py, so the existing DB column and review UI reader work
    # unchanged; fields without coordinates are simply absent.
    result["field_bboxes"] = _bboxes_from_record(rec)
    log(f"Done · {len([c for c in COLUMNS if rec.get(c) and rec[c].value not in (None,'')])} fields, "
        f"{len(items)} products, review={needs_review}, ${result['cost']}",
        'ok' if not needs_review else 'warn')
    return result
