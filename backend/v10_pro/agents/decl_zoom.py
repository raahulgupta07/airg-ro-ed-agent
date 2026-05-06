"""V9 — Critical-field zoom: re-read declaration number at 400 DPI with Opus.
Catches single-digit misreads that the holistic reader misses."""
import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Dict, Optional

import fitz
import requests
from PIL import Image

from v10_pro.config import OPUS, OPENROUTER_API_KEY


API_URL = "https://openrouter.ai/api/v1/chat/completions"


ZOOM_PROMPT_TMPL = """This is page 1 of a Myanmar customs PDF rendered at 400 DPI.

The current draft extraction reads: Declaration No = {current!r}

Look at Box 11 (top-right area, labeled "Declaration No" or "Reg. No"). Read the
declaration number LITERALLY, digit by digit. Myanmar declaration numbers are
typically 11-12 digits.

Common misread pairs to watch for:
- 0 vs 8, 1 vs 7, 3 vs 8, 5 vs 6, 9 vs 0

Output strict JSON only:
{{
  "value": "<full digit string>",
  "digits": ["1","0","0","2","9","6", ...],
  "confidence": "high|med|low",
  "matches_draft": true/false,
  "evidence": "what you saw in Box 11"
}}"""


def _render_page_400(pdf_path: str, page: int = 1) -> Optional[str]:
    try:
        doc = fitz.open(pdf_path)
        if page > len(doc):
            doc.close(); return None
        pix = doc[page - 1].get_pixmap(dpi=400)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 3200
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        doc.close()
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[DeclZoom] render error: {e}")
        return None


def _parse_json(raw: str) -> Optional[Dict]:
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


def zoom_declaration_no(pdf_path: str, current_decl_no: str) -> Optional[Dict]:
    """Re-read declaration number at 400 DPI using Opus.
    Returns {"value", "digits", "confidence", "matches_draft", "evidence"} or None."""
    img_b64 = _render_page_400(pdf_path, 1)
    if not img_b64:
        return None
    payload = {
        "model": OPUS,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": ZOOM_PROMPT_TMPL.format(current=current_decl_no or "")},
        ]}],
        "temperature": 0,
        "max_tokens": 1500,
    }
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=180)
            dt = time.time() - t0
            print(f"[DeclZoom] HTTP {r.status_code} {dt:.1f}s ({OPUS})")
            if r.status_code == 200:
                j = r.json()
                try:
                    from v10_pro._cost_tracker import record_cost
                    record_cost(label="decl_zoom", model=OPUS, usage=j.get("usage", {}))
                except Exception:
                    pass
                if "choices" in j and j["choices"]:
                    parsed = _parse_json(j["choices"][0]["message"]["content"])
                    if parsed:
                        parsed["_usage"] = j.get("usage", {})
                        return parsed
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                print(f"[DeclZoom] body: {r.text[:300]}")
        except Exception as e:
            print(f"[DeclZoom] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def maybe_override(declaration: Dict, zoom_result: Optional[Dict]) -> tuple:
    """If zoom is confident and disagrees with draft, override.
    Returns (declaration, override_note_or_None)."""
    if not zoom_result:
        return declaration, None
    val = zoom_result.get("value")
    conf = (zoom_result.get("confidence") or "").lower()
    matches = zoom_result.get("matches_draft")
    current = declaration.get("declaration_no")
    if val and conf in ("high", "med") and matches is False and val != current:
        declaration["declaration_no"] = val
        return declaration, f"decl_no zoom override: '{current}' → '{val}' ({conf})"
    return declaration, None
