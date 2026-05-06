#!/usr/bin/env python3
"""
Holistic Voter — Phase D ensemble.

Runs Gemini 3.1 Pro + Claude Opus 4.7 in parallel on the same PDF pages.
Both vision LLMs return a holistic field map (same prompt as opus_read.py).
Consensus resolver merges the two readings into a final answer.

Fires only on CUSDEC1 + HIGH sanity flags (handwritten docs).
Cost ~$0.40/doc HW. Skipped for clean MACCS docs.
"""

import base64
import io
import json
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


GEMINI_PRO_MODEL = "~google/gemini-pro-latest"
OPUS_MODEL = "anthropic/claude-opus-4.7"
TIEBREAKER_MODEL = "~google/gemini-pro-latest"


HOLISTIC_PROMPT = """You are reading a Myanmar Customs CUSDEC-1 import declaration form.

## YOUR TASK
Map EVERY field. For each field return: value, confidence (high|med|low), evidence (1 short sentence).

## CRITICAL RULES
1. READ HANDWRITTEN NUMBERS LITERALLY — exact pen-strokes. Do NOT infer "must be 58 because nearby suggests it". Do NOT round, complete, or correct.
2. Decimal points: look for the actual dot stroke — "60.8090" stays 60.8090.
3. HUNT INVOICE NUMBERS in Box 38 description (handwritten "Invoice No: AM-PD-XXX/YYYY") — Box 19 is B/L, NOT invoice.
4. CROSS-FIELD ARITHMETIC: validate
   - Total Customs Value (Box 44) ÷ F.E. Rate (Box 14) ≈ Invoice Price in foreign currency
   - Per Unit × Quantity ≈ Total Value
   - Duty Rate × Total Value ≈ Customs Duty
   If math disagrees → mark confidence "low".
5. CURRENCY: must be 3-letter ISO code (THB, USD, EUR, KRW, etc.). Cross-check rate falls in plausible MMK band:
   THB 40-110, USD 1300-5500, EUR 1500-6500, KRW 1.0-5.0, JPY 10-50, CNY 200-900.
   If currency+rate inconsistent → currency probably misread.

## OUTPUT (strict JSON only, no markdown fences)

{
  "declaration": {
    "Declaration No": {"value": "MA0241042460", "confidence": "high", "evidence": "Box 11 + page 2 stamp match"},
    "Declaration Date": {"value": "2024-07-02", "confidence": "high", "evidence": "..."},
    "Importer (Name)": {"value": "...", "confidence": "high", "evidence": "..."},
    "Consignor (Name)": {"value": "...", "confidence": "high", "evidence": "..."},
    "Invoice Number": {"value": "AM-PD-018/2024", "confidence": "high", "evidence": "Box 38 inline"},
    "Currency": {"value": "THB", "confidence": "high", "evidence": "Box 24"},
    "Exchange Rate": {"value": 57.15875, "confidence": "high", "evidence": "Box 14"},
    "Invoice Price": {"value": 684635.11, "confidence": "high", "evidence": "derived from Box 44/14"},
    "Total Customs Value": {"value": 39132883, "confidence": "high", "evidence": "Box 44"},
    "Country Origin": {"value": "KR", "confidence": "high", "evidence": "Box 22"},
    "Import/Export Customs Duty": {"value": 5869932, "confidence": "high", "evidence": "Box 46"},
    "Commercial Tax (CT)": {"value": 2250141, "confidence": "high", "evidence": "Box 60"}
  },
  "items": [
    {
      "Item name": {"value": "...", "confidence": "high", "evidence": "..."},
      "Quantity (1)": {"value": "11259.96 KG", "confidence": "high", "evidence": "..."},
      "Invoice unit price": {"value": 60.8090, "confidence": "high", "evidence": "Box 43 CIF"},
      "CIF unit price": {"value": 60.8090, "confidence": "high", "evidence": "Box 43"},
      "HS Code": {"value": "2106.90.30 00", "confidence": "high", "evidence": "Box 39"},
      "Origin Country": {"value": "KR", "confidence": "high", "evidence": "Box 22"}
    }
  ],
  "anomalies": ["..."]
}
"""


