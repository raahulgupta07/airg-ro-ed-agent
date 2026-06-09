"""Gate-triggered self-correction — fix only the broken field, no slow fallback.

When the math gates (v11.tools.reconcile) fail, they localize the error to a
small set of header fields. Instead of re-running a heavy 20-stage engine, this
re-reads ONLY those fields with one targeted high-DPI vision call, then keeps the
reading that satisfies the broken equation. Engine-agnostic: Presto and Scribe
both call it. Returns a corrected declaration + a log; never raises.

Currently handles the HEADER closure (invoice_price × exchange_rate ≈ total).
That equation pins exchange_rate / invoice_price / total — the fields most often
misread (e.g. a THB-vs-USD rate mixup). Item-sum gaps keep their existing
page-recovery path; this complements it.
"""
import base64
import io
import json
import re
import time
from typing import Dict, List, Optional

import fitz
import requests

import config
from v11.tools import reconcile as _rc

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Fields the CIF closure pins. Re-read just these.
_HEADER_FIELDS = ["exchange_rate", "invoice_price", "total_customs_value", "customs_duty"]

_PROMPT = """Read ONLY these values from this Myanmar customs declaration header
and return strict JSON (numbers only, strip separators):
{"exchange_rate": <invoice-currency→MMK rate, e.g. 57.3984>,
 "invoice_price": <total invoice price in invoice currency>,
 "total_customs_value": <total customs value in MMK>,
 "customs_duty": <total customs duty in MMK>}
Note: invoice_price × exchange_rate should ≈ total_customs_value. The form may
show several rates (e.g. a THB rate ~57 and a USD rate ~2100) — pick the one that
makes that equation hold. Return ONLY the JSON object."""


def _render_page(pdf_path: str, page_no: int, dpi: int = 400) -> Optional[str]:
    try:
        doc = fitz.open(str(pdf_path))
        idx = max(0, min(page_no - 1, len(doc) - 1))
        pix = doc[idx].get_pixmap(dpi=dpi)
        from PIL import Image
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if max(img.size) > 2400:
            img.thumbnail((2400, 2400), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        doc.close()
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _parse_json(raw: str):
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip().rstrip("`").strip()
    if "{" not in s:
        return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in (s, re.sub(r",(\s*[\}\]])", r"\1", s)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _reread_header(pdf_path: str, page_no: int, model: str) -> Dict:
    b64 = _render_page(pdf_path, page_no)
    if not b64:
        return {}
    parts = [{"type": "text", "text": _PROMPT},
             {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    payload = {"model": model, "messages": [{"role": "user", "content": parts}],
               "temperature": 0, "max_tokens": 500}
    for attempt in range(2):
        try:
            r = requests.post(API_URL, headers={
                "Authorization": f"Bearer {config.API_KEY}",
                "Content-Type": "application/json"}, json=payload, timeout=90)
            if r.status_code == 200:
                return _parse_json(r.json()["choices"][0]["message"]["content"]) or {}
        except Exception:
            pass
        time.sleep(1.5)
    return {}


def correct(pdf_path: str, declaration: Dict, items: List[Dict], verdict: Dict,
            header_page: int = 1, model: Optional[str] = None) -> Dict:
    """Targeted correction driven by the gate verdict.

    Returns {"declaration": <maybe-updated>, "verdict": <re-checked>,
             "corrected": bool, "log": [...]}. Only touches header fields when the
    CIF closure failed; otherwise returns the input unchanged.
    """
    log = []
    # Only act when the gate localized a HEADER (CIF) failure.
    if verdict.get("balanced") or not (verdict.get("cif_checked") and not verdict.get("cif_ok")):
        return {"declaration": declaration, "verdict": verdict, "corrected": False, "log": log}

    model = model or config.EXTRACTION_MODEL
    log.append(f"CIF gap {verdict.get('cif_gap_pct')}% → targeted header re-read (p{header_page})")
    fresh = _reread_header(pdf_path, header_page, model)
    if not fresh:
        log.append("re-read returned nothing")
        return {"declaration": declaration, "verdict": verdict, "corrected": False, "log": log}

    # Build a candidate declaration with the re-read header fields.
    cand = dict(declaration or {})
    for f in _HEADER_FIELDS:
        v = _rc._to_float(fresh.get(f))
        if v is not None:
            cand[f] = v
    new_verdict = _rc.reconcile(cand, items)

    # Keep the correction only if it actually closes the books better.
    if new_verdict.get("balanced") or (
            new_verdict.get("cif_gap_pct", 1e9) < verdict.get("cif_gap_pct", 1e9)):
        changed = {f: (declaration.get(f), cand.get(f)) for f in _HEADER_FIELDS
                   if _rc._to_float(declaration.get(f)) != _rc._to_float(cand.get(f))}
        log.append(f"corrected {list(changed.keys())}; cif_gap "
                   f"{verdict.get('cif_gap_pct')}%→{new_verdict.get('cif_gap_pct')}%")
        return {"declaration": cand, "verdict": new_verdict, "corrected": True,
                "log": log, "changed": changed}

    log.append("re-read did not improve closure — leaving for human review")
    return {"declaration": declaration, "verdict": verdict, "corrected": False, "log": log}
