#!/usr/bin/env python3
"""
LLM Verifier — Cross-check extracted results against source page images.

Sends the assembled declaration + items BACK to the vision model along with
the original page images. The model verifies each value exists in the source.
Catches hallucination, wrong values, and formatting errors.
"""

import json
import re
import time
import requests
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


VERIFY_PROMPT = """You are a verification agent. I extracted data from a customs document using AI.
Now I need you to VERIFY each value by looking at the ORIGINAL page images.

## EXTRACTED RESULTS TO VERIFY:
{results_json}

## YOUR TASK:
Look at the page images below. For EACH field in the extracted results:
1. Find the value on the actual page image
2. Check if the extracted value matches what's printed
3. If WRONG or NOT FOUND on any page, correct it

Return ONLY valid JSON with the CORRECTED results:
{{
  "declaration": {{
    <same 16 fields — corrected values where needed>
  }},
  "items": [
    <same items — corrected values where needed>
  ],
  "corrections": [
    {{"field": "<field name>", "original": "<what was extracted>", "corrected": "<what it should be>", "reason": "<why>"}}
  ]
}}

RULES:
- ONLY change values that are clearly WRONG when compared to the page images.
- If a value matches what's printed, keep it exactly as-is.
- Numbers must match EXACTLY (72,802 not 72,803).
- If you find a value that was null but IS visible on a page, fill it in.
- Do NOT hallucinate — if you can't find a value on any page, leave it as null.
- "Exchange Rate (1)" on items is always the same as declaration Exchange Rate — do NOT change it.
- CRITICAL for fee fields (CT, AT, SF, MF): Each fee must match its SPECIFIC labeled row in the tax/fee table. Do NOT shift values between fee fields. "Commercial Tax" goes to CT, "Security Fee" goes to SF, "MACCS" goes to MF. If a fee is 0, keep it 0.
- Only CORRECT a value if the page image shows a DIFFERENT number. Do NOT remove values just because you can't find them on a specific page — they may come from a different page.
- Return ONLY valid JSON.
"""


def _build_corrections_hint() -> str:
    """Query past corrections and build a hint section for the verifier."""
    try:
        import database
        corrections = database.get_corrections(limit=15)
        if not corrections:
            return ""

        by_field = {}
        for c in corrections:
            field = c.get("field_key", "")
            if field not in by_field:
                by_field[field] = []
            by_field[field].append(c)

        lines = ["\n## COMMON MISTAKES FOUND IN PAST VERIFICATIONS (check for these):"]
        for field, corrs in by_field.items():
            seen = set()
            for c in corrs:
                key = f"{c.get('original_value')}→{c.get('corrected_value')}"
                if key not in seen:
                    seen.add(key)
                    lines.append(f"- {field}: \"{c.get('original_value')}\" should be \"{c.get('corrected_value')}\"")
                if len(seen) >= 2:
                    break

        print(f"    Verifier: injecting {len(corrections)} past corrections as hints")
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


def verify(declaration: Dict, items: List[Dict], pages: List[Dict],
           model: str = None, fmt: str = None) -> Dict:
    """Verify extracted results against source page images.

    Args:
        declaration: Extracted declaration fields
        items: Extracted items list
        pages: Original page dicts with image_b64
        fmt: Document format ("MACCS" or "CUSDEC1") for adaptive page-count.

    Returns: {"declaration": {...}, "items": [...], "corrections": [...], "verified": bool}
    """
    model = model or config.EXTRACTION_MODEL
    print(f"  Verifier: Cross-checking results against source images... (fmt={fmt})")

    results_json = json.dumps({
        "declaration": declaration,
        "items": items,
    }, indent=2, ensure_ascii=False)

    # Inject past corrections as hints
    corrections_hint = _build_corrections_hint()

    # Build prompt with results + corrections
    prompt_text = VERIFY_PROMPT.format(results_json=results_json) + corrections_hint

    # Build message with images — adaptive count to avoid provider payload errors
    content_parts = []

    # Adaptive page count: CUSDEC1 → 3, large docs → 5, else → 10
    if fmt == "CUSDEC1":
        page_limit = 3
    elif len(pages) > 8:
        page_limit = 5
    else:
        page_limit = 10
    pages_to_send = pages[:page_limit]
    print(f"    Verifier: sending {len(pages_to_send)}/{len(pages)} page images")
    for p in pages_to_send:
        img_b64 = p.get("image_b64", "")
        if img_b64:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })

    # Add the verification prompt
    content_parts.append({"type": "text", "text": prompt_text})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 4000,
    }

    fallback_model = "anthropic/claude-haiku-4-5"
    provider_err_count = 0
    used_fallback = False

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=90,
            )
            if resp.status_code == 200:
                result = resp.json()

                # Validate response structure before accessing
                if "choices" not in result or not result["choices"]:
                    # Log actual response for debugging (truncate to avoid spam)
                    err_msg = result.get("error", {})
                    if isinstance(err_msg, dict):
                        err_msg = err_msg.get("message", str(result)[:200])
                    err_str = str(err_msg)
                    print(f"    Verifier: malformed API response — {err_str[:200]}")
                    if "Provider returned error" in err_str:
                        provider_err_count += 1
                        if provider_err_count >= 2 and not used_fallback:
                            print(f"    Verifier: provider error 2x — switching to fallback {fallback_model}")
                            payload["model"] = fallback_model
                            used_fallback = True
                    if attempt < 2:
                        time.sleep(2 ** (attempt + 1))
                    continue

                if cost_tracker:
                    cost_tracker.record("verifier", result, model)

                raw = result["choices"][0]["message"]["content"].strip()
                cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()

                if '{' in cleaned:
                    parsed = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])

                    verified_decl = parsed.get("declaration", declaration)
                    verified_items = parsed.get("items", items)
                    corrections = parsed.get("corrections", [])

                    # Ensure numeric fields stay numeric
                    numeric_fields = [
                        "Invoice Price", "Exchange Rate", "Total Customs Value",
                        "Import/Export Customs Duty", "Commercial Tax (CT)",
                        "Advance Income Tax (AT)", "Security Fee (SF)",
                        "MACCS Service Fee (MF)", "Exemption/Reduction",
                    ]
                    for field in numeric_fields:
                        val = verified_decl.get(field)
                        if val is not None and not isinstance(val, (int, float)):
                            try:
                                verified_decl[field] = float(str(val).replace(",", ""))
                            except (ValueError, TypeError):
                                pass

                    # Default optional fees to 0
                    for f in ["Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction"]:
                        if verified_decl.get(f) is None:
                            verified_decl[f] = 0

                    if corrections:
                        print(f"    Verifier found {len(corrections)} corrections:")
                        for c in corrections:
                            print(f"      {c.get('field')}: {c.get('original')} → {c.get('corrected')} ({c.get('reason')})")
                    else:
                        print("    Verifier: all values confirmed correct")

                    return {
                        "declaration": verified_decl,
                        "items": verified_items,
                        "corrections": corrections,
                        "verified": True,
                    }

            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"    Verifier error: {resp.status_code}")

        except json.JSONDecodeError as e:
            print(f"    Verifier JSON parse error: {e}")
        except Exception as e:
            print(f"    Verifier error: {e}")

        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    # If verification fails, return original results unchanged
    print("    Verifier failed — returning unverified results")
    return {
        "declaration": declaration,
        "items": items,
        "corrections": [],
        "verified": False,
    }
