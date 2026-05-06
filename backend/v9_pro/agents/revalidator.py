"""V9_PRO — Strict re-validator: independent re-read of PDF that does NOT trust the draft.
Uses Opus to verify each critical declaration field against the document directly."""
import base64
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from v9_pro.config import OPUS, OPENROUTER_API_KEY


API_URL = "https://openrouter.ai/api/v1/chat/completions"


CRITICAL_FIELDS = [
    "declaration_no", "declaration_date",
    "importer_name", "consignor_name",
    "invoice_number", "invoice_number_customs", "invoice_number_commercial",
    "currency", "currency_2", "exchange_rate",
    "invoice_price", "total_customs_value",
    "customs_duty", "commercial_tax",
    "advance_income_tax", "security_fee", "maccs_service_fee", "exemption",
]


SYSTEM_PROMPT = """You are a strict, evidence-based customs document re-validator.

Your job: independently re-read the attached Myanmar customs PDF and verify each
field the draft extraction reports. You must NOT trust the draft. Read the
document yourself, then for each field decide whether the draft matches what you
actually see.

Rules:
- Always quote a short verbatim snippet from the document as evidence.
- If you cannot find the field clearly, set confidence="low" and needs_zoom=true.
- Numbers must be reported exactly as written (preserve commas/decimals).
- Never guess. Never fill from training knowledge. Only what is on the page.
- Output STRICT JSON only — no prose, no markdown."""


USER_PROMPT_TMPL = """Re-validate the following draft fields against the attached PDF.

For EACH field below, return an object with:
  - agree: true/false (does the draft value match what you see?)
  - corrected_value: the value you actually see in the document (string)
  - evidence: short verbatim quote from the document
  - confidence: "high" | "med" | "low"
  - needs_zoom: true if the region is unclear and needs higher-DPI re-read

Draft fields to verify:
{fields_block}

Output STRICT JSON ONLY in this exact shape:
{{
  "validations": {{
    "<field_name>": {{
      "agree": true,
      "corrected_value": "...",
      "evidence": "...",
      "confidence": "high",
      "needs_zoom": false
    }}
  }},
  "overall_confidence": "high|med|low",
  "any_disagreement": true
}}

Include EVERY field listed above as a key in "validations" (even if not found —
mark agree=false, confidence=low, needs_zoom=true)."""


def _encode_pdf(pdf_path: str) -> Optional[str]:
    try:
        data = Path(pdf_path).read_bytes()
        return base64.b64encode(data).decode()
    except Exception as e:
        print(f"[Revalidator] pdf read error: {e}")
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


def _build_fields_block(draft: Dict) -> str:
    lines = []
    for f in CRITICAL_FIELDS:
        v = draft.get(f, None)
        if v is None:
            v_str = "<missing>"
        else:
            v_str = str(v)
        lines.append(f"  - {f}: {v_str!r}")
    return "\n".join(lines)


def revalidate(pdf_path: str, draft_declaration: dict) -> Optional[dict]:
    """Strict independent re-read using Opus.

    Returns:
      {
        "validations": {field: {"agree","corrected_value","evidence","confidence","needs_zoom"}},
        "overall_confidence": "high|med|low",
        "any_disagreement": bool,
        "_usage": {...}
      }
    or None on error."""
    pdf_b64 = _encode_pdf(pdf_path)
    if not pdf_b64:
        return None

    fields_block = _build_fields_block(draft_declaration or {})
    user_text = USER_PROMPT_TMPL.format(fields_block=fields_block)

    payload = {
        "model": OPUS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "file", "file": {
                    "filename": Path(pdf_path).name,
                    "file_data": f"data:application/pdf;base64,{pdf_b64}",
                }},
                {"type": "text", "text": user_text},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 6000,
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
                json=payload, timeout=300,
            )
            dt = time.time() - t0
            print(f"[Revalidator] HTTP {r.status_code} {dt:.1f}s ({OPUS})")
            if r.status_code == 200:
                j = r.json()
                if "choices" in j and j["choices"]:
                    parsed = _parse_json(j["choices"][0]["message"]["content"])
                    if parsed:
                        parsed["_usage"] = j.get("usage", {})
                        # normalize any_disagreement if missing
                        if "any_disagreement" not in parsed:
                            vals = parsed.get("validations", {}) or {}
                            parsed["any_disagreement"] = any(
                                v.get("agree") is False for v in vals.values()
                                if isinstance(v, dict)
                            )
                        return parsed
                    print("[Revalidator] JSON parse failed")
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"[Revalidator] body: {r.text[:300]}")
        except Exception as e:
            print(f"[Revalidator] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def apply_revalidator_corrections(
    declaration: dict,
    revalidator_out: Optional[dict],
) -> Tuple[dict, List[str]]:
    """Apply only high/med-confidence disagreements. Returns (updated_decl, notes)."""
    notes: List[str] = []
    if not revalidator_out or not isinstance(revalidator_out, dict):
        return declaration, notes
    validations = revalidator_out.get("validations") or {}
    if not isinstance(validations, dict):
        return declaration, notes

    for field, v in validations.items():
        if not isinstance(v, dict):
            continue
        if field not in CRITICAL_FIELDS:
            continue
        agree = v.get("agree")
        conf = (v.get("confidence") or "").lower()
        corrected = v.get("corrected_value")
        if agree is False and conf in ("high", "med") and corrected not in (None, "", "<missing>"):
            old = declaration.get(field)
            if str(old) != str(corrected):
                declaration[field] = corrected
                notes.append(
                    f"revalidator: {field} '{old}' -> '{corrected}' ({conf})"
                )
    return declaration, notes
