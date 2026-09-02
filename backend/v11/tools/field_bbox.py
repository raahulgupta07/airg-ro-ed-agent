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

import re as _re
_ISO_DATE = _re.compile(r"\d{4}-\d{2}-\d{2}")


def _norm_value(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    # Remove trailing zeros in floats: "60.97525" stays, "0.0" → "0"
    return s


def _variants(v: Any) -> List[str]:
    """Every way this value might be PRINTED, best guess first.

    The database holds `98773433.29` and the form prints `98,773,433.29`, so a
    verbatim search never matched a money field. Measured on a real digital
    declaration: 6 of 23 fields got a box, and every miss was a number — the
    money rows, which are exactly the ones a reviewer wants to see on the page.

    Grouping is driven by the value's TYPE, not by whether the text happens to
    parse as a number. `declaration_no` is the string "100313870641"; rendering
    it as "100,313,870,641" would match nothing, and a partial match on a grouped
    rendering would box the wrong figure. Numeric columns arrive as int/float, so
    the type is the honest signal.
    """
    s = _norm_value(v)
    if not s:
        return []
    out = [s]

    def _add(x):
        if x and x not in out:
            out.append(x)

    if isinstance(v, bool):
        return out
    if isinstance(v, (int, float)):
        f = float(v)
        # `14816014.0` is an artefact of a float column; the form prints
        # `14,816,014`. Only drop the decimals when there are none to lose.
        whole = f.is_integer()
        plain = f"{int(f)}" if whole else s
        _add(plain)
        _add(f"{int(f):,}" if whole else f"{f:,}".rstrip("0").rstrip("."))
    elif _ISO_DATE.fullmatch(s):
        # Dates are STORED as ISO — `dates.py` normalises every engine's output
        # so the four date columns stop holding three formats at once. The forms
        # print them differently, and searching the stored spelling found
        # nothing: `declaration_date` is `2025-10-14` in the column and
        # `2025/10/14` on the page, so it got no box and the review screen fell
        # back to claiming page 1.
        y, m, d = s.split("-")
        _add(f"{y}/{m}/{d}")
        _add(f"{d}/{m}/{y}")
        _add(f"{d}.{m}.{y}")
        _add(f"{d}-{m}-{y}")
    elif "," in s:
        # Already grouped in the engine's output — also try it ungrouped, since
        # some forms print the same figure both ways.
        _add(s.replace(",", ""))
    return out


def _page_order(doc, pages: Optional[List[int]], max_pages: int = 50) -> List[int]:
    """Zero-based page indices to search, in order.

    `pages` is the 1-based set the declaration was read from. `None` means the
    caller does not know, and the whole document is searched exactly as before —
    every historical job and every path that predates this argument depends on
    that. An EMPTY list is not the same thing: it means the classifier found no
    declaration page at all, and searching everything there would put a box on
    an attachment in the one case we understand least.
    """
    if pages is None:
        return list(range(min(len(doc), max_pages)))
    return [p - 1 for p in pages if 1 <= p <= len(doc)]


def _search_first(doc, value: Any, order: List[int]) -> Optional[Dict]:
    """First page in `order` carrying any printed form of `value`.

    Every variant is tried on a page before moving to the next page, so a
    declaration page always beats a continuation sheet — pages are ranked by the
    caller and the ranking has to win over the spelling.
    """
    forms = [f for f in _variants(value) if len(f) >= 2]
    if not forms:
        return None
    forms = [f if len(f) <= 80 else f[:80] for f in forms]
    for pno in order:
        for form in forms:
            try:
                rects = doc[pno].search_for(form, quads=False)
            except Exception:
                continue
            if rects:
                r = rects[0]
                return {"page": pno + 1, "x": float(r.x0), "y": float(r.y0),
                        "w": float(r.x1 - r.x0), "h": float(r.y1 - r.y0)}
    return None


def compute_field_bboxes(pdf_path: str, declaration: Dict, items: List[Dict],
                         pages: Optional[List[int]] = None) -> Dict:
    """For each (declaration field, item field), find first match on PDF.

    `pages` — 1-based page numbers the declaration was read from. These are
    bundled release orders: one PDF holds the CUSDEC, the import licence, the
    invoice, the packing list and the delivery order, and the same importer name
    or invoice number is printed on several of them. Searching the whole bundle
    and taking the first hit sends the reviewer to the invoice to confirm a
    customs figure — a real occurrence of the right text on the wrong document,
    which is worse than showing no box at all.

    Pass `None` (the default) to search the whole document, which is what every
    caller did before this argument existed.

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
        order = _page_order(doc, pages)
        if not order:
            return out
        # Declaration fields
        for field, val in (declaration or {}).items():
            if field in _SKIP_DECL:
                continue
            sval = _norm_value(val)
            if not sval or sval in ("0", "0.0"):
                continue
            # The RAW value, not `sval` — the printed form is derived from the
            # type, and stringifying first throws that away.
            bbox = _search_first(doc, val, order)
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
                # The RAW value, not `sval` — the printed form is derived from the
                # type, and stringifying first throws that away.
                bbox = _search_first(doc, val, order)
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
