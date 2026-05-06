"""V11 — Compute bounding boxes for extracted field values via PyMuPDF text search,
with OCR fallback for image-only / scanned PDFs.

Strategy:
  1. fitz.search_for(value)  — fast text-layer search (typed PDFs).
  2. If page text layer is empty / sparse AND text search missed,
     fall back to PaddleOCR (cached per page) and fuzzy-match spans.

Returns: { "declaration": {field: bbox}, "items": {idx: {field: bbox}} }
where bbox = {page, x, y, w, h, source: "text"|"ocr"}
Coords are PDF page-space (origin top-left, in points) for both sources.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz
except Exception:
    fitz = None

from .ocr_cache import get_page_ocr, img_bbox_to_pdf, get_cache_stats

log = logging.getLogger(__name__)


# Skip these — too generic to match cleanly OR not worth annotating
_SKIP_DECL = {"document_format", "currency", "currency_2", "needs_review",
              "is_valid", "country_origin"}
_SKIP_ITEM = {"customs_duty_rate", "commercial_tax_pct", "commercial_tax_percent"}

# Page text layer threshold — if a page's extractable text is shorter than this,
# treat it as image-only and route to OCR fallback.
_TEXT_LAYER_MIN_CHARS = 50

# Cap OCR to first N image-only pages — declaration data is on early pages,
# and full-document OCR is heavy on CPU containers. Override via env var.
import os as _os
_OCR_MAX_PAGES = int(_os.environ.get("V11_OCR_MAX_PAGES", "5"))

# Fuzzy match thresholds
_FUZZY_HIGH = 0.85
_FUZZY_MED = 0.75


def _norm_value(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _norm_for_match(s: str) -> str:
    """Canonicalize for fuzzy compare: lowercase, collapse whitespace."""
    return " ".join(str(s).lower().split())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _norm_for_match(a), _norm_for_match(b)).ratio()


def _page_text_len(page) -> int:
    try:
        return len(page.get_text().strip())
    except Exception:
        return 0


def _search_text_first(doc, value: str, max_pages: int = 50) -> Optional[Dict]:
    """Try fitz text-layer search. Returns bbox dict (with source='text') or None."""
    if not value or len(value) < 2:
        return None
    short = value if len(value) <= 80 else value[:80]
    for pno in range(min(len(doc), max_pages)):
        try:
            rects = doc[pno].search_for(short, quads=False)
        except Exception:
            continue
        if rects:
            r = rects[0]
            return {"page": pno + 1, "x": float(r.x0), "y": float(r.y0),
                    "w": float(r.x1 - r.x0), "h": float(r.y1 - r.y0),
                    "source": "text"}
    return None


def _ocr_match_value(
    pdf_path: str,
    doc,
    value: str,
    only_image_pages: bool,
    max_pages: Optional[int] = None,
) -> Optional[Dict]:
    """OCR fallback: fuzzy-match value against cached OCR spans across pages.

    If `only_image_pages` is True, restricts to pages whose text layer is sparse
    (< _TEXT_LAYER_MIN_CHARS chars) — avoids re-OCRing typed pages.
    """
    if not value or len(value) < 2:
        return None
    target = value if len(value) <= 120 else value[:120]
    cap = max_pages if max_pages is not None else _OCR_MAX_PAGES

    best: Tuple[float, Optional[Dict]] = (0.0, None)

    for pno in range(min(len(doc), cap)):
        try:
            page = doc[pno]
        except Exception:
            continue
        if only_image_pages and _page_text_len(page) >= _TEXT_LAYER_MIN_CHARS:
            continue

        spans = get_page_ocr(pdf_path, doc, pno)
        if not spans:
            continue

        # Single-span match
        for sp in spans:
            sc = _ratio(target, sp["text"])
            if sc > best[0]:
                px, py, pw, ph = img_bbox_to_pdf(sp)
                best = (sc, {"page": pno + 1, "x": px, "y": py, "w": pw, "h": ph,
                             "source": "ocr"})

        # Multi-span sliding window (helps long values split across tokens)
        for win in (2, 3):
            if len(spans) < win:
                continue
            for i in range(len(spans) - win + 1):
                grp = spans[i:i + win]
                # require approximately the same row (y-close)
                ys = [g["bbox_img"][1] for g in grp]
                row_h = max(g["bbox_img"][3] for g in grp)
                if row_h <= 0 or (max(ys) - min(ys)) > 0.6 * row_h:
                    continue
                joined = " ".join(g["text"] for g in grp)
                sc = _ratio(target, joined)
                if sc > best[0]:
                    xs0 = min(g["bbox_img"][0] for g in grp)
                    ys0 = min(g["bbox_img"][1] for g in grp)
                    xs1 = max(g["bbox_img"][0] + g["bbox_img"][2] for g in grp)
                    ys1 = max(g["bbox_img"][1] + g["bbox_img"][3] for g in grp)
                    fake = {
                        "bbox_img": (xs0, ys0, xs1 - xs0, ys1 - ys0),
                        "img_w": grp[0]["img_w"], "img_h": grp[0]["img_h"],
                        "pdf_w": grp[0]["pdf_w"], "pdf_h": grp[0]["pdf_h"],
                    }
                    px, py, pw, ph = img_bbox_to_pdf(fake)
                    best = (sc, {"page": pno + 1, "x": px, "y": py, "w": pw, "h": ph,
                                 "source": "ocr"})

    score, hit = best
    if hit is None:
        return None
    if score >= 0.9999:
        level = "exact"
    elif score >= _FUZZY_HIGH:
        level = "high"
    elif score >= _FUZZY_MED:
        level = "med"
    else:
        log.debug("[v11.field_bbox] OCR low-score skip val=%r score=%.2f", target[:40], score)
        return None
    log.info("[v11.field_bbox] OCR match level=%s score=%.2f val=%r",
             level, score, target[:60])
    return hit


def compute_field_bboxes(pdf_path: str, declaration: Dict, items: List[Dict]) -> Dict:
    """For each field value, find its first match on PDF.

    Layered: text-layer search first (typed PDFs), OCR fuzzy-match fallback for
    image-only / scanned pages.

    Returns: {"declaration": {field: bbox}, "items": {idx: {field: bbox}}}
    where bbox = {page, x, y, w, h, source: "text"|"ocr"}
    """
    out: Dict[str, Dict] = {"declaration": {}, "items": {}}
    if fitz is None or not pdf_path:
        return out
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log.warning("[v11.field_bbox] fitz.open failed: %s", e)
        return out

    try:
        # Detect whether the doc has any image-only pages worth OCRing
        has_image_pages = False
        try:
            for pno in range(min(len(doc), 50)):
                if _page_text_len(doc[pno]) < _TEXT_LAYER_MIN_CHARS:
                    has_image_pages = True
                    break
        except Exception:
            pass

        def _resolve(value: Any) -> Optional[Dict]:
            sval = _norm_value(value)
            if not sval or sval in ("0", "0.0"):
                return None
            bbox = _search_text_first(doc, sval)
            if bbox:
                return bbox
            if has_image_pages:
                return _ocr_match_value(pdf_path, doc, sval, only_image_pages=True)
            return None

        # Declaration fields
        for field, val in (declaration or {}).items():
            if field in _SKIP_DECL:
                continue
            bbox = _resolve(val)
            if bbox:
                out["declaration"][field] = bbox

        # Item fields
        for idx, item in enumerate(items or []):
            row: Dict = {}
            for field, val in (item or {}).items():
                if field in _SKIP_ITEM or field.startswith("_"):
                    continue
                bbox = _resolve(val)
                if bbox:
                    row[field] = bbox
            if row:
                out["items"][str(idx)] = row

        # Log cache effectiveness
        stats = get_cache_stats(pdf_path)
        total = stats.get("hits", 0) + stats.get("misses", 0)
        if total:
            rate = stats["hits"] / total * 100.0
            log.info("[v11.field_bbox] OCR cache hits=%d misses=%d (%.1f%%)",
                     stats["hits"], stats["misses"], rate)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys
    import json
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python field_bbox.py <pdf>")
        sys.exit(1)
    decl = {"declaration_no": "100269825131"}
    items = [{"item_name": "FOODSTUFFS", "hs_code": "1601.00.90 00"}]
    print(json.dumps(compute_field_bboxes(sys.argv[1], decl, items), indent=2))
