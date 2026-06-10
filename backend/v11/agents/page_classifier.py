"""V11 — Per-page classifier (TYPED/HANDWRITTEN/ATTACHMENT)."""
import base64, io, json, re, time
from pathlib import Path
from typing import Dict, List

import fitz
import requests
from PIL import Image

from v11.config import CLASSIFIER_MODEL, OPENROUTER_API_KEY, PRESTO_TEXT_LAYER_MIN_CHARS


API_URL = "https://openrouter.ai/api/v1/chat/completions"


PAGE_CLASS_PROMPT = """Classify each page of this Myanmar customs document.

Labels:
- TYPED: machine-printed MACCS form (computer-generated, typed text in boxes)
- HANDWRITTEN: ink-pen filled CUSDEC-1 form (handwritten values in boxes)
- ATTACHMENT: Bill of Lading, Commercial Invoice, Packing List, Certificate of Origin, Truck Receipt — any non-declaration supporting doc

Return strict JSON only:
{"pages": [{"page": 1, "label": "TYPED|HANDWRITTEN|ATTACHMENT", "confidence": "high|med|low", "reason": "short"}]}"""


def _render_thumbnails(pdf_path: str, dpi: int = 110) -> List[str]:
    out = []
    doc = fitz.open(str(pdf_path))
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 1100
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=72, optimize=True)
        out.append(base64.b64encode(buf.getvalue()).decode())
    doc.close()
    return out


def _probe_text_layers(pdf_path: str) -> Dict[int, int]:
    """V12 Presto rail: per-page count of extractable text characters.

    A digital PDF (computer-generated MACCS) carries its text in a text layer;
    a scanned PDF does not. Used downstream to route TYPED+digital pages to the
    Presto fast-path (text-layer extraction) instead of image+vision. Probe only
    — does not change classification or routing in Phase 0.

    Returns {page_number(1-based): char_count}. Never raises.
    """
    out: Dict[int, int] = {}
    try:
        doc = fitz.open(str(pdf_path))
        for i, page in enumerate(doc, 1):
            try:
                out[i] = len((page.get_text() or "").strip())
            except Exception:
                out[i] = 0
        doc.close()
    except Exception:
        pass
    return out


def _attach_text_layer(pages: List[Dict], probe: Dict[int, int]) -> List[Dict]:
    """Annotate each page dict with text-layer info (additive, non-breaking)."""
    for p in pages:
        chars = probe.get(p.get("page"), 0)
        p["text_chars"] = chars
        p["has_text_layer"] = chars >= PRESTO_TEXT_LAYER_MIN_CHARS
    return pages


# Strong, customs-DECLARATION-specific text markers. Invoices / packing lists /
# bills of lading do NOT carry these, so they won't be falsely promoted.
_DECL_MARKERS = (
    "customs value", "customs duty", "assessment", "exchange rate",
    "import/export", "maccs", "cusdec", "total customs", "declarant",
    "tariff", "c.i.f", "cif value",
)


def _marker_rescue(pages: List[Dict], pdf_path: str) -> List[Dict]:
    """Deterministic safety net for the vision classifier.

    A page the model tagged ATTACHMENT (often "blank/continuation") but that
    carries a rich text layer containing ≥2 customs-declaration markers is almost
    certainly the real declaration mis-tagged inside a bundled release order.
    Flip it to TYPED so an extraction engine actually reads it. No API call.
    """
    try:
        doc = fitz.open(str(pdf_path))
        texts = {}
        for i, pg in enumerate(doc, 1):
            try:
                texts[i] = (pg.get_text() or "").lower()
            except Exception:
                texts[i] = ""
        doc.close()
    except Exception:
        return pages
    for p in pages:
        if (p.get("label") or "").upper() != "ATTACHMENT":
            continue
        t = texts.get(p.get("page"), "")
        if len(t.strip()) < 400:
            continue
        hits = sum(1 for m in _DECL_MARKERS if m in t)
        if hits >= 2:
            p["label"] = "TYPED"
            p["confidence"] = "med"
            p["reason"] = f"text-layer rescue ({hits} decl markers)"[:80]
            p["_rescued"] = True
    return pages


def _parse_json(raw: str):
    if not raw: return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"): s = s[4:]
    s = s.strip()
    if s.endswith("```"): s = s[:-3].strip()
    if "{" not in s: return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in [s, re.sub(r",(\s*[\}\]])", r"\1", s)]:
        try: return json.loads(cand)
        except Exception: continue
    return None


def classify_pages(pdf_path: str) -> Dict:
    """Classify all pages of a PDF.
    Returns:
      {
        "pages": [{"page": int, "label": str, "confidence": str, "reason": str}],
        "n_pages": int,
        "summary": {"TYPED": int, "HANDWRITTEN": int, "ATTACHMENT": int},
      }
    Falls back to all-TYPED if classification fails."""
    thumbs = _render_thumbnails(pdf_path)
    n = len(thumbs)
    # V12 Presto rail: probe text layers once (cheap, no API). Attached to pages
    # below; does not affect classification or routing in Phase 0.
    text_probe = _probe_text_layers(pdf_path)
    if n == 0:
        return {"pages": [], "n_pages": 0,
                "summary": {"TYPED": 0, "HANDWRITTEN": 0, "ATTACHMENT": 0}}

    parts = []
    for i, b64 in enumerate(thumbs, 1):
        parts.append({"type": "text", "text": f"Page {i}:"})
        parts.append({"type": "image_url",
                       "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    parts.append({"type": "text", "text": PAGE_CLASS_PROMPT})

    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [{"role": "user", "content": parts}],
        "temperature": 0,
        "max_tokens": 3000,
    }

    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=120)
            dt = time.time() - t0
            print(f"[Classifier] HTTP {r.status_code} {dt:.1f}s ({CLASSIFIER_MODEL})")
            if r.status_code == 200:
                raw = r.json()["choices"][0]["message"]["content"]
                parsed = _parse_json(raw)
                if parsed and "pages" in parsed:
                    pages = []
                    summary = {"TYPED": 0, "HANDWRITTEN": 0, "ATTACHMENT": 0}
                    for p in parsed["pages"]:
                        label = (p.get("label") or "TYPED").upper()
                        if label not in summary:
                            label = "TYPED"
                        pages.append({
                            "page": p.get("page"),
                            "label": label,
                            "confidence": p.get("confidence", "med"),
                            "reason": p.get("reason", "")[:80],
                        })
                        summary[label] += 1
                    _attach_text_layer(pages, text_probe)
                    # Deterministic rescue: promote mis-tagged declaration pages
                    # (text layer + decl markers) back to TYPED, then recount.
                    _marker_rescue(pages, pdf_path)
                    summary = {"TYPED": 0, "HANDWRITTEN": 0, "ATTACHMENT": 0}
                    for _p in pages:
                        summary[_p["label"]] = summary.get(_p["label"], 0) + 1
                    return {"pages": pages, "n_pages": n, "summary": summary}
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                print(f"[Classifier] body: {r.text[:200]}")
        except Exception as e:
            print(f"[Classifier] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    # Fallback: all-TYPED
    fallback_pages = _attach_text_layer(
        [{"page": i, "label": "TYPED", "confidence": "low",
          "reason": "classifier fallback"} for i in range(1, n + 1)],
        text_probe,
    )
    return {"pages": fallback_pages,
            "n_pages": n,
            "summary": {"TYPED": n, "HANDWRITTEN": 0, "ATTACHMENT": 0}}
