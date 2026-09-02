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

Give TWO independent answers per page.

1. `label` — how the page is FILLED IN:
- TYPED: machine-printed MACCS form (computer-generated, typed text in boxes)
- HANDWRITTEN: ink-pen filled CUSDEC-1 form (handwritten values in boxes)
- ATTACHMENT: Bill of Lading, Commercial Invoice, Packing List, Certificate of Origin, Truck Receipt — any non-declaration supporting doc

2. `document` — WHICH DOCUMENT the page belongs to. This is a different question and
   the answer is often different: an Import Licence is machine-printed (TYPED) but is
   NOT the declaration, and a CUSDEC filled in by hand (HANDWRITTEN) still IS.
- DECLARATION: the customs declaration itself — CUSDEC-1 / MACCS IMPORT DECLARATION,
  and its CONTINUATION SHEETS. Titled "IMPORT DECLARATION" / "CUSDEC 1"; carries
  "Taxes and fees", "Import Duty", "Commercial Tax", a Registration No.
  A continuation sheet says "Continuation Sheet" and repeats the same registration
  number — it is part of the DECLARATION, not an attachment.
- LICENCE: Import Licence / Permit. Titled "IMPORT LICENCE", marked "APPENDIX 4b",
  carries "Licence No.", "Ministry of Commerce", "Total CIF Value (Kyats)". It lists
  every good the importer is PERMITTED to import — usually MORE goods than this
  shipment actually contains. It is NOT the declaration.
- INVOICE: commercial invoice from the seller.
- PACKING_LIST: packing list / weight list.
- OTHER: anything else (B/L, C/O, delivery order, valuation note, blank page).

If you cannot tell which document a page belongs to, answer OTHER. Do not guess
DECLARATION — a wrong DECLARATION is worse than an honest OTHER.

Return strict JSON only:
{"pages": [{"page": 1, "label": "TYPED|HANDWRITTEN|ATTACHMENT",
            "document": "DECLARATION|LICENCE|INVOICE|PACKING_LIST|OTHER",
            "confidence": "high|med|low", "reason": "short"}]}"""

#: The `document` vocabulary. UNKNOWN is what a page gets when nothing — model or
#: text — established an identity; it is never a synonym for OTHER. OTHER means
#: "identified, and it is not one of the named documents"; UNKNOWN means "not
#: identified", and only UNKNOWN is allowed to fall back to legacy behaviour.
DOCUMENTS = ("DECLARATION", "LICENCE", "INVOICE", "PACKING_LIST", "OTHER", "UNKNOWN")


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


# Title-block phrases that identify a document beyond argument. Deliberately NOT
# the generic customs vocabulary in `_DECL_MARKERS`: an Import Licence prints
# "Total CIF Value (Kyats)" and a goods table too, so anything that keys on
# "cif value" or "hs code" identifies the wrong document with full confidence.
# These are the words that appear on ONE form and not the others.
_LICENCE_MARKERS = (
    "import licence", "import license", "appendix 4b",
    "ministry of commerce", "licence no", "license no",
    "total cif value",              # the licence's own total, not the CUSDEC's
)
_DECLARATION_MARKERS = (
    "import declaration", "cusdec", "taxes and fees",
    "maccs service fee", "import/export customs duty",
    "declaration no", "registration no", "continuation sheet",
)


def _document_from_text(text: str) -> str:
    """Identify a page's document from its printed title block, or UNKNOWN.

    Requires TWO hits and a clear winner. One phrase is not an identity: a
    declaration references its licence number, and a licence names the importer,
    so single markers cross over between the two forms. A tie is UNKNOWN — the
    model's answer then stands, which is the right outcome when the text cannot
    settle it.
    """
    t = (text or "").lower()
    if len(t.strip()) < 200:
        return "UNKNOWN"
    lic = sum(1 for m in _LICENCE_MARKERS if m in t)
    dec = sum(1 for m in _DECLARATION_MARKERS if m in t)
    if lic >= 2 and lic > dec:
        return "LICENCE"
    if dec >= 2 and dec > lic:
        return "DECLARATION"
    return "UNKNOWN"


def _document_rescue(pages: List[Dict], pdf_path: str) -> List[Dict]:
    """Settle `document` from the text layer where the text layer can settle it.

    Text beats the model here, and only here: these are printed titles, not a
    judgement. On a photographed bundle every page returns UNKNOWN and the
    model's answer is all there is — which is exactly the case this whole
    feature exists for, so the model's answer must remain usable. Never raises.
    """
    try:
        doc = fitz.open(str(pdf_path))
        texts = {}
        for i, pg in enumerate(doc, 1):
            try:
                texts[i] = pg.get_text() or ""
            except Exception:
                texts[i] = ""
        doc.close()
    except Exception:
        return pages
    for p in pages:
        found = _document_from_text(texts.get(p.get("page"), ""))
        if found != "UNKNOWN" and found != p.get("document"):
            p["document_model"] = p.get("document")
            p["document"] = found
            p["document_source"] = "text"
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
                        # Which DOCUMENT this page belongs to — a separate axis
                        # from how it is filled in. An unrecognised answer becomes
                        # UNKNOWN, never DECLARATION: the whole point of the field
                        # is to stop a licence being treated as the declaration,
                        # and a permissive default would reintroduce exactly that.
                        document = (p.get("document") or "UNKNOWN").upper()
                        if document not in DOCUMENTS:
                            document = "UNKNOWN"
                        pages.append({
                            "page": p.get("page"),
                            "label": label,
                            "document": document,
                            "document_source": "model",
                            "confidence": p.get("confidence", "med"),
                            "reason": p.get("reason", "")[:80],
                        })
                        summary[label] += 1
                    _attach_text_layer(pages, text_probe)
                    # Printed title blocks settle `document` where they exist.
                    _document_rescue(pages, pdf_path)
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

    # Fallback: all-TYPED, and every document UNKNOWN — the classifier never
    # answered, so nothing here is an identification. `_document_rescue` still
    # runs: on a digital bundle the printed titles settle it with no model at
    # all, which is the one case where a classifier outage costs nothing.
    # Downstream must treat an all-UNKNOWN document set as "no scoping possible"
    # and keep legacy behaviour rather than dropping every item.
    fallback_pages = _attach_text_layer(
        [{"page": i, "label": "TYPED", "confidence": "low",
          "document": "UNKNOWN", "document_source": "fallback",
          "reason": "classifier fallback"} for i in range(1, n + 1)],
        text_probe,
    )
    _document_rescue(fallback_pages, pdf_path)
    return {"pages": fallback_pages,
            "n_pages": n,
            "summary": {"TYPED": n, "HANDWRITTEN": 0, "ATTACHMENT": 0}}
