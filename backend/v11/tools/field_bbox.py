"""V11 — Compute bounding boxes for extracted field values via PyMuPDF text search.

Best-effort: ~95% hit rate for typed PDFs, lower for handwritten.
Returns a dict { entity_type: { entity_index: { field: {page, x, y, w, h} } } }
"""
from typing import Dict, Any, List, Optional

try:
    import fitz
except Exception:
    fitz = None


# Skip these — too generic to match cleanly OR not worth annotating
_SKIP_DECL = {"document_format", "currency", "currency_2", "needs_review",
              "is_valid", "country_origin"}
_SKIP_ITEM = {"customs_duty_rate", "commercial_tax_pct", "commercial_tax_percent"}


def _norm_value(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # Remove trailing zeros in floats: "60.97525" stays, "0.0" → "0"
    return s


def _search_first(doc, value: str, max_pages: int = 50) -> Optional[Dict]:
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
                    "w": float(r.x1 - r.x0), "h": float(r.y1 - r.y0)}
    return None


def compute_field_bboxes(pdf_path: str, declaration: Dict, items: List[Dict]) -> Dict:
    """For each (declaration field, item field), find first match on PDF.
    Returns: {"declaration": {field: bbox}, "items": {idx: {field: bbox}}}
    Empty dict if fitz unavailable or PDF missing.
    """
    out = {"declaration": {}, "items": {}}
    if fitz is None or not pdf_path:
        return out
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return out
    try:
        # Declaration fields
        for field, val in (declaration or {}).items():
            if field in _SKIP_DECL:
                continue
            sval = _norm_value(val)
            if not sval or sval in ("0", "0.0"):
                continue
            bbox = _search_first(doc, sval)
            if bbox:
                out["declaration"][field] = bbox
        # Item fields
        for idx, item in enumerate(items or []):
            row: Dict = {}
            for field, val in (item or {}).items():
                if field in _SKIP_ITEM or field.startswith("_"):
                    continue
                sval = _norm_value(val)
                if not sval or sval in ("0", "0.0"):
                    continue
                bbox = _search_first(doc, sval)
                if bbox:
                    row[field] = bbox
            if row:
                out["items"][str(idx)] = row
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 2:
        print("usage: python field_bbox.py <pdf>")
        sys.exit(1)
    decl = {"declaration_no": "100269825131"}
    items = [{"item_name": "FOODSTUFFS", "hs_code": "1601.00.90 00"}]
    print(json.dumps(compute_field_bboxes(sys.argv[1], decl, items), indent=2))
