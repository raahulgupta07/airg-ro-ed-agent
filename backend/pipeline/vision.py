#!/usr/bin/env python3
"""
RO-ED: Vision Page Extractor
Send each HD page image to vision model. Get structured JSON back.
No text input — pure image analysis.
"""

import os
import json
import re
import time
import threading
import requests
from typing import Dict, List, Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

# Global semaphore — limits total concurrent API calls across ALL users.
# Prevents OpenRouter rate limiting and OOM with 10+ concurrent users.
# Tune via VISION_MAX_CONCURRENCY (raise with more workers; watch OpenRouter
# account rate limits + worker memory).
_API_SEMAPHORE = threading.Semaphore(int(os.environ.get("VISION_MAX_CONCURRENCY", "24")))

# Cost tracking
try:
    import cost_tracker
except ImportError:
    cost_tracker = None

EXTRACT_PROMPT = """Look at this document page image carefully. Extract ALL information you can see.

Return ONLY valid JSON:
{
  "page_type": "<what type of document — invoice, customs declaration, packing list, bill of lading, import licence, certificate, etc.>",
  "language": "<primary language on this page>",
  "confidence": 0.95,
  "explanation": "<2-3 sentences: what this page is, who issued it, what is important>",
  "document": {
    "title": "<document title as shown>",
    "issuer": "<who created/issued this>",
    "date": "<date on document, YYYY-MM-DD if possible>",
    "reference": "<any ID/reference number>",
    "country": "<issuing country>"
  },
  "fields": {
    "<every label you see>": "<its value — copy EXACTLY as printed>",
    "<read ALL key-value pairs>": "<be thorough, include every field>"
  },
  "tables": [
    {
      "title": "<table title if visible>",
      "headers": ["<column 1>", "<column 2>", "..."],
      "rows": [["<row 1 col 1>", "<row 1 col 2>"], ...]
    }
  ],
  "amounts": [
    {"label": "<what this number is>", "value": 0.0, "currency": "<if shown>"}
  ],
  "entities": {
    "companies": ["<company names found>"],
    "dates": ["<dates found>"],
    "references": ["<reference/ID numbers found>"]
  },
  "visual": {
    "has_logo": false,
    "has_stamp": false,
    "has_signature": false,
    "has_barcode": false,
    "quality": "<good|fair|poor>"
  }
}

RULES:
- Read EVERY piece of text on this page. Be thorough.
- Copy text EXACTLY as printed. Never change abbreviations.
- For tables: copy EXACT column headers and ALL rows.
- Numbers: commas are thousands separators (72,802 = 72802).
- If text is small, zoom in mentally and read carefully.
- Return ONLY valid JSON.

IMPORTANT — Fee/Tax Fields:
If this page has a tax or fee table/section, extract EACH fee as a SEPARATE labeled field:
  "Commercial Tax (CT)": <amount or 0>,
  "Advance Income Tax (AT)": <amount or 0>,
  "Security Fee (SF)": <amount or 0>,
  "MACCS Service Fee (MF)": <amount or 0>,
  "Exemption/Reduction": <amount or 0>
Read each fee from its OWN labeled row. Do NOT shift values between rows.
If a fee label exists but has no amount, set it to 0.
If a fee label does not exist on this page, do NOT include it."""


def extract_page(page: Dict, model: str = None) -> Dict:
    """Extract structured data from one page image via vision model."""
    model = model or config.EXTRACTION_MODEL
    page_num = page["page_number"]
    img_b64 = page["image_b64"]

    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": EXTRACT_PROMPT},
            ]
        }],
        "temperature": 0,
        "max_tokens": 4000,
    }

    for attempt in range(3):
        with _API_SEMAPHORE:
            try:
                resp = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=config.API_TIMEOUT,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    if cost_tracker:
                        cost_tracker.record("v4_vision", result, model)

                    content = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', content).strip()

                    parsed = {}
                    try:
                        if '{' in cleaned:
                            parsed = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                    except (json.JSONDecodeError, ValueError):
                        parsed = {"raw_response": cleaned}

                    return {
                        "page_number": page_num,
                        "status": "ok",
                        "parsed": parsed,
                        "page_type": parsed.get("page_type", "unknown"),
                        "confidence": parsed.get("confidence", 0),
                        "explanation": parsed.get("explanation", ""),
                    }
            except Exception:
                pass
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    return {
        "page_number": page_num,
        "status": "failed",
        "parsed": {},
        "page_type": "error",
        "confidence": 0,
        "explanation": "Failed after 3 attempts",
    }