def _build_payload(images_b64: List[str], model: str) -> Dict:
    content_parts = []
    for img_b64 in images_b64:
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })
    content_parts.append({"type": "text", "text": HOLISTIC_PROMPT})
    return {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 4000,
    }


def _pages_to_jpeg_b64(pages: List[Dict], dpi: int = 200, jpeg_quality: int = 85) -> List[str]:
    """Re-encode page images as JPEG (smaller than PNG, fits OpenRouter 5MB/image limit).
    Pages already have image_b64 (PNG, 300 DPI from splitter)."""
    from PIL import Image
    out = []
    for p in pages[:3]:  # 3 pages max for cost control
        img_b64 = p.get("image_b64", "")
        if not img_b64:
            continue
        try:
            png_bytes = base64.b64decode(img_b64)
            img = Image.open(io.BytesIO(png_bytes))
            # Downsample if too big (200 DPI equivalent ≈ 1700x2400 for letter)
            max_dim = 2400
            if img.width > max_dim or img.height > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            out.append(base64.b64encode(buf.getvalue()).decode())
        except Exception as e:
            print(f"    Voter: page conversion error: {e}")
    return out


def _call_model(model: str, images_b64: List[str], label: str) -> Dict:
    """One vision call, returns parsed JSON or {} on failure."""
    payload = _build_payload(images_b64, model)
    for attempt in range(3):
        try:
            t0 = time.time()
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=180,
            )
            elapsed = time.time() - t0
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record(f"holistic_voter_{label}", result, model)
                    raw = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
                    if '{' in cleaned:
                        json_text = cleaned[cleaned.index('{'):cleaned.rindex('}') + 1]
                        try:
                            parsed = json.loads(json_text)
                            print(f"    Voter [{label}]: OK in {elapsed:.1f}s ({model})")
                            return parsed
                        except json.JSONDecodeError as e:
                            # Repair common Gemini issues: missing commas, trailing commas, etc.
                            r1 = re.sub(r'(\}\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', json_text)
                            r1 = re.sub(r'(["\d\}\]]\s*)\n(\s*"[^"]+"\s*:)', r'\1,\n\2', r1)
                            r1 = re.sub(r',(\s*[\}\]])', r'\1', r1)  # trailing comma
                            for attempt_text in [r1, json_text.replace(',\n}', '\n}').replace(',\n]', '\n]')]:
                                try:
                                    parsed = json.loads(attempt_text)
                                    print(f"    Voter [{label}]: OK after JSON repair in {elapsed:.1f}s")
                                    return parsed
                                except json.JSONDecodeError:
                                    continue
                            # Try json5-style if available, else give up with debug
                            try:
                                # Use ast.literal_eval as last resort for simple cases
                                pass
                            except Exception:
                                pass
                            print(f"    Voter [{label}] JSON parse fail: {e}")
                            print(f"    Voter [{label}] raw preview: {json_text[max(0, e.pos-50):e.pos+50]}")
                else:
                    err = str(result.get("error", result))[:200]
                    print(f"    Voter [{label}] error: {err}")
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"    Voter [{label}] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"    Voter [{label}] exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {}


def run_ensemble(pages: List[Dict]) -> Dict:
    """Run Gemini 3.1 Pro + Opus 4.7 in parallel. Returns {gemini, opus, ensemble_meta}."""
    images_b64 = _pages_to_jpeg_b64(pages)
    if not images_b64:
        return {"gemini": {}, "opus": {}, "error": "no_images"}

    print(f"    Holistic voter: {len(images_b64)} pages → Gemini 3.1 Pro + Opus 4.7 (parallel)")

    results = {"gemini": {}, "opus": {}}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(_call_model, GEMINI_PRO_MODEL, images_b64, "gemini"): "gemini",
            ex.submit(_call_model, OPUS_MODEL, images_b64, "opus"): "opus",
        }
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as e:
                print(f"    Voter [{label}] thread exception: {e}")
                results[label] = {}

    return results
