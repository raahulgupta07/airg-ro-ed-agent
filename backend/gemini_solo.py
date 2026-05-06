#!/usr/bin/env python3
"""
Standalone test — single-model pipeline using google/gemini-pro-latest.
Sends NATIVE PDF (not rasterized images) to test if PDF input + latest Gemini Pro
matches our ensemble accuracy at lower cost.

Usage:
  python3 gemini_solo.py /path/to/file.pdf [output.json] [model_id]
"""

import base64
import json
import os
import sys
import time
import requests
from pathlib import Path

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-pro-latest"

try:
    import config
    API_KEY = config.API_KEY
except (ImportError, AttributeError):
    API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


HOLISTIC_PROMPT = """You are reading a Myanmar Customs CUSDEC-1 / MACCS Release Order import declaration form.

## TASK
Map EVERY field on the form. For each field return: value, confidence (high|med|low), evidence.

## CRITICAL RULES
1. Read HANDWRITTEN numbers LITERALLY — exact pen-strokes. Do NOT infer from context.
2. Decimal points: look for the actual dot stroke. "60.8090" stays 60.8090 — do NOT collapse to 58.5.
3. HUNT INVOICE NUMBERS in Box 38 description (handwritten "Invoice No: AM-PD-XXX/YYYY") — Box 19 is B/L, NOT invoice.
4. CROSS-FIELD ARITHMETIC: validate
   - Total Customs Value (Box 44) ÷ F.E. Rate (Box 14) ≈ Invoice Price (foreign currency)
   - Per Unit × Quantity ≈ Total Value
   - Duty Rate × Total Value ≈ Customs Duty
5. CURRENCY: 3-letter ISO. Cross-check rate band: THB 40-110, USD 1300-5500, KRW 1.0-5.0, JPY 10-50, CNY 200-900, EUR 1500-6500.

## OUTPUT (strict JSON only, NO markdown fences)

{
  "declaration": {
    "Declaration No": {"value": "MA0241042460", "confidence": "high", "evidence": "Box 11"},
    "Declaration Date": {"value": "2024-07-02", "confidence": "high", "evidence": "Box 5"},
    "Importer (Name)": {"value": "...", "confidence": "high", "evidence": "Box 2"},
    "Consignor (Name)": {"value": "...", "confidence": "high", "evidence": "Box 1"},
    "Invoice Number": {"value": "AM-PD-018/2024", "confidence": "high", "evidence": "Box 38 inline"},
    "Currency": {"value": "THB", "confidence": "high", "evidence": "Box 24"},
    "Exchange Rate": {"value": 57.15875, "confidence": "high", "evidence": "Box 14"},
    "Invoice Price": {"value": 684635.11, "confidence": "high", "evidence": "derived Box 44/14"},
    "Total Customs Value": {"value": 39132883, "confidence": "high", "evidence": "Box 44"},
    "Country Origin": {"value": "KR", "confidence": "high", "evidence": "Box 22"},
    "Import/Export Customs Duty": {"value": 5869932, "confidence": "high", "evidence": "Box 46"},
    "Commercial Tax (CT)": {"value": 2250141, "confidence": "high", "evidence": "Box 60"},
    "MACCS Service Fee (MF)": {"value": 782658, "confidence": "high", "evidence": "Box 61 / 2% AV"}
  },
  "items": [
    {
      "Item name": {"value": "...", "confidence": "high", "evidence": "Box 38"},
      "Quantity (1)": {"value": "11259.96 KG", "confidence": "high", "evidence": "Box 42"},
      "Invoice unit price": {"value": 60.8090, "confidence": "high", "evidence": "Box 43 CIF"},
      "CIF unit price": {"value": 60.8090, "confidence": "high", "evidence": "Box 43"},
      "HS Code": {"value": "2106.90.30 00", "confidence": "high", "evidence": "Box 39"},
      "Origin Country": {"value": "KR", "confidence": "high", "evidence": "Box 22"}
    }
  ],
  "anomalies": ["..."]
}
"""


def call_with_pdf(pdf_path: str, model: str) -> dict:
    """Try native PDF upload first (Gemini supports it via OpenRouter)."""
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode()
    print(f"  PDF size: {len(pdf_bytes)//1024} KB, b64 {len(pdf_b64)//1024} KB")

    content_parts = [
        {
            "type": "file",
            "file": {
                "filename": Path(pdf_path).name,
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
            },
        },
        {"type": "text", "text": HOLISTIC_PROMPT},
    ]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 16000,
    }

    print(f"  Calling {model} (native PDF)...")
    t0 = time.time()
    for attempt in range(3):
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=180,
            )
            elapsed = time.time() - t0
            print(f"  HTTP {resp.status_code} in {elapsed:.1f}s")
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    raw = result["choices"][0]["message"]["content"].strip()
                    usage = result.get("usage", {})
                    print(f"  Tokens: in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}")
                    cost = result.get("total_cost", 0)
                    if cost == 0 and usage.get("prompt_tokens"):
                        # Estimate
                        cost = usage["prompt_tokens"] * 2 / 1e6 + usage.get("completion_tokens", 0) * 12 / 1e6
                    print(f"  Cost: ${cost:.4f}")
                    return _parse_response(raw)
                err = str(result.get("error", result))[:300]
                print(f"  Error: {err}")
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"  HTTP body: {resp.text[:300]}")
        except Exception as e:
            print(f"  Exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {"error": "all attempts failed"}


def _parse_response(raw: str) -> dict:
    import re
    cleaned = raw
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    if '{' in cleaned:
        json_text = cleaned[cleaned.index('{'):cleaned.rindex('}') + 1]
        for attempt in [json_text,
                        re.sub(r'(\}\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text),
                        re.sub(r'([\d"\}\]])\s*\n(\s*"[^"]+"\s*:)', r'\1,\n\2',
                               re.sub(r'(\}\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text))]:
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
        print(f"  JSON parse failed. Raw preview: {cleaned[:300]}")
        return {"raw": raw, "parse_error": True}
    return {"raw": raw}


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gemini_solo.py /path/to/file.pdf [output.json] [model_id]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/gemini_solo_result.json"
    model = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_MODEL

    if not Path(pdf_path).exists():
        print(f"ERROR: {pdf_path} not found")
        sys.exit(1)

    print(f"Reading PDF: {pdf_path}")
    print(f"Model: {model}")
    print()

    result = call_with_pdf(pdf_path, model)

    print(f"\nSave → {output_path}")
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)

    if "declaration" in result:
        for k, v in result["declaration"].items():
            if isinstance(v, dict):
                val = v.get("value")
                conf = v.get("confidence")
                print(f"  {k:30s} = {val} [{conf}]")
            else:
                print(f"  {k:30s} = {v}")

    if "items" in result:
        for i, it in enumerate(result["items"]):
            print(f"\n  ITEM {i+1}:")
            for k, v in it.items():
                val = v.get("value") if isinstance(v, dict) else v
                conf = v.get("confidence") if isinstance(v, dict) else ""
                print(f"    {k:25s} = {val} [{conf}]")

    if "anomalies" in result and result["anomalies"]:
        print("\n  ANOMALIES:")
        for a in result["anomalies"]:
            print(f"    - {a}")

    print(f"\nFull JSON → {output_path}")


if __name__ == "__main__":
    main()
