#!/usr/bin/env python3
"""
Vision Arbiter — Phase B: targeted second-opinion vision recheck.

Fires when sanity validators raise HIGH flags on numeric fields that may be
vision-correlated misreads (handwritten CUSDEC-1: D11/D12/D18/D19 pattern —
Gemini reads same wrong digit consistently, text-only retry can't fix).

Sends ONLY the suspect fields + page images to a different vision model
(Claude Sonnet) with a narrow re-read prompt. If second model returns a
different value AND that value passes closure-equation arithmetic, accept.

Cost: ~$0.05 per flagged doc. Only fires on ~25% of CUSDEC-1 handwritten.
"""

import json
import re
import time
import requests
from typing import Dict, List, Tuple, Optional

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


ARBITER_MODEL = "anthropic/claude-sonnet-4-6"
ARBITER_FALLBACK = "anthropic/claude-haiku-4-5"
ARBITER_FALLBACK_2 = "~google/gemini-pro-latest"  # cross-vendor last resort (SOTA HW)


ARBITER_PROMPT = """You are a careful numeric re-reader for a Myanmar customs document.
A previous AI extracted these fields but a sanity validator flagged them as
potentially wrong (failed cross-arithmetic or out-of-band exchange rate).

## SUSPECT FIELDS (re-read carefully from images):
{suspect_json}

## ORIGINAL EXTRACTION CONTEXT (for cross-check, don't overwrite):
{context_json}

## CRITICAL READING RULES:
- These are HANDWRITTEN or low-quality OCR digits. Look at exact pen-strokes.
- Decimal points often shift: "56.93" can look like "5693" or "5.693" — pick what
  fits the exchange-rate context (THB-MMK ~50, USD-MMK ~3000, KRW ~2.5).
- Currency symbol/code may be near top of page, near total, or in stamp.
- Do NOT invent. If you cannot read clearly, return the field with confidence "low".
- Numbers must be exactly as printed — no rounding, no math.

## RETURN FORMAT (JSON only):
{{
  "rereads": [
    {{
      "field": "<exact field name from suspect list>",
      "value": <number or string>,
      "confidence": "high" | "med" | "low",
      "evidence": "<what part of which page you read it from>"
    }}
  ]
}}
"""


def _build_payload(suspect_fields: Dict, context: Dict, pages: List[Dict],
                   page_limit: int = 2) -> Dict:
    suspect_json = json.dumps(suspect_fields, indent=2)
    context_json = json.dumps({k: context.get(k) for k in [
        "Currency", "Exchange Rate", "Invoice Price", "Total Customs Value",
        "Importer (Name)", "Consignor (Name)"
    ] if context.get(k) is not None}, indent=2)
    prompt_text = ARBITER_PROMPT.format(suspect_json=suspect_json, context_json=context_json)

    content_parts = []
    for p in pages[:page_limit]:
        img_b64 = p.get("image_b64", "")
        if img_b64:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_b64}"}
            })
    content_parts.append({"type": "text", "text": prompt_text})

    return {
        "model": ARBITER_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 1500,
    }


def _validate_with_closure(field: str, new_value, declaration: Dict, items: List[Dict]) -> Tuple[bool, str]:
    """Check if proposed new value passes closure arithmetic.
    Returns (passes, reason)."""
    try:
        if field == "Exchange Rate":
            cv = float(declaration.get("Total Customs Value") or 0)
            inv = float(declaration.get("Invoice Price") or 0)
            new_rate = float(new_value)
            if cv > 0 and inv > 0 and new_rate > 0:
                expected_cv = inv * new_rate
                ratio = expected_cv / cv if cv else 0
                if 0.85 <= ratio <= 1.15:
                    return True, f"closure pass: inv*rate={expected_cv:.0f} ≈ cv={cv:.0f}"
                return False, f"closure fail: inv*rate={expected_cv:.0f} vs cv={cv:.0f} ratio={ratio:.2f}"
        if field == "Invoice Price":
            cv = float(declaration.get("Total Customs Value") or 0)
            rate = float(declaration.get("Exchange Rate") or 0)
            new_inv = float(new_value)
            if cv > 0 and rate > 0 and new_inv > 0:
                expected_cv = new_inv * rate
                ratio = expected_cv / cv if cv else 0
                if 0.85 <= ratio <= 1.15:
                    return True, f"closure pass: new_inv*rate={expected_cv:.0f} ≈ cv={cv:.0f}"
                return False, f"closure fail: ratio={ratio:.2f}"
        if field == "Currency":
            # Currency change: must be valid 3-letter ISO code AND rate must fall in band
            cur_new = str(new_value).strip().upper()
            if not re.fullmatch(r'[A-Z]{3}', cur_new):
                return False, f"invalid currency format '{new_value}' — not 3-letter ISO"
            from pipeline.assembler import _RATE_RANGES
            rate = float(declaration.get("Exchange Rate") or 0)
            if rate > 0 and cur_new in _RATE_RANGES:
                lo, hi = _RATE_RANGES[cur_new]
                if lo <= rate <= hi:
                    return True, f"rate {rate} in band [{lo},{hi}] for {cur_new}"
                return False, f"rate {rate} out-of-band for {cur_new} [{lo},{hi}]"
    except (ValueError, TypeError, ImportError):
        pass
    return False, "no closure check available"


