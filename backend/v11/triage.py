"""Document-type triage — the single authority on what KIND of document a job is.

Every extraction bug we chased shared one enabler: the "is this page digital or
scanned?" signal was re-derived ad hoc in three places (page_classifier, presto,
workflow) and never recorded, so nothing could route on it reliably, log it, or
test it. This module computes that decision ONCE, from the already-classified
pages, and hands back one authoritative record that:

  * routing reads  → digital → text fast-path; scanned CUSDEC → vision rescue,
  * the guard reads → scanned CUSDEC rate is inherently unreliable → flag,
  * the UI/logs show → "why is this slow / flagged?" is answered at second 0,
  * the DB stores  → `jobs.doc_class` becomes queryable ("how many scanned?"),
  * a golden test pins → detection can't silently drift.

Pure + deterministic + never raises. Consumes the page list the classifier
already produced (each page carries `label`, `has_text_layer`, `text_chars`),
plus a light text-layer probe of the CUSDEC page specifically.
"""
from typing import Dict, List, Optional

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

# Whole-document class thresholds on the fraction of pages carrying a text layer.
_DIGITAL_MIN_RATIO = 0.80   # ≥80% pages digital → DIGITAL
_SCANNED_MAX_RATIO = 0.20   # ≤20% pages digital → SCANNED; between → MIXED

DIGITAL = "DIGITAL"
SCANNED = "SCANNED"
MIXED = "MIXED"

# CUSDEC page markers (kept in sync with cusdec_rescue._MARKERS; a page is the
# customs declaration when it carries ≥2). Used to locate the CUSDEC page so we
# can ask the one question that actually drives rescue: is IT digital or scanned?
_CUSDEC_MARKERS = (
    "taxes and fees", "maccs service fee", "import/export customs duty",
    "release order", "cusdec", "customs declaration", "exchange rate",
)
_MIN_CUSDEC_TEXT = 200  # chars — a real text-layer CUSDEC has far more than this


def _is_cusdec_text(text: str) -> bool:
    tl = (text or "").lower()
    return sum(m in tl for m in _CUSDEC_MARKERS) >= 2


def _locate_cusdec_page(pdf_path: str):
    """Return (0-based page index, is_digital) of the CUSDEC page.

    Two cases:
      * text-layer CUSDEC → found by markers in extractable text → digital.
      * scanned CUSDEC    → the page renders text as an image, so markers won't
        appear in get_text(); we can't confirm it by text. Return (None, False)
        which the caller reads as "no digital CUSDEC → needs vision rescue".
    Never raises.
    """
    if not fitz or not pdf_path:
        return None, False
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return None, False
    try:
        for pg in range(doc.page_count):
            try:
                t = doc[pg].get_text() or ""
            except Exception:
                continue
            if _is_cusdec_text(t) and len(t.strip()) >= _MIN_CUSDEC_TEXT:
                return pg, True
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return None, False


def declaration_pages(pdf_path: str, cusdec_page: Optional[int],
                      declaration_no: Optional[str] = None) -> List[int]:
    """1-based pages the customs declaration occupies. Never raises.

    `cusdec_page` is the 0-based anchor `compute_triage` already found; pass it
    straight through rather than re-scanning. A MACCS declaration runs 1/3-3/3
    and only the FIRST sheet carries the tax block, so the marker probe finds
    the anchor and nothing else — the continuation sheets hold the item rows,
    which is exactly what an item highlight needs to point at.

    Those sheets are found by the declaration number, which is reprinted in the
    header of every one. Only pages AFTER the anchor count: the number is also
    quoted on the release-order notification that precedes the declaration in
    the bundle, and on `100313870641` and `100314743761` that alone pulled in
    page 9 and page 13 — attachments, not the declaration.

    Returns `[]` when the anchor is unknown, which is every scanned declaration.
    That is deliberate. A scanned page has no text layer, so no box could be
    found on it anyway; searching the rest of the bundle would return boxes
    drawn exclusively from attachments, which is the failure this exists to
    prevent. No box is the honest answer.

    Measured on the 20-document UAT corpus: 9 documents exact, 0 strays, 11
    empty (every one a scanned declaration).
    """
    if not fitz or not pdf_path or cusdec_page is None:
        return []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    try:
        anchor = int(cusdec_page)
        if not 0 <= anchor < doc.page_count:
            return []
        pages = {anchor + 1}
        dn = (str(declaration_no).strip() if declaration_no else "")
        if len(dn) >= 6:                      # too short and it matches anything
            for pg in range(anchor + 1, doc.page_count):
                try:
                    if dn in (doc[pg].get_text() or ""):
                        pages.add(pg + 1)
                except Exception:
                    continue
        return sorted(pages)
    except Exception:
        return []
    finally:
        try:
            doc.close()
        except Exception:
            pass


