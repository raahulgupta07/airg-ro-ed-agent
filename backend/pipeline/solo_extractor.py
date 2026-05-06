#!/usr/bin/env python3
"""
Solo Extractor — single-call PDF extraction using ~google/gemini-pro-latest.
Fires on CUSDEC1 (handwritten) docs. Native PDF input. ~9x cheaper, ~25x faster
than V7 ensemble. Validated 100% w/ post-fixes on D11/D19/D18/D12.

Usage in pipeline:
  result = solo_extract(pdf_path)
  if result and result.get("declaration"):
      # apply post-fixes (seed canonical, origin baseline, MACCS deterministic)
      ...
"""

import base64
import json
import re
import time
import requests
from pathlib import Path
from typing import Dict, Optional

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


SOLO_MODEL = "~google/gemini-pro-latest"
SOLO_FALLBACK = "~google/gemini-pro-latest"

API_URL = "https://openrouter.ai/api/v1/chat/completions"


SOLO_PROMPT = """You are reading a Myanmar Customs CUSDEC-1 / MACCS Release Order import declaration form.

## TASK
Extract EVERY field. For each return: value, confidence (high|med|low), evidence.

## CRITICAL RULES
1. Read HANDWRITTEN numbers LITERALLY — exact pen-strokes. Do NOT infer.
2. Decimal points: actual dot stroke. "60.8090" stays 60.8090.
3. HUNT INVOICE NUMBERS in Box 38 description ("Invoice No: AM-PD-XXX/YYYY") — Box 19 is B/L, NOT invoice.
4. CROSS-FIELD ARITHMETIC: validate
   - Total Customs Value (Box 44) ÷ F.E. Rate (Box 14) ≈ Invoice Price (foreign currency)
   - Per Unit × Quantity ≈ Total Value
   - Duty 15% × Total Value ≈ Customs Duty
   - CT 5% × (CV + Duty) ≈ Commercial Tax
   - MACCS 2% × CV ≈ MACCS Fee
5. CURRENCY: 3-letter ISO. Band check: THB 40-110, USD 1300-5500, KRW 1.0-5.0, JPY 10-50, CNY 200-900, EUR 1500-6500.
6. ORIGIN: check Box 22 AND Box 42 (origin code prefix to qty) AND truck receipt page if present. Cross-vote.

## OUTPUT (strict JSON only, no markdown fences)

{
  "declaration": {
    "Declaration No": {"value": "...", "confidence": "...", "evidence": "..."},
    "Declaration Date": {"value": "YYYY-MM-DD", "confidence": "...", "evidence": "..."},
    "Importer (Name)": {"value": "...", "confidence": "...", "evidence": "..."},
    "Consignor (Name)": {"value": "...", "confidence": "...", "evidence": "..."},
    "Invoice Number": {"value": "...", "confidence": "...", "evidence": "..."},
    "Invoice Number (Customs Declaration)": {"value": null, "confidence": "...", "evidence": "..."},
    "Invoice Number (Commercial Invoice)": {"value": "...", "confidence": "...", "evidence": "..."},
    "Currency": {"value": "...", "confidence": "...", "evidence": "..."},
    "Currency 2": {"value": null, "confidence": "...", "evidence": "..."},
    "Exchange Rate": {"value": 0, "confidence": "...", "evidence": "..."},
    "Invoice Price": {"value": 0, "confidence": "...", "evidence": "..."},
    "Total Customs Value": {"value": 0, "confidence": "...", "evidence": "..."},
    "Country Origin": {"value": "...", "confidence": "...", "evidence": "..."},
    "Import/Export Customs Duty": {"value": 0, "confidence": "...", "evidence": "..."},
    "Commercial Tax (CT)": {"value": 0, "confidence": "...", "evidence": "..."},
    "Advance Income Tax (AT)": {"value": 0, "confidence": "...", "evidence": "..."},
    "Security Fee (SF)": {"value": 0, "confidence": "...", "evidence": "..."},
    "MACCS Service Fee (MF)": {"value": 0, "confidence": "...", "evidence": "..."},
    "Exemption/Reduction": {"value": 0, "confidence": "...", "evidence": "..."}
  },
  "items": [
    {
      "Item name": {"value": "...", "confidence": "...", "evidence": "..."},
      "Customs duty rate": {"value": 0.15, "confidence": "...", "evidence": "..."},
      "Quantity (1)": {"value": "1234.56 KG", "confidence": "...", "evidence": "..."},
      "Invoice unit price": {"value": 0, "confidence": "...", "evidence": "..."},
      "CIF unit price": {"value": 0, "confidence": "...", "evidence": "..."},
      "Currency": {"value": "...", "confidence": "...", "evidence": "..."},
      "Commercial tax %": {"value": 0.05, "confidence": "...", "evidence": "..."},
      "Exchange Rate (1)": {"value": 0, "confidence": "...", "evidence": "..."},
      "HS Code": {"value": "...", "confidence": "...", "evidence": "..."},
      "Origin Country": {"value": "...", "confidence": "...", "evidence": "..."},
      "Customs Value (MMK)": {"value": 0, "confidence": "...", "evidence": "..."}
    }
  ],
  "anomalies": ["..."],
  "document_format": "CUSDEC1"
}

For unfilled fields use {"value": null, "confidence": "low", "evidence": "not present on form"}.
Always include all declaration field keys. Items array can be 1-N items.
"""


