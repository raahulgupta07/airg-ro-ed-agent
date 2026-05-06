"""V11 — Result merger.
Combines V7 result (typed slice) + V10 result (HW slice) into single output."""
from typing import Dict, List, Optional


# V7 returns declaration with title-case keys ("Declaration No", "Importer (Name)", ...)
# V10 returns snake_case keys ("declaration_no", "importer_name", ...)
# Map V7 keys → snake_case for unified output

V7_TO_SNAKE = {
    "Declaration No": "declaration_no",
    "Declaration Date": "declaration_date",
    "Importer (Name)": "importer_name",
    "Consignor (Name)": "consignor_name",
    "Invoice Number": "invoice_number",
    "Invoice Number (Customs Declaration)": "invoice_number_customs",
    "Invoice Number (Commercial Invoice)": "invoice_number_commercial",
    "Currency": "currency",
    "Currency 2": "currency_2",
    "Exchange Rate": "exchange_rate",
    "Invoice Price": "invoice_price",
    "Total Customs Value": "total_customs_value",
    "Country Origin": "country_origin",
    "Import/Export Customs Duty": "customs_duty",
    "Commercial Tax (CT)": "commercial_tax",
    "Advance Income Tax (AT)": "advance_income_tax",
    "Security Fee (SF)": "security_fee",
    "MACCS Service Fee (MF)": "maccs_service_fee",
    "Exemption/Reduction": "exemption",
}


def _normalize_v7_decl(decl: Dict) -> Dict:
    """V7 keys → snake_case; pass-through if already snake."""
    out = {}
    for k, v in (decl or {}).items():
        snake = V7_TO_SNAKE.get(k, k)
        out[snake] = v
    return out


V7_TO_SNAKE_ITEM = {
    "Item name": "item_name",
    "Customs duty rate": "customs_duty_rate",
    "Quantity (1)": "quantity",
    "Invoice unit price": "invoice_unit_price",
    "CIF unit price": "cif_unit_price",
    "Currency": "currency",
    "Commercial tax %": "commercial_tax_pct",
    "Exchange Rate (1)": "exchange_rate",
    "HS Code": "hs_code",
    "Origin Country": "origin",
    "Customs Value (MMK)": "customs_value_mmk",
}


def _normalize_v7_item(it: Dict) -> Dict:
    return {V7_TO_SNAKE_ITEM.get(k, k): v for k, v in (it or {}).items()}


def merge_results(v7_result: Optional[Dict], v10_result: Optional[Dict]) -> Dict:
    """Merge V7 (typed) + V10 (HW) extraction outputs.

    Strategy:
      - Declaration: prefer typed (V7) for header fields; fill blanks from HW (V10).
      - Items: union — V7 first, V10 items appended unless duplicate (same hs_code+name).
      - needs_review: True if V10 ran (HW always flagged) OR V7 had cross_validation issue.
      - Cost & duration: sum.
    """
    v7 = v7_result or {}
    v10 = v10_result or {}

    v7_decl_raw = v7.get("declaration") or {}
    v7_items_raw = v7.get("items") or []
    v10_decl = v10.get("declaration") or {}
    v10_items = v10.get("items") or []

    v7_decl = _normalize_v7_decl(v7_decl_raw)
    v7_items = [_normalize_v7_item(it) for it in v7_items_raw]

    # Merge declaration: V7 wins where present, else V10
    merged_decl = dict(v10_decl)  # start with V10
    for k, v in v7_decl.items():
        if v not in (None, "", "None"):
            merged_decl[k] = v   # V7 overrides

    # Merge items: V7 first, then V10 items merged into matching V7 entries
    import re
    def _norm_name(it):
        return str(it.get("item_name") or "").strip().upper()[:30]

    def _norm_hs(it):
        """HS code: digits only — handles '0406.10.10 00' vs '0406101000'."""
        return re.sub(r"\D", "", str(it.get("hs_code") or ""))

    def _dedup_match(a, b):
        """Same item if normalized name fuzzy-matches AND HS codes don't conflict.
           Multiple items can share the same HS code (e.g. all chicken products
           under 1601.00.90), so HS alone is NEVER sufficient — name must match too."""
        na = _norm_name(a)
        nb = _norm_name(b)
        if not na or not nb:
            return False
        # Names must overlap (substring or equal) — handles minor LLM phrasing diffs
        name_match = (na == nb) or (na in nb) or (nb in na)
        if not name_match:
            return False
        # HS must agree (or one missing)
        ha = _norm_hs(a)
        hb = _norm_hs(b)
        if ha and hb and ha != hb:
            return False
        return True

    def _fill_blanks(target, source):
        """Copy source fields into target only where target value is blank."""
        for k, v in source.items():
            cur = target.get(k)
            if cur in (None, "", "None"):
                target[k] = v
        return target

    merged_items = []

    def _add(it):
        for existing in merged_items:
            if _dedup_match(existing, it):
                _fill_blanks(existing, it)
                return
        merged_items.append(dict(it))

    for it in v7_items:
        _add(it)
    for it in v10_items:
        _add(it)

    cost = (v7.get("cost") or v7.get("cost_usd") or 0) + (v10.get("cost") or v10.get("cost_usd") or 0)
    duration = max(v7.get("duration") or 0, v10.get("duration") or 0)  # parallel
    if not duration:
        duration = (v7.get("duration_seconds") or 0) + (v10.get("duration_seconds") or 0)

    return {
        "declaration": merged_decl,
        "items": merged_items,
        "document_format": v7.get("document_format")
                            or v10.get("document_format") or "MIXED",
        "cost": round(cost, 4),
        "duration": round(duration, 1),
        "needs_review": bool(v10) or bool(v7.get("needs_review")) or bool(v10.get("needs_review")),
        "v7_result": {"used": bool(v7), "items_count": len(v7_items)},
        "v10_result": {"used": bool(v10), "items_count": len(v10_items)},
    }