def compute_triage(pdf_path: str, cls: Dict, engine: str = "auto") -> Dict:
    """Build the authoritative document-type record from the classifier output.

    `cls` is the page_classifier result: {"pages": [{page,label,has_text_layer,
    text_chars,...}], "n_pages": N, ...}. Returns a plain dict (JSON-serialisable
    for the DB/SSE). Never raises — on any gap it degrades to a safe, review-biased
    verdict (treat unknown as scanned so the rate guard stays conservative).
    """
    pages: List[Dict] = (cls or {}).get("pages") or []
    n_pages = (cls or {}).get("n_pages") or len(pages) or 1
    n_text = sum(1 for p in pages if p.get("has_text_layer"))
    text_ratio = round(n_text / n_pages, 3) if n_pages else 0.0

    if text_ratio >= _DIGITAL_MIN_RATIO:
        doc_class = DIGITAL
    elif text_ratio <= _SCANNED_MAX_RATIO:
        doc_class = SCANNED
    else:
        doc_class = MIXED

    # The decision that actually matters for the rescue path: is the CUSDEC page
    # itself digital (text-layer) or a scan?
    cusdec_idx, cusdec_digital = _locate_cusdec_page(pdf_path)

    # Fast-path (Presto) eligibility: every TYPED page digital (same rule the
    # router used inline, now centralised here as the single source of truth).
    typed = [p for p in pages if (p.get("label") or "").upper() == "TYPED"]
    # `all(...)` here meant ONE scanned page switched the fast path off for the whole
    # bundle. These bundles almost always carry a scanned attachment — a stamped
    # licence, a photographed invoice — so the fast path was effectively never taken:
    # on a real 12-page document, pages 3, 6, 7, 8 and 9 had no text layer and Presto
    # never ran, even though the declaration page itself was perfectly readable. The
    # slower image re-read then supplied the header instead, and got the declaration
    # number, the customs value and the adjustment wrong on a page whose text layer
    # gives all three exactly.
    #
    # Presto only reads the pages handed to it, so the requirement is that SOME typed
    # page is digital — the caller passes just those. A scanned attachment no longer
    # disqualifies a readable declaration.
    typed_digital = [p for p in typed if p.get("has_text_layer")]
    fast_path_available = bool(typed_digital)

    det_rescue_available = bool(cusdec_digital)
    # Scanned/absent CUSDEC text → deterministic rescue can't run → vision rescue.
    needs_vision_rescue = not cusdec_digital

    return {
        "doc_class": doc_class,
        "n_pages": n_pages,
        "n_text_pages": n_text,
        "text_ratio": text_ratio,
        "cusdec_page": cusdec_idx,            # 0-based, or None if not text-located
        "cusdec_page_digital": bool(cusdec_digital),
        "fast_path_available": fast_path_available,
        "det_rescue_available": det_rescue_available,
        "needs_vision_rescue": needs_vision_rescue,
        # Human-readable one-liner for the SSE terminal / worker log.
        "summary": _summary(doc_class, cusdec_digital, fast_path_available),
    }


def _summary(doc_class, cusdec_digital, fast_path) -> str:
    cusdec = "text" if cusdec_digital else "image"
    path = "text fast-path" if fast_path else "vision"
    rescue = "deterministic rescue" if cusdec_digital else "vision rescue (rate will be flagged)"
    return f"{doc_class} (cusdec={cusdec}) → {path}; {rescue}"
