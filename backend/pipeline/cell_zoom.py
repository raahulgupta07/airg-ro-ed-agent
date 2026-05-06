#!/usr/bin/env python3
"""
Phase C1 — Per-field narrow re-read.

Minimal viable version: when Phase A/B fail to fix vision-correlated misreads
(handwritten CUSDEC-1 D11/D18/D19 family), re-prompt Gemini with NARROW
context-disabled prompts on the same page images. The narrow prompt tells
the model to read ONLY the labeled field, ignore neighboring numbers, and
not infer from context.

Eliminates "context inference" failure mode where Gemini sees handwritten
60.81 but silently corrects to 58.5 because surrounding numbers suggest
58.5 is plausible.

Future C5 = bbox-crop + zoom. This is C1 = narrow-prompt only.
"""

import json
import re
import time
import requests
from typing import Dict, List

import config

try:
    import cost_tracker
except ImportError:
    cost_tracker = None


CELL_ZOOM_MODEL = "google/gemini-3-flash-preview"
CELL_ZOOM_FALLBACK = "anthropic/claude-haiku-4-5"

# C3: Gemini 3 Pro per-cell — SOTA on handwriting per OmniDocBench 1.5 (ED 0.115)
# Use as escalation when C1 (Flash) closure-rejects all readings
CELL_ZOOM_PRO_MODEL = "~google/gemini-pro-latest"


# Map sanity-flag types to which fields to re-read + which CUSDEC-1 form labels to look for
FIELD_LABELS = {
    "Currency": ["Currency", "CCY", "Currency code"],
    "Exchange Rate": ["Exchange Rate", "Rate of Exchange", "Rate", "Currency Rate"],
    "Invoice Price": ["Invoice Price", "Total Invoice", "Total Invoice Price", "Invoice Amount", "Invoice Total"],
    "Total Customs Value": ["Total Customs Value", "CIF Value", "Customs Value", "Total CIF"],
    "Import/Export Customs Duty": ["Customs Duty", "Import Duty", "Duty Amount"],
}

ITEM_FIELD_LABELS = {
    "Invoice unit price": ["Unit Price", "Invoice Unit Price", "Price per Unit", "Unit Cost"],
    "Quantity (1)": ["Quantity", "Qty", "Net Weight", "Gross Weight"],
}


CELL_PROMPT = """You are reading ONE specific field from a customs document image.

## TASK
Find the field labeled with one of: {labels}
Read ONLY the value (number/text) written in or next to that field.

## STRICT RULES
- Return ONLY the value of THAT field. Do not return any other field.
- For HANDWRITTEN values: look at the EXACT pen-strokes. Do not infer from context.
- For numbers: read each digit literally. Do NOT round, do NOT correct, do NOT guess from surrounding numbers.
- Decimal points: look for the dot stroke explicitly. If you see "60.81" do NOT collapse to "58.5" or "608.1".
- If the field is blank or unreadable, return null.
- If you see multiple candidate values, return the one most clearly labeled with one of: {labels}

## OUTPUT (JSON only)
{{
  "field": "{field}",
  "value": <number or string or null>,
  "confidence": "high" | "med" | "low",
  "evidence": "<short note: which page, what the pen-strokes look like>"
}}
"""


