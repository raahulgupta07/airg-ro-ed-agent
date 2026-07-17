"""Visual verification overlay. Draws a colored box on the customs PDF over the
place each extracted field's value was found, so a reviewer can see — at a glance —
exactly where every number came from. Boxes are colored by status: amber = disputed
(suspect/review), teal = choice field (has alternates), green = clean. No LLM, no
network: pure fitz text search on the value the pipeline already stored."""
import fitz
import re
import os


# --- box colors (RGB 0..1) -------------------------------------------------
_AMBER = (0.78, 0.52, 0.07)   # suspect / review — a disputed value
_TEAL = (0.17, 0.48, 0.51)    # choice / disputed field — has alternates
_GREEN = (0.18, 0.55, 0.34)   # clean, single-source value


def _value_variants(v) -> list:
    """Search strings to try on the page for a stored value, in priority order.

    Yields the raw string first, then numeric spellings so a value stored plainly
    (198450000) still matches a page that prints it grouped ("198,450,000"), and a
    prefixed code like "A- 960773210" also matches its bare digit core. De-duped,
    empties dropped, order preserved.
    """
    if v is None:
        return []
    raw = str(v).strip()
    out = []

    def _add(s):
        if s:
            s = str(s).strip()
            if s and s not in out:
                out.append(s)

    _add(raw)

    # strip a trailing ".0" (float that is really an integer)
    trimmed = re.sub(r"\.0+$", "", raw)
    _add(trimmed)

    # digit core: longest run of digits (with optional decimal) inside the string,
    # e.g. "A- 960773210" -> "960773210", "198,450,000" -> "198450000"
    for m in re.findall(r"\d[\d,]*\.?\d*", raw):
        digits = m.replace(",", "")
        if not digits:
            continue
        digits = re.sub(r"\.0+$", "", digits)
        _add(digits)
        # comma-grouped form of the integer part
        int_part, _, frac = digits.partition(".")
        if int_part.isdigit() and len(int_part) > 3:
            grouped = "{:,}".format(int(int_part))
            _add(grouped + ("." + frac if frac else ""))

    return [s for s in out if s]


def _color(cell) -> tuple:
    """Box color for a cell dict: amber (disputed) > teal (choice) > green (clean)."""
    status = (cell.get("status") or "").lower()
    if status in ("suspect", "review"):
        return _AMBER
    if cell.get("alternates"):
        return _TEAL
    return _GREEN


def annotate(pdf_path, record, out_path):
    """Render the PDF with a colored box over each located field value.

    For every populated cell in `record`, search every page for the field's value
    (trying each `_value_variants` spelling) and draw one labeled box at the first
    hit. Pages that received a box are rendered at 2x to PNG. Returns the PNG path
    (single page), a list of paths (multiple annotated pages), or — if no field was
    found anywhere — page 1 rendered unannotated as a preview. Returns None on any
    error. The document is always closed.
    """
    doc = None
    try:
        doc = fitz.open(pdf_path)
        annotated_pages = set()

        for col, cell in record.items():
            if not isinstance(cell, dict):
                continue
            value = cell.get("value")
            if value is None or str(value).strip() == "":
                continue

            color = _color(cell)
            found = False
            for needle in _value_variants(value):
                for page in doc:
                    rects = page.search_for(needle)
                    if rects:
                        rect = rects[0]
                        page.draw_rect(rect, color=color, width=2)
                        page.insert_text(
                            (rect.x0, rect.y0 - 2),
                            col[:14],
                            color=color,
                            fontsize=7,
                        )
                        annotated_pages.add(page.number)
                        found = True
                        break
                if found:
                    break

        matrix = fitz.Matrix(2, 2)
        stem, ext = os.path.splitext(out_path)
        ext = ext or ".png"

        if not annotated_pages:
            # No text layer hit at all — still give a preview of page 1.
            doc[0].get_pixmap(matrix=matrix).save(out_path)
            return out_path

        pages = sorted(annotated_pages)
        if len(pages) == 1:
            doc[pages[0]].get_pixmap(matrix=matrix).save(out_path)
            return out_path

        paths = []
        for n in pages:
            p = "{}_p{}{}".format(stem, n + 1, ext)
            doc[n].get_pixmap(matrix=matrix).save(p)
            paths.append(p)
        return paths
    except Exception:
        return None
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


def annotate_result(result, pdf_dir, out_dir):
    """Convenience wrapper: annotate a pipeline `result` (`result["pdf"]` +
    `result["record"]`) from `pdf_dir` into `out_dir`, returning the PNG path(s)
    as a list (empty on failure)."""
    pdf_path = os.path.join(pdf_dir, result["pdf"])
    out = os.path.join(out_dir, result["pdf"].replace(".pdf", "") + "_annot.png")
    os.makedirs(out_dir, exist_ok=True)
    res = annotate(pdf_path, result["record"], out)
    if res is None:
        return []
    if isinstance(res, list):
        return res
    return [res]