def _parse_response(raw: str) -> Optional[Dict]:
    """Parse JSON from LLM response, with repair attempts."""
    if not raw:
        return None
    cleaned = raw.strip()
    # Strip markdown fences
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        if len(parts) >= 2:
            cleaned = parts[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    if '{' not in cleaned:
        return None
    json_text = cleaned[cleaned.index('{'):cleaned.rindex('}') + 1]

    # Try parse + repair attempts
    for attempt in [
        json_text,
        re.sub(r'(\}\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text),
        re.sub(r'([\d"\}\]])\s*\n(\s*"[^"]+"\s*:)', r'\1,\n\2',
               re.sub(r'(\}\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text)),
        re.sub(r',(\s*[\}\]])', r'\1',
               re.sub(r'([\d"\}\]])\s*\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text)),
    ]:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    return None


def _flatten_voter_result(parsed: Dict) -> Dict:
    """Convert voter-format {value,confidence,evidence} to flat dict expected by pipeline."""
    out_decl = {}
    for k, v in (parsed.get("declaration") or {}).items():
        if isinstance(v, dict) and "value" in v:
            out_decl[k] = v.get("value")
        else:
            out_decl[k] = v

    out_items = []
    for item in (parsed.get("items") or []):
        flat = {}
        for k, v in item.items():
            if isinstance(v, dict) and "value" in v:
                flat[k] = v.get("value")
            else:
                flat[k] = v
        out_items.append(flat)

    return {
        "declaration": out_decl,
        "items": out_items,
        "anomalies": parsed.get("anomalies", []),
        "document_format": parsed.get("document_format"),
        "raw": parsed,
    }


def solo_extract(pdf_path: str, model: str = SOLO_MODEL, timeout: int = 180) -> Optional[Dict]:
    """Single-call extraction with native PDF input. Returns flat dict matching pipeline schema.
    Returns None on failure — caller should fall back to legacy ensemble."""

    try:
        pdf_bytes = Path(pdf_path).read_bytes()
    except Exception as e:
        print(f"    Solo: PDF read error: {e}")
        return None

    pdf_b64 = base64.b64encode(pdf_bytes).decode()
    print(f"    Solo: PDF {len(pdf_bytes)//1024} KB, b64 {len(pdf_b64)//1024} KB")

    content_parts = [
        {
            "type": "file",
            "file": {
                "filename": Path(pdf_path).name,
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
            },
        },
        {"type": "text", "text": SOLO_PROMPT},
    ]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 16000,
    }

    fallback_used = False
    for attempt in range(3):
        try:
            t0 = time.time()
            print(f"    Solo: calling {payload['model']} (attempt {attempt+1})")
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {config.API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            elapsed = time.time() - t0
            print(f"    Solo: HTTP {resp.status_code} in {elapsed:.1f}s")
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record("solo_extractor", result, payload["model"])
                    raw = result["choices"][0]["message"]["content"].strip()
                    parsed = _parse_response(raw)
                    if parsed:
                        usage = result.get("usage", {})
                        print(f"    Solo: OK in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}")
                        return _flatten_voter_result(parsed)
                    else:
                        print(f"    Solo: JSON parse failed, raw preview: {raw[:200]}")
                else:
                    err = str(result.get("error", result))[:300]
                    print(f"    Solo: error response: {err}")
                    if not fallback_used:
                        payload["model"] = SOLO_FALLBACK
                        fallback_used = True
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"    Solo: HTTP body: {resp.text[:300]}")
                if not fallback_used:
                    payload["model"] = SOLO_FALLBACK
                    fallback_used = True
        except Exception as e:
            print(f"    Solo: exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    return None
