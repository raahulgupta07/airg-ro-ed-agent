"""V9_PRO — Generic field zoom: render page at high DPI and ask Opus to re-read
a specific named field verbatim. Same pattern as v9/agents/decl_zoom.py."""
import base64
import io
import json
import re
import time
from typing import Dict, Optional

import fitz
import requests
from PIL import Image

from v9_pro.config import OPUS, OPENROUTER_API_KEY


API_URL = "https://openrouter.ai/api/v1/chat/completions"


ZOOM_PROMPT_TMPL = """This is page {page} of a Myanmar customs PDF rendered at {dpi} DPI.

Field to re-read: {field_name!r}
Current draft value: {current!r}
Hint about where to look: {hint}

Task: read this specific field LITERALLY from the document, verbatim, character
by character. Do not rely on the draft. Do not infer. Only report what you can
see on the page.

If the field is numeric, preserve commas/decimals exactly as printed.
If you cannot find the field clearly, set confidence="low".

Output STRICT JSON only (no prose, no markdown):
{{
  "value": "<verbatim value as printed>",
  "confidence": "high|med|low",
  "evidence": "<short verbatim quote of surrounding text/label>",
  "matches_current": true/false
}}"""


def _render_page(pdf_path: str, page: int, dpi: int) -> Optional[str]:
    try:
        doc = fitz.open(pdf_path)
        if page < 1 or page > len(doc):
            doc.close()
            return None
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        max_dim = 3200
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        doc.close()
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"[FieldZoom] render error: {e}")
        return None


def _parse_json(raw: str) -> Optional[Dict]:
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
    for cand in [s, re.sub(r",(\s*[\}\]])", r"\1", s)]:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def zoom_field(
    pdf_path: str,
    page: int,
    field_name: str,
    current_value: str,
    dpi: int = 400,
    hint: str = "",
) -> Optional[Dict]:
    """Re-read a specific named field at high DPI using Opus.

    Returns {"value", "confidence", "evidence", "matches_current"} or None."""
    img_b64 = _render_page(pdf_path, page, dpi)
    if not img_b64:
        return None

    prompt = ZOOM_PROMPT_TMPL.format(
        page=page,
        dpi=dpi,
        field_name=field_name,
        current=current_value or "",
        hint=hint or "(no hint provided — find the labeled box for this field)",
    )

    payload = {
        "model": OPUS,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0,
        "max_tokens": 1500,
    }

    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload, timeout=180,
            )
            dt = time.time() - t0
            print(f"[FieldZoom] HTTP {r.status_code} {dt:.1f}s field={field_name} ({OPUS})")
            if r.status_code == 200:
                j = r.json()
                if "choices" in j and j["choices"]:
                    parsed = _parse_json(j["choices"][0]["message"]["content"])
                    if parsed:
                        parsed["_usage"] = j.get("usage", {})
                        return parsed
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"[FieldZoom] body: {r.text[:300]}")
        except Exception as e:
            print(f"[FieldZoom] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None
