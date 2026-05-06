"""V10 PRO — Geometric form box detection.

Heuristic: Myanmar CUSDEC-1/MACCS forms have a fixed grid layout. We extract
all rectangles from PyMuPDF page drawings, filter by typical box sizes, and
identify candidates for known fields based on relative position on the page.

This is best-effort — if no boxes detected, callers fall back to full-page render."""
import io
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz


# Approximate normalized positions of key boxes on Myanmar customs declaration form.
# Coordinates are fractions of page width/height (top-left origin).
# These are HEURISTIC — actual forms vary. Tune per importer / template.
KNOWN_BOX_POSITIONS = {
    "declaration_no": {"x": 0.55, "y": 0.05, "w": 0.30, "h": 0.04, "page": 1},
    "declaration_date": {"x": 0.55, "y": 0.10, "w": 0.30, "h": 0.04, "page": 1},
    "importer_name": {"x": 0.05, "y": 0.18, "w": 0.45, "h": 0.06, "page": 1},
    "consignor_name": {"x": 0.05, "y": 0.10, "w": 0.45, "h": 0.06, "page": 1},
    "exchange_rate": {"x": 0.55, "y": 0.30, "w": 0.30, "h": 0.04, "page": 1},
    "currency": {"x": 0.55, "y": 0.25, "w": 0.20, "h": 0.04, "page": 1},
    "total_customs_value": {"x": 0.55, "y": 0.55, "w": 0.30, "h": 0.05, "page": 1},
    "country_origin": {"x": 0.05, "y": 0.55, "w": 0.20, "h": 0.04, "page": 1},
}


def get_page_size(pdf_path: str, page: int = 1) -> Optional[Tuple[float, float]]:
    """Return (width, height) in PDF points for the given page."""
    try:
        doc = fitz.open(str(pdf_path))
        if page < 1 or page > len(doc):
            doc.close(); return None
        rect = doc[page - 1].rect
        size = (rect.width, rect.height)
        doc.close()
        return size
    except Exception:
        return None


def get_known_box(field: str, pdf_path: str, page: int = 1) -> Optional[Dict]:
    """Return absolute (x, y, w, h) PDF-point coordinates for a known field on a known page.

    Returns None if no known position for this field."""
    pos = KNOWN_BOX_POSITIONS.get(field)
    if not pos:
        return None
    size = get_page_size(pdf_path, page)
    if not size:
        return None
    pw, ph = size
    return {
        "field": field,
        "page": pos.get("page", page),
        "x": pos["x"] * pw,
        "y": pos["y"] * ph,
        "w": pos["w"] * pw,
        "h": pos["h"] * ph,
        "method": "fixed_template",
    }


def extract_rectangles(pdf_path: str, page: int = 1,
                        min_width: float = 30, min_height: float = 10,
                        max_width: float = 600, max_height: float = 80) -> List[Dict]:
    """Extract all candidate rectangle regions from a PDF page using vector drawings.

    Useful when fixed template doesn't match — find boxes by geometry."""
    rects = []
    try:
        doc = fitz.open(str(pdf_path))
        if page < 1 or page > len(doc):
            doc.close()
            return rects
        p = doc[page - 1]
        for d in p.get_drawings():
            r = d.get("rect")
            if not r: continue
            w = r.width
            h = r.height
            if min_width <= w <= max_width and min_height <= h <= max_height:
                rects.append({
                    "x": r.x0, "y": r.y0, "w": w, "h": h,
                    "fill": d.get("fill"), "stroke": d.get("stroke"),
                })
        doc.close()
    except Exception as e:
        print(f"[BoxDetect] extract error: {e}")
    return rects


def detect_box_for_field(field: str, pdf_path: str, page: int = 1) -> Optional[Dict]:
    """Best-effort box detection. First try template, fall back to geometry."""
    # Try template first
    box = get_known_box(field, pdf_path, page)
    if box:
        return box
    # Fall back to geometric extraction (returns first match by typical size)
    rects = extract_rectangles(pdf_path, page)
    if not rects:
        return None
    # Pick top-half rect for header fields, bottom-half for value fields
    page_size = get_page_size(pdf_path, page)
    if not page_size:
        return None
    pw, ph = page_size
    if field in ("declaration_no", "consignor_name", "importer_name"):
        candidates = [r for r in rects if r["y"] < ph * 0.3]
    else:
        candidates = rects
    if not candidates:
        return None
    # Pick widest as most likely declaration field box
    candidates.sort(key=lambda r: -r["w"])
    r = candidates[0]
    return {**r, "field": field, "page": page, "method": "geometric"}


def render_box(pdf_path: str, box: Dict, dpi: int = 600) -> Optional[bytes]:
    """Render only the box region as JPEG bytes."""
    try:
        from PIL import Image
        doc = fitz.open(str(pdf_path))
        page = box.get("page", 1)
        if page < 1 or page > len(doc):
            doc.close(); return None
        rect = fitz.Rect(box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"])
        pix = doc[page - 1].get_pixmap(dpi=dpi, clip=rect)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        doc.close()
        return buf.getvalue()
    except Exception as e:
        print(f"[BoxDetect] render error: {e}")
        return None
