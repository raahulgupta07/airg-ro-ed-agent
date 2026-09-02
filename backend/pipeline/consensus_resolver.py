#!/usr/bin/env python3
"""
Consensus Resolver — merges Gemini 3.1 Pro + Opus 4.7 holistic readings.

Per-field rules to pick winner on disagreement:
- Currency: pick whichever passes band check
- Exchange Rate: pick whichever passes closure equation
- Numeric: pick whichever passes closure
- Names: fuzzy match against seed canonical (KNOWN_CONSIGNORS)
- IDs: regex pattern fit wins
- Decl No: priority MD- > MA0 > MACCS-12digit > raw
- Default: take agreement OR Gemini (cheaper, faster) if no validation rule
"""

import re

import numeric
import difflib
from typing import Dict, List, Tuple, Any


def _extract(field_obj):
    """Extract value + confidence + evidence from voter format {value, confidence, evidence}."""
    if isinstance(field_obj, dict) and "value" in field_obj:
        return field_obj.get("value"), field_obj.get("confidence", "med"), field_obj.get("evidence", "")
    return field_obj, "med", ""


def _str_eq(a, b) -> bool:
    """Loose string equality (case + whitespace + comma normalized)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    sa = re.sub(r'\s+', '', str(a).strip().lower().replace(",", ""))
    sb = re.sub(r'\s+', '', str(b).strip().lower().replace(",", ""))
    return sa == sb


def _num_eq(a, b, tol: float = 0.001) -> bool:
    """Numeric equality with tolerance.

    Parsed by the shared parser so "THB 652,279.7184" and "652279.7184" compare
    equal — a comma-strip alone read the first as unparseable and declared two
    identical amounts different, costing a consensus vote.
    """
    fa = numeric.to_float(a)
    fb = numeric.to_float(b)
    if fa is None or fb is None:
        return False
    if fa == 0 and fb == 0:
        return True
    return abs(fa - fb) / max(abs(fa), abs(fb), 1e-9) < tol


def _is_currency(v) -> bool:
    return bool(v and re.fullmatch(r'[A-Z]{3}', str(v).strip().upper()))


def _passes_currency_band(currency, rate) -> bool:
    """Check rate falls in band for currency."""
    from pipeline.assembler import _RATE_RANGES
    cur = str(currency or "").strip().upper()
    if not _is_currency(cur):
        return False
    try:
        r = float(rate)
    except (ValueError, TypeError):
        return False
    if cur not in _RATE_RANGES:
        return True  # unknown 3-letter, pass
    lo, hi = _RATE_RANGES[cur]
    return lo <= r <= hi


def _passes_closure(decl: Dict) -> bool:
    """inv * rate ≈ cv within 15%."""
    try:
        cv = float(decl.get("Total Customs Value") or 0)
        inv = float(decl.get("Invoice Price") or 0)
        rate = float(decl.get("Exchange Rate") or 0)
        if cv > 0 and inv > 0 and rate > 0:
            ratio = (inv * rate) / cv
            return 0.85 <= ratio <= 1.15
    except (ValueError, TypeError):
        return False
    return True


def _matches_decl_no_priority(value: str) -> int:
    """Return priority score for decl no candidate. Higher = better."""
    if not value:
        return 0
    s = str(value).strip().upper().replace(" ", "")
    # MD-XXXXXX = best
    if re.match(r'^MD-?\d{6,8}$', s):
        return 100
    # MA0XXXXXXXXX = good
    if re.match(r'^MA0\d{9,10}$', s):
        return 90
    # MACCS 12-digit = good for modern
    if re.match(r'^\d{12}$', s):
        return 85
    # Generic alpha+digit
    if re.match(r'^[A-Z0-9-]{6,15}$', s):
        return 50
    return 10


def _matches_invoice_pattern(value: str) -> int:
    """Score invoice number candidates."""
    if not value:
        return 0
    s = str(value).strip().upper()
    # AM-PD-XXX/YYYY = best
    if re.match(r'^AM-PD-\d{3,4}/\d{4}$', s):
        return 100
    if re.match(r'^IV\d{3,5}/\d{3,5}$', s):
        return 90
    if re.match(r'^INV[-]?\d{4,10}$', s):
        return 80
    # B/L-like patterns = LOWER (don't pick as invoice)
    if re.match(r'^[A-Z]{3,4}\d{4}-\d{6,8}$', s):
        return 5
    return 30


def _name_similarity(name: str, canonical_list: List[str]) -> float:
    if not canonical_list:
        return 0.0
    best = 0.0
    n_lower = str(name).strip().lower()
    for c in canonical_list:
        ratio = difflib.SequenceMatcher(None, n_lower, str(c).strip().lower()).ratio()
        if ratio > best:
            best = ratio
    return best


def resolve_field(field_name: str, gem_obj, opus_obj, declaration_so_far: Dict,
                  importer_hint: str = "") -> Tuple[Any, str]:
    """Per-field consensus resolver. Returns (winning_value, reason)."""
    gem_v, gem_c, _ = _extract(gem_obj)
    opus_v, opus_c, _ = _extract(opus_obj)

    # Both empty
    if gem_v is None and opus_v is None:
        return None, "both_null"

    # One is empty
    if gem_v is None:
        return opus_v, "only_opus_has_value"
    if opus_v is None:
        return gem_v, "only_gemini_has_value"

    # Numeric agreement
    if _num_eq(gem_v, opus_v):
        return gem_v, "numeric_match"

    # String agreement
    if _str_eq(gem_v, opus_v):
        return gem_v, "string_match"

    # === DISAGREEMENT — apply per-field rule ===

    if field_name == "Currency":
        rate = declaration_so_far.get("Exchange Rate")
        gem_pass = _passes_currency_band(gem_v, rate) if rate else _is_currency(gem_v)
        opus_pass = _passes_currency_band(opus_v, rate) if rate else _is_currency(opus_v)
        if gem_pass and not opus_pass:
            return gem_v, f"gemini_currency_band_pass(rate={rate})"
        if opus_pass and not gem_pass:
            return opus_v, f"opus_currency_band_pass(rate={rate})"
        if gem_pass and opus_pass:
            return gem_v, "both_pass_currency_take_gemini"  # tiebreak Gemini (cheaper)
        return None, "both_fail_currency"

    if field_name == "Exchange Rate":
        # Pick whichever passes closure given current decl
        try:
            gem_f = float(gem_v); opus_f = float(opus_v)
        except (ValueError, TypeError):
            return gem_v, "rate_parse_fail_take_gemini"
        cand_gem = {**declaration_so_far, "Exchange Rate": gem_f}
        cand_opus = {**declaration_so_far, "Exchange Rate": opus_f}
        if _passes_closure(cand_gem) and not _passes_closure(cand_opus):
            return gem_f, "gemini_rate_closure_pass"
        if _passes_closure(cand_opus) and not _passes_closure(cand_gem):
            return opus_f, "opus_rate_closure_pass"
        # Both pass or both fail — prefer agreement w/ Phase A pipeline rate if available
        return gem_f, "rate_tiebreak_gemini"

    if field_name == "Declaration No":
        gem_score = _matches_decl_no_priority(gem_v)
        opus_score = _matches_decl_no_priority(opus_v)
        if gem_score > opus_score:
            return gem_v, f"gemini_decl_pattern(score={gem_score})"
        if opus_score > gem_score:
            return opus_v, f"opus_decl_pattern(score={opus_score})"
        return gem_v, "decl_tiebreak_gemini"

    if field_name == "Invoice Number":
        gem_score = _matches_invoice_pattern(gem_v)
        opus_score = _matches_invoice_pattern(opus_v)
        if gem_score > opus_score:
            return gem_v, f"gemini_invoice_pattern(score={gem_score})"
        if opus_score > gem_score:
            return opus_v, f"opus_invoice_pattern(score={opus_score})"
        return gem_v, "invoice_tiebreak_gemini"

    if field_name in ("Consignor (Name)", "Importer (Name)"):
        from pipeline.assembler import get_canonical_consignors_for_importer
        canon = get_canonical_consignors_for_importer(importer_hint or declaration_so_far.get("Importer (Name)", ""))
        if canon:
            gem_sim = _name_similarity(gem_v, canon)
            opus_sim = _name_similarity(opus_v, canon)
            if gem_sim > opus_sim and gem_sim >= 0.85:
                return gem_v, f"gemini_name_seed(sim={gem_sim:.2f})"
            if opus_sim > gem_sim and opus_sim >= 0.85:
                return opus_v, f"opus_name_seed(sim={opus_sim:.2f})"
        # Length tiebreak — longer name usually more complete
        if len(str(gem_v)) > len(str(opus_v)) * 1.2:
            return gem_v, "gemini_longer_name"
        if len(str(opus_v)) > len(str(gem_v)) * 1.2:
            return opus_v, "opus_longer_name"
        return gem_v, "name_tiebreak_gemini"

    # Numeric default: prefer one that doesn't blow up arithmetic
    try:
        gem_f = float(str(gem_v).replace(",", ""))
        opus_f = float(str(opus_v).replace(",", ""))
        cand_gem = {**declaration_so_far, field_name: gem_f}
        cand_opus = {**declaration_so_far, field_name: opus_f}
        gem_passes = _passes_closure(cand_gem)
        opus_passes = _passes_closure(cand_opus)
        if gem_passes and not opus_passes:
            return gem_f, f"gemini_{field_name}_closure_pass"
        if opus_passes and not gem_passes:
            return opus_f, f"opus_{field_name}_closure_pass"
    except (ValueError, TypeError):
        pass

    # Confidence tiebreak
    if gem_c == "high" and opus_c != "high":
        return gem_v, "gemini_high_conf"
    if opus_c == "high" and gem_c != "high":
        return opus_v, "opus_high_conf"

    # Default: Gemini (cheaper, slightly higher D19 score)
    return gem_v, "default_gemini"


def merge_ensemble(gemini_result: Dict, opus_result: Dict, base_declaration: Dict,
                   base_items: List[Dict]) -> Tuple[Dict, List[Dict], List[Dict]]:
    """Merge two voter outputs into final declaration + items.
    Returns (declaration, items, change_log)."""

    new_decl = dict(base_declaration)
    new_items = [dict(it) for it in base_items]
    changes = []

    gem_decl = gemini_result.get("declaration", {}) or {}
    opus_decl = opus_result.get("declaration", {}) or {}
    importer = base_declaration.get("Importer (Name)", "")

    # Resolve declaration fields
    for field in set(list(gem_decl.keys()) + list(opus_decl.keys())):
        gem_obj = gem_decl.get(field)
        opus_obj = opus_decl.get(field)
        winning_val, reason = resolve_field(field, gem_obj, opus_obj, new_decl, importer)
        old_val = new_decl.get(field)
        if winning_val is not None and not _str_eq(winning_val, old_val) and not _num_eq(winning_val, old_val):
            new_decl[field] = winning_val
            changes.append({
                "field": field, "original": old_val, "corrected": winning_val,
                "reason": f"ensemble:{reason}",
            })

    # Resolve items — if only one voter has items, use that one's reading
    gem_items = gemini_result.get("items", []) or []
    opus_items = opus_result.get("items", []) or []
    if (gem_items or opus_items) and len(new_items) >= 1:
        for i, base_item in enumerate(new_items):
            gem_item = gem_items[i] if i < len(gem_items) else {}
            opus_item = opus_items[i] if i < len(opus_items) else {}
            field_set = set(list(gem_item.keys()) + list(opus_item.keys()))
            for field in field_set:
                gem_obj = gem_item.get(field)
                opus_obj = opus_item.get(field)
                winning_val, reason = resolve_field(field, gem_obj, opus_obj, new_decl, importer)
                old_val = base_item.get(field)
                if winning_val is not None and not _str_eq(winning_val, old_val) and not _num_eq(winning_val, old_val):
                    new_items[i][field] = winning_val
                    changes.append({
                        "field": f"item[{i}].{field}", "original": old_val, "corrected": winning_val,
                        "reason": f"ensemble:{reason}",
                    })

    return new_decl, new_items, changes
