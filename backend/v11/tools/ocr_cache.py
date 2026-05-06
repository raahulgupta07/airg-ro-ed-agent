"""V11 — Per-PDF OCR cache for image-only/scanned PDFs.

Lazy-imports PaddleOCR (heavy: ~600MB CPU). If import fails, OCR is silently
disabled and the cache returns empty page results so callers fall back gracefully.

Each cache entry is keyed by (pdf_path, page_number) and stores a list of
{"text": str, "bbox_img": (x, y, w, h)} in *rendered image pixel space* (200 DPI).
Coordinate translation back to PDF space is done by the caller, since the caller
already holds the page rect.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Lazy global PaddleOCR singleton.
_ocr = None
_ocr_init_attempted = False
_ocr_lock = threading.Lock()

# Render DPI used for OCR. 200 DPI is a good text/cost trade-off.
OCR_DPI = 200


def _get_ocr():
    """Return a PaddleOCR instance or None if unavailable. Initialized once."""
    global _ocr, _ocr_init_attempted
    if _ocr is not None or _ocr_init_attempted:
        return _ocr
    with _ocr_lock:
        if _ocr_init_attempted:
            return _ocr
        _ocr_init_attempted = True
        try:
            from paddleocr import PaddleOCR  # type: ignore
            # PaddleOCR 2.x → 3.x changed kwargs (e.g. show_log, use_angle_cls were
            # removed in 3.x). Try richest config first, fall back to minimal.
            # PaddleOCR 3.x: disable heavy doc-preprocessing & textline-orient
            # to keep memory in check (OCR is fallback-only, not core).
            # PaddleOCR 2.x: ignores unknown kwargs gracefully on a per-attempt basis.
            # Prefer mobile detection model — server detection is OOM-prone on
            # constrained CPU containers (esp. ARM64). Fall back through other
            # API shapes for compat with PaddleOCR 2.x.
            attempts = (
                dict(lang="en",
                     text_detection_model_name="PP-OCRv5_mobile_det",
                     text_recognition_model_name="en_PP-OCRv5_mobile_rec",
                     use_doc_orientation_classify=False,
                     use_doc_unwarping=False,
                     use_textline_orientation=False),
                dict(lang="en",
                     use_doc_orientation_classify=False,
                     use_doc_unwarping=False,
                     use_textline_orientation=False),
                dict(lang="en", use_angle_cls=True, show_log=False),
                dict(lang="en", use_angle_cls=True),
                dict(lang="en"),
                dict(),
            )
            last_err: Optional[Exception] = None
            for kw in attempts:
                try:
                    _ocr = PaddleOCR(**kw)
                    log.info("[v11.ocr_cache] PaddleOCR initialized kw=%s DPI=%d", kw, OCR_DPI)
                    break
                except Exception as e:  # try next config
                    last_err = e
                    _ocr = None
            if _ocr is None and last_err is not None:
                raise last_err
        except Exception as e:
            log.warning("[v11.ocr_cache] PaddleOCR unavailable, OCR fallback disabled: %s", e)
            _ocr = None
        return _ocr


# Cache: { pdf_path: { page_no_0based: [ {text, bbox_img:(x,y,w,h), pdf_w, pdf_h, img_w, img_h} ] } }
_PAGE_CACHE: Dict[str, Dict[int, List[Dict]]] = {}
_CACHE_STATS: Dict[str, Dict[str, int]] = {}  # pdf_path -> {hits, misses}


def get_cache_stats(pdf_path: str) -> Dict[str, int]:
    return dict(_CACHE_STATS.get(pdf_path, {"hits": 0, "misses": 0}))


def reset_cache(pdf_path: Optional[str] = None) -> None:
    if pdf_path is None:
        _PAGE_CACHE.clear()
        _CACHE_STATS.clear()
    else:
        _PAGE_CACHE.pop(pdf_path, None)
        _CACHE_STATS.pop(pdf_path, None)


def _bump(pdf_path: str, key: str) -> None:
    s = _CACHE_STATS.setdefault(pdf_path, {"hits": 0, "misses": 0})
    s[key] = s.get(key, 0) + 1


def get_page_ocr(pdf_path: str, doc, page_no_0based: int) -> List[Dict]:
    """Return cached OCR spans for a page, running OCR if not cached.

    Each span is a dict:
      {
        "text":   str,
        "bbox_img": (x, y, w, h)  # rendered-image pixel coords
        "img_w": int, "img_h": int,
        "pdf_w": float, "pdf_h": float,
      }

    Returns [] if OCR unavailable or page render failed.
    """
    pdf_cache = _PAGE_CACHE.setdefault(pdf_path, {})
    if page_no_0based in pdf_cache:
        _bump(pdf_path, "hits")
        return pdf_cache[page_no_0based]

    _bump(pdf_path, "misses")
    ocr = _get_ocr()
    if ocr is None:
        pdf_cache[page_no_0based] = []
        return []

    try:
        import fitz  # noqa: F401
        page = doc[page_no_0based]
        pdf_w = float(page.rect.width)
        pdf_h = float(page.rect.height)
        # 200 DPI render. PDF default 72 DPI → zoom = 200/72.
        zoom = OCR_DPI / 72.0
        import fitz as _fitz
        mat = _fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_w = int(pix.width)
        img_h = int(pix.height)
        png_bytes = pix.tobytes("png")
    except Exception as e:
        log.warning("[v11.ocr_cache] page render failed p=%d: %s", page_no_0based, e)
        pdf_cache[page_no_0based] = []
        return []

    try:
        import numpy as np
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        arr = np.array(img)
        # PaddleOCR 2.x: .ocr(img, cls=True) → list[list[ [poly, (text, conf)] ]]
        # PaddleOCR 3.x: .predict(img) → list[dict]  with 'rec_texts' & 'rec_polys'
        result = None
        if hasattr(ocr, "ocr"):
            try:
                result = ocr.ocr(arr, cls=True)
            except TypeError:
                try:
                    result = ocr.ocr(arr)
                except Exception:
                    result = None
        if result is None and hasattr(ocr, "predict"):
            result = ocr.predict(arr)
    except Exception as e:
        log.warning("[v11.ocr_cache] OCR call failed p=%d: %s", page_no_0based, e)
        pdf_cache[page_no_0based] = []
        return []

    spans: List[Dict] = []
    try:
        # ---- Detect output shape ----
        # PaddleOCR 3.x: result is list[dict] with keys including 'rec_texts'
        #   and 'rec_polys' (or 'dt_polys'). Each entry is one page-result.
        # PaddleOCR 2.x: result is list[list[ [poly, (text, conf)] ]]
        #   (outer list = pages, inner = lines).
        page_entry = None
        if isinstance(result, list) and result:
            page_entry = result[0]

        def _add_span(text: str, poly) -> None:
            if not text:
                return
            try:
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
            except Exception:
                return
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            spans.append({
                "text": str(text),
                "bbox_img": (x0, y0, x1 - x0, y1 - y0),
                "img_w": img_w, "img_h": img_h,
                "pdf_w": pdf_w, "pdf_h": pdf_h,
            })

        if isinstance(page_entry, dict):
            # PaddleOCR 3.x dict-style result
            texts = page_entry.get("rec_texts") or page_entry.get("texts") or []
            polys = page_entry.get("rec_polys") or page_entry.get("dt_polys") or \
                    page_entry.get("polys") or []
            for t, p in zip(texts, polys):
                _add_span(t, p)
        else:
            # PaddleOCR 2.x style
            rows = page_entry if isinstance(page_entry, list) else result
            for row in rows or []:
                if not row or len(row) < 2:
                    continue
                poly = row[0]
                text_conf = row[1]
                text = text_conf[0] if isinstance(text_conf, (list, tuple)) else text_conf
                _add_span(text, poly)
    except Exception as e:
        log.warning("[v11.ocr_cache] parse OCR result failed p=%d: %s", page_no_0based, e)

    pdf_cache[page_no_0based] = spans
    log.info("[v11.ocr_cache] OCR p=%d  spans=%d", page_no_0based, len(spans))
    return spans


def img_bbox_to_pdf(span: Dict) -> Tuple[float, float, float, float]:
    """Translate (x,y,w,h) from rendered image pixels back to PDF page coords."""
    x, y, w, h = span["bbox_img"]
    sx = span["pdf_w"] / max(span["img_w"], 1)
    sy = span["pdf_h"] / max(span["img_h"], 1)
    return x * sx, y * sy, w * sx, h * sy