def _call_cell_pro(field: str, labels: List[str], pages: List[Dict], page_limit: int = 2) -> Dict:
    """C3: Gemini 3 Pro per-cell. SOTA on handwriting per research.
    Use when C1 (Flash) closure-rejects all readings."""
    content_parts = []
    for p in pages[:page_limit]:
        img = p.get("image_b64", "")
        if img:
            content_parts.append({"type": "image_url",
                                   "image_url": {"url": f"data:image/png;base64,{img}"}})
    label_str = ", ".join(f"'{l}'" for l in labels)
    prompt = CELL_PROMPT.format(field=field, labels=label_str)
    content_parts.append({"type": "text", "text": prompt})

    payload = {
        "model": CELL_ZOOM_PRO_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 600,
    }

    fallback_used = False
    for attempt in range(3):
        try:
            print(f"    Cell zoom PRO: re-reading '{field}' attempt {attempt+1} model={payload['model']}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload, timeout=120,
            )
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record("cell_zoom_pro", result, payload["model"])
                    raw = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
                    if '{' in cleaned:
                        return json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                else:
                    err = str(result.get("error", result))[:200]
                    print(f"    Cell zoom PRO error: {err}")
                    if not fallback_used:
                        # Fallback to Claude Opus for cross-vendor on Pro tier
                        payload["model"] = "anthropic/claude-opus-4-7"
                        fallback_used = True
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
        except Exception as e:
            print(f"    Cell zoom PRO exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {}


def _call_cell(field: str, labels: List[str], pages: List[Dict], page_limit: int = 2) -> Dict:
    """Single field re-read call with narrow prompt."""
    content_parts = []
    for p in pages[:page_limit]:
        img = p.get("image_b64", "")
        if img:
            content_parts.append({"type": "image_url",
                                   "image_url": {"url": f"data:image/png;base64,{img}"}})
    label_str = ", ".join(f"'{l}'" for l in labels)
    prompt = CELL_PROMPT.format(field=field, labels=label_str)
    content_parts.append({"type": "text", "text": prompt})

    payload = {
        "model": CELL_ZOOM_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "temperature": 0,
        "max_tokens": 500,
    }

    fallback_used = False
    for attempt in range(3):
        try:
            print(f"    Cell zoom: re-reading '{field}' attempt {attempt+1} model={payload['model']}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"},
                json=payload, timeout=60,
            )
            if resp.status_code == 200:
                result = resp.json()
                if "choices" in result and result["choices"]:
                    if cost_tracker:
                        cost_tracker.record("cell_zoom", result, payload["model"])
                    raw = result["choices"][0]["message"]["content"].strip()
                    cleaned = re.sub(r'```json\n?|```\n?', '', raw).strip()
                    if '{' in cleaned:
                        return json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
                else:
                    err = str(result.get("error", result))[:200]
                    print(f"    Cell zoom error: {err}")
                    if not fallback_used:
                        payload["model"] = CELL_ZOOM_FALLBACK
                        fallback_used = True
            elif resp.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
        except Exception as e:
            print(f"    Cell zoom exception: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return {}


def zoom_check(declaration: Dict, items: List[Dict], pages: List[Dict],
               sanity_flags: List[str]) -> Dict:
    """Phase C1 fallback re-read. Fires when Phase B arbiter exhausted on numerics.
    Returns: {"changes": [...], "applied_declaration": {...}, "applied_items": [...]}"""

    # Determine which fields to re-read
    decl_fields_to_check = set()
    item_fields_to_check = set()
    for f in sanity_flags:
        if f.startswith("currency_rate:HIGH") or f.startswith("currency_not_in_pagetext:HIGH"):
            decl_fields_to_check.update(["Currency", "Exchange Rate", "Invoice Price"])
        if f.startswith("invoice_ratio:HIGH"):
            decl_fields_to_check.update(["Invoice Price", "Total Customs Value", "Exchange Rate", "Currency"])
        if f.startswith("closure_eq1"):
            decl_fields_to_check.update(["Invoice Price", "Exchange Rate", "Total Customs Value", "Currency"])
        if f.startswith("closure_eq2"):
            item_fields_to_check.update(["Invoice unit price", "Quantity (1)"])
        if f.startswith("duty_customs_ratio:HIGH"):
            decl_fields_to_check.update(["Total Customs Value", "Import/Export Customs Duty"])

    if not decl_fields_to_check and not item_fields_to_check:
        return {"changes": [], "applied_declaration": declaration, "applied_items": items}

    print(f"    Cell zoom: decl_fields={list(decl_fields_to_check)} item_fields={list(item_fields_to_check)}")

    new_decl = dict(declaration)
    new_items = [dict(it) for it in items]
    changes = []

    # Re-read declaration fields
    for field in decl_fields_to_check:
        labels = FIELD_LABELS.get(field, [field])
        result = _call_cell(field, labels, pages)
        if not result:
            continue
        new_val = result.get("value")
        conf = result.get("confidence", "low")
        evidence = result.get("evidence", "")
        old_val = declaration.get(field)
        if new_val is None or str(new_val) == str(old_val):
            continue
        if conf != "high":
            print(f"    Cell zoom low-conf {field}: {old_val} → {new_val} ({conf}) — flag only")
            continue
        # Closure validation: try applying and check arithmetic
        candidate = dict(new_decl)
        candidate[field] = new_val
        if _passes_closure(candidate):
            print(f"    Cell zoom ACCEPTED {field}: {old_val} → {new_val} (closure pass)")
            new_decl[field] = new_val
            changes.append({
                "field": field,
                "original": old_val,
                "corrected": new_val,
                "reason": f"cell_zoom:high:{evidence[:80]}",
            })
        else:
            print(f"    Cell zoom REJECTED {field}: {old_val} → {new_val} (closure fail)")

    # Re-read item fields (1-item docs only for now)
    if item_fields_to_check and len(new_items) == 1:
        for field in item_fields_to_check:
            labels = ITEM_FIELD_LABELS.get(field, [field])
            result = _call_cell(field, labels, pages)
            if not result:
                continue
            new_val = result.get("value")
            conf = result.get("confidence", "low")
            evidence = result.get("evidence", "")
            old_val = new_items[0].get(field)
            if new_val is None or str(new_val) == str(old_val):
                continue
            if conf != "high":
                print(f"    Cell zoom low-conf item.{field}: {old_val} → {new_val} ({conf})")
                continue
            # Validate via item closure: qty × unit ≈ invoice_price
            if _item_closure_pass(new_items[0], field, new_val, new_decl):
                print(f"    Cell zoom ACCEPTED item.{field}: {old_val} → {new_val}")
                new_items[0][field] = new_val
                changes.append({
                    "field": f"item[0].{field}",
                    "original": old_val,
                    "corrected": new_val,
                    "reason": f"cell_zoom:high:{evidence[:80]}",
                })
            else:
                print(f"    Cell zoom REJECTED item.{field}: {old_val} → {new_val} (item closure fail)")

    return {
        "changes": changes,
        "applied_declaration": new_decl,
        "applied_items": new_items,
    }


def zoom_check_pro(declaration: Dict, items: List[Dict], pages: List[Dict],
                    sanity_flags: List[str]) -> Dict:
    """C3: Gemini 3 Pro per-cell. SOTA on handwriting. Fires after C1 (Flash) exhausted.
    Same flow as C1 but uses Pro model — costs ~10x more, accuracy much higher."""

    decl_fields_to_check = set()
    item_fields_to_check = set()
    for f in sanity_flags:
        if f.startswith("currency_rate:HIGH") or f.startswith("currency_not_in_pagetext:HIGH"):
            decl_fields_to_check.update(["Currency", "Exchange Rate", "Invoice Price"])
        if f.startswith("invoice_ratio:HIGH"):
            decl_fields_to_check.update(["Invoice Price", "Total Customs Value", "Exchange Rate", "Currency"])
        if f.startswith("closure_eq1"):
            decl_fields_to_check.update(["Invoice Price", "Exchange Rate", "Total Customs Value", "Currency"])
        if f.startswith("closure_eq2"):
            item_fields_to_check.update(["Invoice unit price", "Quantity (1)"])
        if f.startswith("duty_customs_ratio:HIGH"):
            decl_fields_to_check.update(["Total Customs Value", "Import/Export Customs Duty"])

    if not decl_fields_to_check and not item_fields_to_check:
        return {"changes": [], "applied_declaration": declaration, "applied_items": items}

    print(f"    Cell zoom PRO: decl_fields={list(decl_fields_to_check)} item_fields={list(item_fields_to_check)}")

    new_decl = dict(declaration)
    new_items = [dict(it) for it in items]
    changes = []

    # Stage all Pro re-reads first, then validate as a SET (multi-field misread case)
    pro_decl_results = {}
    for field in decl_fields_to_check:
        labels = FIELD_LABELS.get(field, [field])
        result = _call_cell_pro(field, labels, pages)
        if not result:
            continue
        new_val = result.get("value")
        conf = result.get("confidence", "low")
        ev = result.get("evidence", "")
        old_val = declaration.get(field)
        if new_val is None or str(new_val) == str(old_val) or conf != "high":
            if new_val is not None and conf != "high":
                print(f"    Cell zoom PRO low-conf {field}: {old_val} → {new_val} ({conf})")
            continue
        pro_decl_results[field] = (old_val, new_val, ev)

    # Apply all Pro changes simultaneously, validate combined closure
    if pro_decl_results:
        candidate = dict(new_decl)
        for f, (_, new_val, _) in pro_decl_results.items():
            candidate[f] = new_val
        if _passes_closure(candidate):
            for f, (old, new_val, ev) in pro_decl_results.items():
                print(f"    Cell zoom PRO ACCEPTED {f}: {old} → {new_val} (multi-closure pass)")
                new_decl[f] = new_val
                changes.append({
                    "field": f,
                    "original": old,
                    "corrected": new_val,
                    "reason": f"cell_zoom_pro:multi_closure_pass:{ev[:80]}",
                })
        else:
            # Try single-field at a time
            for f, (old, new_val, ev) in pro_decl_results.items():
                single = dict(new_decl)
                single[f] = new_val
                if _passes_closure(single):
                    print(f"    Cell zoom PRO ACCEPTED {f}: {old} → {new_val} (single closure pass)")
                    new_decl[f] = new_val
                    changes.append({
                        "field": f,
                        "original": old,
                        "corrected": new_val,
                        "reason": f"cell_zoom_pro:single_closure_pass:{ev[:80]}",
                    })
                else:
                    print(f"    Cell zoom PRO REJECTED {f}: {old} → {new_val} (closure fail)")

    # Item-level Pro re-read
    if item_fields_to_check and len(new_items) == 1:
        item_pro_results = {}
        for field in item_fields_to_check:
            labels = ITEM_FIELD_LABELS.get(field, [field])
            result = _call_cell_pro(field, labels, pages)
            if not result:
                continue
            new_val = result.get("value")
            conf = result.get("confidence", "low")
            ev = result.get("evidence", "")
            old_val = new_items[0].get(field)
            if new_val is None or str(new_val) == str(old_val) or conf != "high":
                continue
            item_pro_results[field] = (old_val, new_val, ev)

        # Apply all item changes together, validate via item closure
        if item_pro_results:
            candidate_item = dict(new_items[0])
            for f, (_, new_val, _) in item_pro_results.items():
                candidate_item[f] = new_val
            # Item closure: qty × unit ≈ invoice price
            try:
                inv_price = float(new_decl.get("Invoice Price") or 0)
                qty_str = str(candidate_item.get("Quantity (1)") or "").replace(",", "")
                qty_num = 0.0
                for tok in qty_str.split():
                    try:
                        qty_num = float(tok); break
                    except ValueError:
                        continue
                unit = float(candidate_item.get("Invoice unit price") or 0)
                if inv_price > 0 and qty_num > 0 and unit > 0:
                    ratio = (qty_num * unit) / inv_price
                    if 0.85 <= ratio <= 1.15:
                        for f, (old, new_val, ev) in item_pro_results.items():
                            print(f"    Cell zoom PRO ACCEPTED item.{f}: {old} → {new_val} (item closure pass ratio={ratio:.2f})")
                            new_items[0][f] = new_val
                            changes.append({
                                "field": f"item[0].{f}",
                                "original": old,
                                "corrected": new_val,
                                "reason": f"cell_zoom_pro:item_closure_pass:{ev[:80]}",
                            })
                    else:
                        print(f"    Cell zoom PRO REJECTED item: closure fail qty×unit={qty_num*unit:.0f} vs inv={inv_price:.0f}")
            except (ValueError, TypeError) as _e:
                print(f"    Cell zoom PRO item validation error: {_e}")

    return {
        "changes": changes,
        "applied_declaration": new_decl,
        "applied_items": new_items,
    }


def _passes_closure(declaration: Dict) -> bool:
    """Check inv*rate ≈ cv within 15% AND currency is valid 3-letter code in band."""
    try:
        cv = float(declaration.get("Total Customs Value") or 0)
        inv = float(declaration.get("Invoice Price") or 0)
        rate = float(declaration.get("Exchange Rate") or 0)
        cur = str(declaration.get("Currency") or "").strip().upper()
        # Reject invalid currency formats (not 3-letter ISO code)
        if cur and not re.fullmatch(r'[A-Z]{3}', cur):
            return False
        from pipeline.assembler import _RATE_RANGES
        if cur in _RATE_RANGES and rate > 0:
            lo, hi = _RATE_RANGES[cur]
            if not (lo <= rate <= hi):
                return False
        if cv > 0 and inv > 0 and rate > 0:
            ratio = (inv * rate) / cv
            return 0.85 <= ratio <= 1.15
    except (ValueError, TypeError, ImportError):
        return False
    return True


def _item_closure_pass(item: Dict, field: str, new_val, declaration: Dict) -> bool:
    """Item closure: qty × unit_price ≈ invoice price (decl)."""
    try:
        inv_price = float(declaration.get("Invoice Price") or 0)
        if inv_price <= 0:
            return False
        candidate = dict(item)
        candidate[field] = new_val
        # Extract qty number
        qty_str = str(candidate.get("Quantity (1)") or "").replace(",", "")
        qty_num = 0.0
        for token in qty_str.split():
            try:
                qty_num = float(token)
                break
            except ValueError:
                continue
        unit = float(candidate.get("Invoice unit price") or 0)
        if qty_num <= 0 or unit <= 0:
            return False
        expected = qty_num * unit
        ratio = expected / inv_price
        return 0.85 <= ratio <= 1.15
    except (ValueError, TypeError):
        return False
