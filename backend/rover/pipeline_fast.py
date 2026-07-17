"""Fleet v2 (fast/cheap) — L1 page routing + L2 single vision call.
Same tiers as pipeline.py, minus the 4× image waste.

  Tier 0 deterministic -> route pages -> ONE vision call (all columns)
  -> deterministic supervisor -> challenger on suspect only.

run(pdf_path, use_challenger=True) -> dict (same shape as pipeline.run).
"""
import time
from typing import Dict

from . import context, deterministic, supervisor, router, single_agent, llm
from . import products, item_text, recovery
from . import pipeline as v1pipeline
from .pipeline import _run_challenger, _agree, _cost
from .schema import Cell, COLUMNS


def run(pdf_path: str, use_challenger: bool = True, use_rescue: bool = True) -> dict:
    t0 = time.time()
    ctx = context.load(pdf_path)
    cost = 0.0

    # Tier 0
    det = deterministic.extract(ctx.all_lines)

    # L1 — route to field-bearing pages only
    pages = router.select(ctx, max_pages=2)
    imgs = router.image_content(pages)

    # L2 — single vision call, all columns
    vis_res = single_agent.run(imgs)
    cost += _cost(vis_res.pop("_usage", {}))
    vis_res.pop("_err", None)
    routed_items = vis_res.pop("_items", [])
    vis = {k: v for k, v in vis_res.items() if isinstance(v, Cell)}

    # PRODUCT LANE — the header router only sends 1–2 pages, so items on later pages
    # of a multi-page bundle are missed. Find ALL item pages, extract every product,
    # de-dup bundle repeats, fill quantities from the text layer, and cross-check the
    # count against the doc's declared 'Total items'. Fall back to the routed-page
    # items if the lane finds nothing.
    prod = products.run(ctx)
    cost += _cost(prod.get("usage", {}))
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

    # Supervisor
    rec, suspect, notes, needs_review = supervisor.compile(det, vis)

    # Fix B — adaptive escalation. If the whole value/tax block came back empty, the
    # router missed the CUSDEC page on this (scanned) doc. Re-read on more pages once
    # and fill the gaps. Bounded: single extra call, only when genuinely empty.
    escalated = False
    if supervisor.value_block_empty(rec):
        escalated = True
        more = router.select(ctx, max_pages=6)
        if len(more) <= len(pages):                # router gave no new pages → send front 6
            more = ctx.pages[:6]
        vis2 = single_agent.run(router.image_content(more))
        cost += _cost(vis2.pop("_usage", {}))
        vis2.pop("_err", None); vis2.pop("_items", None)
        for k, cell in vis2.items():
            if isinstance(cell, Cell) and cell.value not in (None, "") \
                    and vis.get(k, Cell(column=k)).value in (None, ""):
                vis[k] = cell
        rec, suspect, notes, needs_review = supervisor.compile(det, vis)
        notes.append(f"escalated to pages {[p.number for p in more]}")

    # RECOVERY AGENT — bounded cell-zoom re-read of suspect fields. Cheap, targeted,
    # and the deterministic supervisor stays the judge (a field only clears review if
    # the math accepts the zoomed value). Runs before the challenger/rescue.
    recovery_report = {}
    if use_challenger and suspect:
        rc = recovery.recover(ctx, rec, suspect)
        for u in rc.get("report", {}).get("cost_usage", []):
            cost += _cost(u)
        rec, suspect, needs_review = rc["rec"], rc["suspect"], rc["needs_review"]
        recovery_report = rc.get("report", {})
        if recovery_report.get("recovered"):
            notes.append(f"recovered by zoom: {recovery_report['recovered']}")

    # Tier 2 — challenger on suspect columns only (still full doc for a second look)
    challenger_report = {}
    if use_challenger and suspect:
        ch = _run_challenger(ctx, suspect, rec, images=imgs)
        cost += _cost(ch.pop("_usage", {}))
        for col in suspect:
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

    # V1 RESCUE — if the cheap routed pass still flags review, the router likely
    # missed the CUSDEC page on a many-page scan. Run the thorough v1 (all pages,
    # 4 families) once. If v1 comes back CLEAN we adopt it (its reads are direct,
    # not derived, so they auto-pass safely); otherwise we keep the flag for a human.
    if use_rescue and needs_review:
        v1 = v1pipeline.run(pdf_path, use_challenger=True)
        result["cost"] = round(result["cost"] + (v1.get("cost") or 0), 4)
        result["sec"] = round(time.time() - t0, 1)
        if not v1.get("needs_review"):
            result.update({
                "rescued_by": "v1",
                "needs_review": False,
                "suspect": [],
                "record": v1.get("record", result["record"]),
                "values": v1.get("values", result["values"]),
            })
            # Keep the product lane's items — it reads ALL item pages; v1's 4-family
            # pass reads the routed pages only, so never let it shrink the item list.
            if (v1.get("n_items") or 0) > result["n_items"]:
                result["items"] = v1.get("items"); result["n_items"] = v1.get("n_items")
            result["notes"].append("rescued by v1 (full-doc pass)")
        else:
            result["rescued_by"] = "v1-attempted-still-review"
            result["notes"].append("v1 rescue attempted, still needs review")

    # Product completeness is independent of the header review/rescue: if we captured
    # fewer items than the doc declares, flag for a human — never silently under-report.
    if items_incomplete:
        result["needs_review"] = True
        if "products" not in result["suspect"]:
            result["suspect"].append("products")
        result["notes"].append(
            f"products incomplete: captured {result['n_items']} of {declared_count} declared")

    return result
