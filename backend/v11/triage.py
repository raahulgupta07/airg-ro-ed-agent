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
    fast_path_available = bool(typed) and all(p.get("has_text_layer") for p in typed)

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
