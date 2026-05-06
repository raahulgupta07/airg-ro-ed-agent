"""V9 — Page filter agent (Haiku).
Classifies each page of a PDF as DECL (customs declaration) or ATTACHMENT
(BL, packing list, invoice, certificate of origin, truck receipt, etc).
Returns list of page indices to keep for the main extraction.
"""
import base64
import io
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import fitz
from PIL import Image
from agno.agent import Agent
from agno.models.openrouter import OpenRouter

from v10.config import FILTER_MODEL, OPENROUTER_API_KEY


FILTER_SYSTEM = """You are a page classifier for Myanmar customs document bundles.
Classify each page as DECL (CUSDEC-1 or MACCS Release Order — the form with
boxes 1-44, declaration number, importer/consignor) or ATTACHMENT
(Bill of Lading, Commercial Invoice, Packing List, Certificate of Origin,
Truck Receipt, etc).
Return strict JSON only: {"pages": [{"page": 1, "label": "DECL"}, ...]}"""


def _render_thumbnails(pdf_path: str, dpi: int = 60) -> List[str]:
    """Render every page as a small JPEG thumbnail (base64)."""
    out = []
    doc = fitz.open(pdf_path)
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 800
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=70, optimize=True)
        out.append(base64.b64encode(buf.getvalue()).decode())
    doc.close()
    return out


def _grep_decl_pages(pdf_path: str) -> List[int]:
    """Always-keep pages with text-extractable declaration markers."""
    keywords = ("DECLARATION NO", "RELEASE ORDER", "CUSDEC", "BOX 11",
                "TOTAL CUSTOMS VALUE", "TOTAL ITEMS")
    forced = []
    doc = fitz.open(pdf_path)
    for i, page in enumerate(doc, 1):
        t = (page.get_text() or "").upper()
        if any(kw in t for kw in keywords):
            forced.append(i)
    doc.close()
    return forced


def _build_agent() -> Agent:
    return Agent(
        name="PageFilter",
        model=OpenRouter(id=FILTER_MODEL, api_key=OPENROUTER_API_KEY),
        instructions=FILTER_SYSTEM,
        markdown=False,
    )


def filter_pages(pdf_path: str) -> Tuple[List[int], Dict]:
    """Return (kept_page_indices_1_based, metadata).

    Strategy:
      1. Render all page thumbnails.
      2. If <=3 pages, keep all.
      3. Send thumbnails to haiku, get DECL/ATTACHMENT labels.
      4. Force-keep any page whose text contains declaration markers.
      5. Always include page 1 as safety net.
    """
    forced = _grep_decl_pages(pdf_path)
    thumbs = _render_thumbnails(pdf_path)
    n = len(thumbs)
    if n <= 3:
        return list(range(1, n + 1)), {"reason": "short doc, keep all", "n_pages": n}

    parts = []
    for i, b64 in enumerate(thumbs, 1):
        parts.append({"type": "text", "text": f"Page {i}:"})
        parts.append({"type": "image_url",
                       "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    parts.append({"type": "text",
                   "text": ("Classify each page. Return JSON: "
                            '{"pages": [{"page": int, "label": "DECL|ATTACHMENT"}]}')})

    agent = _build_agent()
    try:
        resp = agent.run([{"role": "user", "content": parts}])
        raw = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        return list(range(1, n + 1)), {"reason": f"filter failed: {e}", "n_pages": n}

    parsed = _parse_json(raw)
    if not parsed or "pages" not in parsed:
        return list(range(1, n + 1)), {"reason": "filter parse fail", "n_pages": n,
                                        "raw_preview": str(raw)[:200]}
    # Always-keep first 4 pages (header repetition + cross-validation source)
    safety = {i for i in (1, 2, 3, 4) if i <= n}
    keep = sorted({p.get("page") for p in parsed["pages"]
                   if p.get("label") == "DECL" and isinstance(p.get("page"), int)}
                  | set(forced) | safety)
    keep = [p for p in keep if 1 <= p <= n]
    if not keep:
        return list(range(1, n + 1)), {"reason": "no DECL detected, keep all", "n_pages": n}
    return keep, {"reason": "filtered", "n_pages": n, "kept": keep,
                   "forced_by_text": forced}


def _parse_json(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    if "{" not in s:
        return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in [s,
                 re.sub(r"(\}\s*)\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
                 re.sub(r",(\s*[\}\]])", r"\1", s)]:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None