def arbiter_check(declaration: Dict, items: List[Dict], pages: List[Dict],
                  sanity_flags: List[str]) -> Dict:
    """Run focused vision recheck on fields flagged by sanity validators.
    Returns: {"changes": [...], "applied_declaration": {...}, "skipped": int}"""

    # Determine which fields to recheck based on flag types
    # IMPORTANT: D11-class bug — when currency/rate wrong, invoice price often also wrong
    # by same magnitude factor. Always include Invoice Price when rate/currency flagged
    # so multi-field closure can recover all together.
    suspect_keys = set()
    for f in sanity_flags:
        if f.startswith("currency_rate:HIGH") or f.startswith("currency_not_in_pagetext:HIGH"):
            suspect_keys.update(["Currency", "Exchange Rate", "Invoice Price"])
        if f.startswith("invoice_ratio:HIGH"):
            suspect_keys.update(["Invoice Price", "Exchange Rate", "Total Customs Value", "Currency"])
        if f.startswith("closure_eq"):
            suspect_keys.update(["Invoice Price", "Exchange Rate", "Total Customs Value", "Currency"])
        if f.startswith("importer_baseline_exch:HIGH"):
            suspect_keys.update(["Currency", "Exchange Rate", "Invoice Price"])
        if f.startswith("duty_customs_ratio:HIGH"):
            suspect_keys.update(["Total Customs Value", "Import/Export Customs Duty"])

    if not suspect_keys:
        return {"changes": [], "applied_declaration": declaration, "skipped": 0}

    suspect_fields = {k: declaration.get(k) for k in suspect_keys if k in declaration}
    if not suspect_fields:
        return {"changes": [], "applied_declaration": declaration, "skipped": 0}

    print(f"    Vision Arbiter: rechecking {list(suspect_fields.keys())} (flags={len(sanity_flags)})")

    payload = _build_payload(suspect_fields, declaration, pages)

    fallback_stage = 0  # 0=Sonnet, 1=Haiku, 2=Gemini
    parsed = None
    for attempt in range(5):
        try:
            print(f"    Vision Arbiter attempt {attempt+1} model={payload['model']} pages={len([c for c in payload['messages'][0]['content'] if c.get('type')=='image_url'])}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=90,
            )
            print(f"    Vision Arbiter HTTP {resp.status_code}")
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record("vision_arbiter", result, payload["model"])
                    raw = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
                    if '{' in cleaned:
                        parsed = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                        break
                else:
                    err_msg = str(result.get("error", result))[:300]
                    print(f"    Vision Arbiter error response: {err_msg}")
                    # Cross-vendor escalation
                    if fallback_stage == 0:
                        payload["model"] = ARBITER_FALLBACK
                        fallback_stage = 1
                        print(f"    Vision Arbiter: switching to {ARBITER_FALLBACK}")
                    elif fallback_stage == 1:
                        payload["model"] = ARBITER_FALLBACK_2
                        fallback_stage = 2
                        print(f"    Vision Arbiter: switching to {ARBITER_FALLBACK_2}")
            elif resp.status_code == 429:
                print(f"    Vision Arbiter: rate limit, sleep + retry")
                time.sleep(2 ** (attempt + 1))
                continue
            else:
                print(f"    Vision Arbiter HTTP body: {resp.text[:300]}")
                if fallback_stage == 0:
                    payload["model"] = ARBITER_FALLBACK
                    fallback_stage = 1
                elif fallback_stage == 1:
                    payload["model"] = ARBITER_FALLBACK_2
                    fallback_stage = 2
        except Exception as e:
            print(f"    Vision Arbiter exception: {e}")
        if attempt < 4:
            time.sleep(2 ** (attempt + 1))

    if not parsed:
        print("    Vision Arbiter: no response — keeping originals")
        return {"changes": [], "applied_declaration": declaration, "skipped": 1}

    rereads = parsed.get("rereads", [])
    changes = []
    new_decl = dict(declaration)

    # Stage all high-conf rereads tentatively, then validate as a SET (handles multi-field misreads)
    tentative = {}
    for rr in rereads:
        field = rr.get("field")
        new_val = rr.get("value")
        conf = rr.get("confidence", "low")
        if field not in suspect_fields:
            continue
        old_val = declaration.get(field)
        if str(old_val) == str(new_val):
            continue
        if conf != "high":
            print(f"    Arbiter low-conf {field}: {old_val} → {new_val} ({conf}) — flag only")
            continue
        tentative[field] = (old_val, new_val, rr.get("evidence", ""))

    if tentative:
        # Apply all tentative changes to candidate decl
        candidate = dict(declaration)
        for f, (_old, new_val, _ev) in tentative.items():
            candidate[f] = new_val

        # Multi-field closure check: do new values together pass arithmetic?
        try:
            cv = float(candidate.get("Total Customs Value") or 0)
            inv = float(candidate.get("Invoice Price") or 0)
            rate = float(candidate.get("Exchange Rate") or 0)
            cur = str(candidate.get("Currency") or "").upper()
            from pipeline.assembler import _RATE_RANGES
            currency_ok = (cur in _RATE_RANGES and _RATE_RANGES[cur][0] <= rate <= _RATE_RANGES[cur][1]) if cur and rate > 0 else True
            closure_ok = True
            if cv > 0 and inv > 0 and rate > 0:
                expected = inv * rate
                ratio = expected / cv
                closure_ok = 0.85 <= ratio <= 1.15
            multi_pass = currency_ok and closure_ok
            if multi_pass:
                for f, (old, new_val, ev) in tentative.items():
                    print(f"    Arbiter ACCEPTED {f}: {old} → {new_val} (multi-field closure pass)")
                    new_decl[f] = new_val
                    changes.append({
                        "field": f,
                        "original": old,
                        "corrected": new_val,
                        "reason": f"vision_arbiter:high:multi-field-closure-pass:{ev[:80]}",
                    })
            else:
                # Fall through to single-field validation per change
                for f, (old, new_val, ev) in tentative.items():
                    passes, reason = _validate_with_closure(f, new_val, declaration, items)
                    if passes:
                        print(f"    Arbiter ACCEPTED {f}: {old} → {new_val} ({reason})")
                        new_decl[f] = new_val
                        changes.append({
                            "field": f,
                            "original": old,
                            "corrected": new_val,
                            "reason": f"vision_arbiter:high:{ev[:80]}|{reason}",
                        })
                    else:
                        print(f"    Arbiter REJECTED {f}: {old} → {new_val} ({reason})")
        except (ValueError, TypeError, ImportError) as _e:
            print(f"    Arbiter validation error: {_e}")

    return {
        "changes": changes,
        "applied_declaration": new_decl,
        "skipped": 0,
    }