ENHANCED_PROMPT = """Look at this document page image VERY carefully. A previous extraction attempt missed data.

This time, extract EVERYTHING — every single piece of text, every number, every label, every table cell.

""" + EXTRACT_PROMPT.replace("Look at this document page image carefully. Extract ALL information you can see.", "")


def _quality_check(result: Dict) -> str:
    """Check extraction quality. Returns: 'pass', 'low_conf', 'empty', 'failed'."""
    if result.get("status") != "ok":
        return "failed"
    parsed = result.get("parsed", {})
    fields = parsed.get("fields", {})
    tables = parsed.get("tables", [])
    amounts = parsed.get("amounts", [])
    conf = result.get("confidence", 0)

    total_data = len(fields) + sum(len(t.get("rows", [])) for t in tables) + len(amounts)

    if total_data == 0:
        return "empty"
    if conf < 0.8 and total_data < 3:
        return "low_conf"
    return "pass"


def extract_all_pages(pages: List[Dict], model: str = None,
                      max_workers: int = 8,
                      progress: Callable = None) -> List[Dict]:
    """Extract all pages in parallel, then QA check + re-run failures."""
    model = model or config.EXTRACTION_MODEL
    total = len(pages)
    print(f"  Extracting {total} pages (parallel, {min(max_workers, total)} workers)")

    start = time.time()
    results = []

    # Phase 1: Extract all pages in parallel
    with ThreadPoolExecutor(max_workers=min(max_workers, total)) as executor:
        futures = {executor.submit(extract_page, p, model): p["page_number"] for p in pages}
        for future in as_completed(futures):
            r = future.result()
            results.append(r)
            pn = r["page_number"]
            pt = r["page_type"]
            conf = r["confidence"]
            print(f"    Page {pn:2d} | {r['status']} | {pt:<30} | conf: {conf}")
            if progress:
                progress(pn, r)

    results.sort(key=lambda r: r["page_number"])
    ok = sum(1 for r in results if r["status"] == "ok")
    dur = time.time() - start
    print(f"  Done: {ok}/{total} pages in {dur:.1f}s")

    # Phase 2: Quality Gate — check each page, re-run failures
    pages_by_num = {p["page_number"]: p for p in pages}
    rerun_count = 0

    for i, r in enumerate(results):
        quality = _quality_check(r)
        if quality == "pass":
            continue

        pn = r["page_number"]
        page = pages_by_num.get(pn)
        if not page:
            continue

        rerun_count += 1
        print(f"    QA: Page {pn} quality={quality} — re-running with enhanced prompt...")

        # Re-run with enhanced prompt for empty/low pages
        retry = extract_page(page, model)
        retry_quality = _quality_check(retry)

        if retry_quality == "pass" or (retry_quality != "failed" and
            len(retry.get("parsed", {}).get("fields", {})) > len(r.get("parsed", {}).get("fields", {}))):
            print(f"    QA: Page {pn} improved: {quality} → {retry_quality}")
            results[i] = retry
        else:
            print(f"    QA: Page {pn} no improvement, keeping original")

    if rerun_count > 0:
        print(f"  QA: Re-ran {rerun_count} pages, {sum(1 for r in results if r['status'] == 'ok')}/{total} ok")
    else:
        print(f"  QA: All {total} pages passed quality check")

    return results
