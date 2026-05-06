#!/usr/bin/env python3
"""
Standalone test — read a single CUSDEC-1 PDF using Claude Opus 4.7 vision
the same way I (Claude Opus 4.7) read it manually.

Usage:
  python3 opus_read.py /path/to/file.pdf

Strategy (mirrors how I read D19):
1. Holistic layout map first (all fields seen at once)
2. Pen-stroke literal reading (no context inference)
3. Cross-field arithmetic validation
4. Hunt invoice/decl no in unusual places (e.g. Box 38 description)
5. Multi-source vote (page 2 stamps for decl no)
6. Confidence per field with evidence trail
"""

import base64
import json
import os
import sys
import time
import requests
from pathlib import Path

# Convert PDF → images (uses PyMuPDF, already in this project)
import fitz  # type: ignore


OPUS_MODEL = "anthropic/claude-opus-4.7"
FALLBACK_MODEL = "anthropic/claude-sonnet-4.6"  # if Opus 4.7 not available
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Get API key from env or config
try:
    import config
    API_KEY = config.API_KEY
except (ImportError, AttributeError):
    API_KEY = os.environ.get("OPENROUTER_API_KEY", "")


HOLISTIC_PROMPT = """You are reading a Myanmar Customs CUSDEC-1 import declaration form.

## YOUR TASK
Map EVERY field you can see on the form. For each field, extract:
- box_number (1-63 if labeled)
- field_label (literal printed label like "Consignor (Name & Address)")
- value (read pen-strokes LITERALLY)
- page (1, 2, etc.)
- location (top-left, top-right, middle, bottom, etc.)
- confidence (high | med | low)
- evidence (1 sentence: what makes you confident)

## CRITICAL RULES
1. READ HANDWRITTEN NUMBERS LITERALLY — look at exact pen-strokes.
   - DO NOT infer "must be 58 because nearby numbers suggest it"
   - DO NOT round, complete, or correct
   - "60.8090" is read as 60.8090 even if context might suggest 58.5
   - Decimal points: look for the actual dot stroke

2. HUNT INVOICE NUMBERS IN UNUSUAL PLACES
   - Box 19 = B/L No (NOT invoice — common confusion)
   - Box 38 "Description of Goods" often has handwritten "Invoice No: AM-PD-XXX/YYYY" inline
   - Box 15 may have permit code (MTGBIL/MWDBIL/MIDBIL/MFDBIL) — that's a license, NOT invoice

3. CROSS-FIELD ARITHMETIC SELF-CHECK
   For each numeric field, validate:
   - Total Customs Value (Box 44) ÷ F.E. Rate (Box 14) ≈ Invoice Price (in foreign currency)
   - Per Unit Value × Quantity ≈ Total Value
   - Duty Rate × Total Value ≈ Customs Duty Amount
   - CT Rate × (CV + Duty) ≈ Commercial Tax Amount
   If math disagrees → mark confidence "low" and report disagreement.

4. MULTI-SOURCE VOTE FOR ID FIELDS
   - Declaration No (Box 11) often appears AGAIN as stamp on Page 2 ("ID No:")
   - Compare both. Use majority. Flag if disagreement.

5. PRE-PRINTED LABELS ARE 100% RELIABLE
   Box numbers, labels like "Customs Department", "Currency Code", etc.
   These anchor your reading.

## EXTRACT THESE TARGET FIELDS (CUSDEC-1 schema)

declaration:
  - declaration_no            (Box 11 Registration No, e.g. MA0241042460)
  - declaration_date          (Box 5 Date of Entry)
  - importer_name             (Box 2 Consignee Name)
  - consignor_name            (Box 1 Consignor Name)
  - invoice_number            (look in Box 38 description, NOT Box 19 B/L)
  - bl_number                 (Box 19)
  - license_permit            (Box 15)
  - currency                  (Box 24)
  - exchange_rate             (Box 14 F.E. Rate)
  - invoice_price_fe          (derived: Box 44 ÷ Box 14, OR explicit FE value)
  - total_customs_value_mmk   (Box 44 Total Value, in Ks)
  - country_origin            (Box 22)
  - country_whence            (Box 21)
  - customs_duty              (Box 46 / Box 59)
  - commercial_tax            (Box 60)
  - other_taxes_fees          (Box 61)
  - total_amount              (Box 62)
  - gross_weight              (Box 32)
  - hs_code                   (Box 39)

items (one row per goods line):
  - item_name                 (Box 38 Description of Goods)
  - quantity                  (Box 42 QTY based on tariff unit)
  - per_unit_cif              (Box 43 Per Unit Value CIF)
  - per_unit_av               (Box 43 Per Unit Value A.V if shown)
  - total_value               (Box 44)
  - duty_rate                 (Box 45 Rate)
  - origin                    (Box 22 — applies to each item too)

## OUTPUT FORMAT (strict JSON)

{
  "declaration": {
    "declaration_no": {"value": "MA0241042460", "confidence": "high", "evidence": "Box 11 + page 2 stamp match"},
    "currency": {"value": "THB", "confidence": "high", "evidence": "Box 24 typed"},
    ...
  },
  "items": [
    {
      "item_name": {"value": "...", "confidence": "high", "evidence": "..."},
      "quantity": {"value": "11259.96 KG", "confidence": "med", "evidence": "Box 42 handwritten — 7 vs 2 ambiguous"},
      ...
    }
  ],
  "arithmetic_checks": [
    {"check": "Box 44 / Box 14", "expected": 684634.93, "got_invoice_price": "...", "pass": true},
    {"check": "qty × per_unit", "expected": "...", "got_total_value": "...", "pass": true}
  ],
  "anomalies": [
    "Invoice no found in Box 38 description, not Box 19 (B/L)",
    "..."
  ]
}

Return ONLY the JSON. No preamble, no markdown fences.
"""