ITEM_ARBITER_PROMPT = """You are re-reading SPECIFIC item-level numbers from a customs document.
A previous AI extracted these item fields but qty × unit_price ≠ invoice_total — likely a vision misread.

## CONTEXT (declaration totals — for cross-check):
- Currency: {currency}
- Exchange Rate: {exchange_rate}
- Total Invoice Price: {invoice_price}
- Total Customs Value: {customs_value}

## SUSPECT ITEM (re-read from images):
{item_json}

## CHECK: qty × unit_price should approximately equal the per-item total.
For 1-item docs: qty × unit_price ≈ Total Invoice Price.
Pen-strokes on handwritten docs often misread decimals (60.81 read as 58.5, etc).

Return JSON:
{{
  "rereads": [
    {{"field": "Invoice unit price", "value": <number>, "confidence": "high|med|low", "evidence": "..."}},
    {{"field": "Quantity (1)", "value": "<value with unit>", "confidence": "...", "evidence": "..."}}
  ]
}}
"""


def arbiter_check_items(declaration: Dict, items: List[Dict], pages: List[Dict],
                        sanity_flags: List[str]) -> Dict:
    """Item-level vision recheck. Fires on closure_eq2 flags (qty × unit ≠ price).
    Currently scoped to 1-item docs (D11/D18/D19 family). Returns updated items."""

    # Only fire if closure_eq2 in flags
    has_eq2 = any(f.startswith("closure_eq2") for f in sanity_flags)
    if not has_eq2 or not items:
        return {"changes": [], "applied_items": items}

    # Conservative: only handle 1-item docs initially
    if len(items) != 1:
        return {"changes": [], "applied_items": items}

    item = items[0]
    inv_unit = item.get("Invoice unit price")
    qty_str = str(item.get("Quantity (1)") or "")
    if inv_unit is None:
        return {"changes": [], "applied_items": items}

    print(f"    Item Arbiter: rechecking item-level fields (closure_eq2 fired)")

    item_json = json.dumps({
        "Item name": item.get("Item name", ""),
        "Quantity (1)": qty_str,
        "Invoice unit price": inv_unit,
        "Customs Value (MMK)": item.get("Customs Value (MMK)"),
    }, indent=2)

    prompt_text = ITEM_ARBITER_PROMPT.format(
        currency=declaration.get("Currency", ""),
        exchange_rate=declaration.get("Exchange Rate", ""),
        invoice_price=declaration.get("Invoice Price", ""),
        customs_value=declaration.get("Total Customs Value", ""),
        item_json=item_json,
    )

    content_parts = []
    for p in pages[:2]:
        img_b64 = p.get("image_b64", "")
        if img_b64:
            content_parts.append({"type": "image_url",
                                   "image_url": {"url": f"data:image/png;base64,{img_b64}"}})
    content_parts.append({"type": "text", "text": prompt_text})

    payload = {
        "model": ARBITER_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 1000,
    }

    fallback_stage = 0
    parsed = None
    for attempt in range(3):
        try:
            print(f"    Item Arbiter attempt {attempt+1} model={payload['model']}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload, timeout=90,
            )
            print(f"    Item Arbiter HTTP {resp.status_code}")
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record("item_arbiter", result, payload["model"])
                    raw = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
                    if '{' in cleaned:
                        parsed = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                        break
                else:
                    err_msg = str(result.get("error", result))[:300]
                    print(f"    Item Arbiter error: {err_msg}")
                    if fallback_stage == 0:
                        payload["model"] = ARBITER_FALLBACK
                        fallback_stage = 1
                    elif fallback_stage == 1:
                        payload["model"] = ARBITER_FALLBACK_2
                        fallback_stage = 2
        except Exception as e:
            print(f"    Item Arbiter exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    if not parsed:
        print("    Item Arbiter: no response — keeping originals")
        return {"changes": [], "applied_items": items}

    new_item = dict(item)
    changes = []
    rereads = parsed.get("rereads", [])
    tentative = {}
    for rr in rereads:
        field = rr.get("field")
        new_val = rr.get("value")
        conf = rr.get("confidence", "low")
        if field not in ("Invoice unit price", "Quantity (1)", "CIF unit price"):
            continue
        old_val = item.get(field)
        if str(old_val) == str(new_val):
            continue
        if conf != "high":
            print(f"    Item Arbiter low-conf {field}: {old_val} → {new_val} ({conf})")
            continue
        tentative[field] = (old_val, new_val, rr.get("evidence", ""))

    # Validate: qty × new_unit ≈ Invoice Price (decl)
    if tentative:
        try:
            inv_price_decl = float(declaration.get("Invoice Price") or 0)
            # Pull qty num
            new_qty = tentative.get("Quantity (1)", (None, qty_str, ""))[1]
            qty_num = 0
            for token in str(new_qty).replace(",", "").split():
                try:
                    qty_num = float(token)
                    break
                except ValueError:
                    continue
            new_unit = tentative.get("Invoice unit price", (None, inv_unit, ""))[1]
            try:
                new_unit_f = float(new_unit)
            except (ValueError, TypeError):
                new_unit_f = float(inv_unit or 0)

            if inv_price_decl > 0 and qty_num > 0 and new_unit_f > 0:
                expected = qty_num * new_unit_f
                ratio = expected / inv_price_decl
                if 0.85 <= ratio <= 1.15:
                    for f, (old, new_v, ev) in tentative.items():
                        print(f"    Item Arbiter ACCEPTED {f}: {old} → {new_v} (qty×unit={expected:.0f} ≈ inv={inv_price_decl:.0f})")
                        new_item[f] = new_v
                        changes.append({
                            "field": f"item[0].{f}",
                            "original": old,
                            "corrected": new_v,
                            "reason": f"item_arbiter:closure_pass_ratio={ratio:.2f}:{ev[:60]}",
                        })
                else:
                    print(f"    Item Arbiter REJECTED: closure fail qty×unit={expected:.0f} vs inv={inv_price_decl:.0f} ratio={ratio:.2f}")
        except (ValueError, TypeError) as _e:
            print(f"    Item Arbiter validation error: {_e}")

    new_items = [new_item] + items[1:]
    return {"changes": changes, "applied_items": new_items}