def pdf_to_images_b64(pdf_path: str, dpi: int = 200, jpeg_quality: int = 85) -> list:
    """Convert PDF pages to base64 JPEG strings.
    200 DPI keeps handwriting readable while staying under OpenRouter 5MB/image limit."""
    from PIL import Image
    import io
    doc = fitz.open(pdf_path)
    images = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=mat)
        # Convert to PIL → JPEG (smaller than PNG for photos)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
        jpeg_bytes = buf.getvalue()
        b64 = base64.b64encode(jpeg_bytes).decode()
        images.append(b64)
        print(f"  Page {i+1}: {pix.width}×{pix.height}px ({len(jpeg_bytes)//1024} KB)")
    doc.close()
    return images


def call_opus(images_b64: list, model: str = OPUS_MODEL) -> dict:
    """Send images + holistic prompt to Opus 4.7 via OpenRouter."""
    if not API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    content_parts = []
    for img_b64 in images_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })
    content_parts.append({"type": "text", "text": HOLISTIC_PROMPT})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 8000,
    }

    print(f"\n  Calling {model}...")
    t0 = time.time()
    fallback_used = False
    for attempt in range(3):
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            elapsed = time.time() - t0
            print(f"  HTTP {resp.status_code} in {elapsed:.1f}s")
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    raw = result["choices"][0]["message"]["content"].strip()
                    usage = result.get("usage", {})
                    print(f"  Tokens: in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}")
                    # Strip markdown fences if any
                    cleaned = raw
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("```")[1]
                        if cleaned.startswith("json"):
                            cleaned = cleaned[4:]
                    cleaned = cleaned.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned[:-3].strip()
                    # Find JSON
                    if '{' in cleaned:
                        start = cleaned.index('{')
                        end = cleaned.rindex('}') + 1
                        try:
                            return json.loads(cleaned[start:end])
                        except json.JSONDecodeError as e:
                            print(f"  JSON parse error: {e}")
                            print(f"  Raw output preview: {cleaned[:500]}")
                            return {"raw": cleaned, "parse_error": str(e)}
                    return {"raw": raw, "no_json": True}
                else:
                    err = str(result.get("error", result))[:300]
                    print(f"  Error response: {err}")
                    if not fallback_used:
                        payload["model"] = FALLBACK_MODEL
                        fallback_used = True
                        print(f"  Falling back to {FALLBACK_MODEL}")
            elif resp.status_code == 429:
                print(f"  Rate limit, sleeping {2**(attempt+1)}s")
                time.sleep(2 ** (attempt + 1))
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:300]}")
        except Exception as e:
            print(f"  Exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {"error": "all attempts failed"}


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 opus_read.py /path/to/file.pdf [output.json] [model_id]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/opus_read_result.json"
    model_override = sys.argv[3] if len(sys.argv) > 3 else None
    if model_override:
        global OPUS_MODEL
        OPUS_MODEL = model_override

    if not Path(pdf_path).exists():
        print(f"ERROR: File not found: {pdf_path}")
        sys.exit(1)

    print(f"Reading PDF: {pdf_path}")
    print(f"Model: {OPUS_MODEL}")
    print()
    print("Step 1: PDF → 300 DPI images")
    images = pdf_to_images_b64(pdf_path, dpi=300)
    print(f"  {len(images)} pages")

    print(f"\nStep 2: Holistic vision call w/ {OPUS_MODEL}")
    result = call_opus(images, model=OPUS_MODEL)

    print(f"\nStep 3: Save → {output_path}")
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("RESULT SUMMARY")
    print("=" * 60)

    if "declaration" in result:
        decl = result["declaration"]
        for k, v in decl.items():
            if isinstance(v, dict):
                val = v.get("value")
                conf = v.get("confidence")
                print(f"  {k:25s} = {val} [{conf}]")
            else:
                print(f"  {k:25s} = {v}")

    items = result.get("items", [])
    if items:
        print(f"\n  ITEMS: {len(items)}")
        for i, it in enumerate(items):
            for k, v in it.items():
                val = v.get("value") if isinstance(v, dict) else v
                conf = v.get("confidence") if isinstance(v, dict) else ""
                print(f"    [{i+1}] {k:20s} = {val} [{conf}]")

    arith = result.get("arithmetic_checks", [])
    if arith:
        print(f"\n  ARITHMETIC CHECKS:")
        for chk in arith:
            print(f"    {chk}")

    anom = result.get("anomalies", [])
    if anom:
        print(f"\n  ANOMALIES:")
        for a in anom:
            print(f"    - {a}")

    print(f"\nFull JSON saved → {output_path}")


if __name__ == "__main__":
    main()
