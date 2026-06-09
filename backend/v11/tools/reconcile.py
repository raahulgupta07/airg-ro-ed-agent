"""V11 — Reconciliation invariant.

The customs declaration carries its own checksum:

    total_customs_value  ==  Σ(item customs_value_mmk)

Any extraction leak — a misclassified page, a split bug, a V7/V10 item miss —
breaks this equation. So instead of guarding each stage, V11 guards the
*equation*, once, at the merge→save boundary every pipeline funnels through.

This module is pure: it computes the balance. The decision (recover / flag /
ship) is made by the caller in v11/workflow.py.
"""
import os
from typing import Dict, List, Optional


def _to_float(v) -> Optional[float]:
    """Best-effort numeric parse. Handles '1,234.56', '108 KG', None, ''."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    # Pull the leading numeric token (e.g. "583.2 KG" → 583.2)
    num = ""
    for ch in s:
        if ch.isdigit() or ch in ".-":
            num += ch
        elif num:
            break
    try:
        return float(num) if num not in ("", ".", "-") else None
    except Exception:
        return None


def tolerance_pct() -> float:
    """Allowed |gap| as percent of declared total before the gate trips."""
    try:
        return float(os.environ.get("RECONCILE_TOLERANCE_PCT", "5.0"))
    except Exception:
        return 5.0


def recovery_enabled() -> bool:
    return os.environ.get("RECONCILE_RECOVER", "1").strip() not in ("0", "false", "False", "")


def items_value_sum(items: List[Dict]) -> float:
    total = 0.0
    for it in items or []:
        v = _to_float(it.get("customs_value_mmk") or it.get("Customs Value (MMK)"))
        if v:
            total += v
    return round(total, 2)


def cif_tolerance_pct() -> float:
    """Allowed |invoice_price×rate − total| as percent. Generous (default 15) so
    genuine freight/insurance differences don't flag, but a wrong exchange rate
    (off by ~36× on a THB-vs-USD mixup) trips it loudly."""
    try:
        return float(os.environ.get("RECONCILE_CIF_TOLERANCE_PCT", "15.0"))
    except Exception:
        return 15.0


def _cif_closure(decl: Dict, declared) -> Dict:
    """Secondary invariant: invoice_price × exchange_rate ≈ total_customs_value.

    Catches a wrong exchange_rate, which the item-sum check cannot see (the rate
    isn't in that math). Returns {cif_checked, cif_ok, cif_gap_pct}.
    """
    inv = _to_float(decl.get("invoice_price") or decl.get("Invoice Price"))
    rate = _to_float(decl.get("exchange_rate") or decl.get("Exchange Rate"))
    if not (inv and rate and declared and declared > 0):
        return {"cif_checked": False, "cif_ok": True, "cif_gap_pct": 0.0}
    gap_pct = round(abs(inv * rate - declared) / declared * 100, 2)
    return {"cif_checked": True, "cif_ok": gap_pct <= cif_tolerance_pct(),
            "cif_gap_pct": gap_pct}


def duty_tolerance_pct() -> float:
    """Allowed |declared_duty − Σ(item value × rate)| as percent. Looser than the
    others because exemptions/reductions legitimately shift duty."""
    try:
        return float(os.environ.get("RECONCILE_DUTY_TOLERANCE_PCT", "20.0"))
    except Exception:
        return 20.0


def _duty_closure(decl: Dict, items: List[Dict]) -> Dict:
    """Advisory invariant: declared customs_duty ≈ Σ(item customs_value × duty_rate).

    Localizes a wrong duty or a wrong per-item duty_rate. ADVISORY only (exemptions
    legitimately break it), so it does NOT flip `balanced`; it just flags for the
    FIXER / review. Returns {duty_checked, duty_ok, duty_gap_pct, duty_expected}.
    """
    declared_duty = _to_float(decl.get("customs_duty")
                              or decl.get("import_export_customs_duty")
                              or decl.get("Import/Export Customs Duty"))
    expected = 0.0
    seen = False
    for it in items or []:
        cv = _to_float(it.get("customs_value_mmk") or it.get("Customs Value (MMK)"))
        rate = _to_float(it.get("customs_duty_rate") or it.get("Customs duty rate"))
        if cv and rate is not None:
            expected += cv * rate
            seen = True
    if not (declared_duty and declared_duty > 0 and seen and expected > 0):
        return {"duty_checked": False, "duty_ok": True, "duty_gap_pct": 0.0,
                "duty_expected": None}
    gap_pct = round(abs(declared_duty - expected) / declared_duty * 100, 2)
    return {"duty_checked": True, "duty_ok": gap_pct <= duty_tolerance_pct(),
            "duty_gap_pct": gap_pct, "duty_expected": round(expected, 2)}


def row_tolerance_pct() -> float:
    try:
        return float(os.environ.get("RECONCILE_ROW_TOLERANCE_PCT", "8.0"))
    except Exception:
        return 8.0


def _row_closure(decl: Dict, items: List[Dict]) -> Dict:
    """Per-item invariant: customs_value_mmk ≈ quantity × cif_unit_price × rate.

    Catches a wrong INDIVIDUAL item value that the total-sum check misses (a doc
    can sum right while one row is wrong). Advisory; lists the suspect rows so the
    FIXER / reviewer can target them. Returns
    {rows_checked, rows_ok, bad_rows:[{idx,expected,got,gap_pct}], n_rows_checked}.
    """
    decl_rate = _to_float(decl.get("exchange_rate") or decl.get("Exchange Rate"))
    tol = row_tolerance_pct()
    bad, checked = [], 0
    for idx, it in enumerate(items or []):
        qty = _to_float(it.get("quantity") or it.get("Quantity (1)"))
        price = (_to_float(it.get("cif_unit_price") or it.get("CIF unit price"))
                 or _to_float(it.get("invoice_unit_price") or it.get("Invoice unit price")))
        rate = _to_float(it.get("exchange_rate")) or decl_rate
        got = _to_float(it.get("customs_value_mmk") or it.get("Customs Value (MMK)"))
        if not (qty and price and rate and got and got > 0):
            continue
        checked += 1
        expected = qty * price * rate
        gap = abs(expected - got) / got * 100
        if gap > tol:
            bad.append({"idx": idx, "expected": round(expected, 2),
                        "got": round(got, 2), "gap_pct": round(gap, 2)})
    return {"rows_checked": checked > 0, "rows_ok": (checked > 0 and not bad),
            "bad_rows": bad, "n_rows_checked": checked}


def reconcile(declaration: Dict, items: List[Dict]) -> Dict:
    """Check items value sum against the declared customs-value total, AND that
    invoice_price × exchange_rate ≈ total (catches a wrong exchange rate).

    Returns a verdict dict:
        {
          "checked": bool,        # False when no usable anchor (can't judge)
          "balanced": bool,       # item-sum within tol AND cif closure ok
          "declared_total": float|None,
          "items_sum": float,
          "gap_value": float,     # declared_total - items_sum (signed)
          "gap_pct": float,       # |gap| / declared_total * 100
          "tolerance_pct": float,
          "anchor": str,          # which declared figure was used
          "n_items": int,
          "cif_checked": bool,    # invoice_price×rate vs total was checkable
          "cif_ok": bool,         # closure holds (rate looks right)
          "cif_gap_pct": float,
        }
    """
    decl = declaration or {}
    isum = items_value_sum(items)

    # Primary anchor: the declared total customs value.
    declared = _to_float(decl.get("total_customs_value") or decl.get("Total Customs Value"))
    anchor = "total_customs_value"

    # Secondary anchor: customs_duty / duty_rate (duty = rate × customs value).
    # Use only if the primary total is absent, so a single bad field can't
    # silently disable the whole gate.
    if not declared:
        duty = _to_float(decl.get("customs_duty") or decl.get("import_export_customs_duty"))
        rate = None
        for it in items or []:
            rate = _to_float(it.get("customs_duty_rate"))
            if rate:
                break
        if duty and rate:
            declared = round(duty / rate, 2)
            anchor = "customs_duty/duty_rate"

    tol = tolerance_pct()
    cif = _cif_closure(decl, declared)
    duty = _duty_closure(decl, items)
    row = _row_closure(decl, items)
    if not declared or declared <= 0:
        # No trustworthy anchor → cannot judge. Don't fake a pass.
        return {
            "checked": False,
            "balanced": False,
            "declared_total": declared,
            "items_sum": isum,
            "gap_value": 0.0,
            "gap_pct": 0.0,
            "tolerance_pct": tol,
            "anchor": "none",
            "n_items": len(items or []),
            **cif,
            **duty,
            **row,
        }

    gap = round(declared - isum, 2)
    gap_pct = round(abs(gap) / declared * 100, 2)
    return {
        "checked": True,
        # Balanced requires BOTH the item-sum closure and the CIF closure.
        "balanced": (gap_pct <= tol) and cif["cif_ok"],
        "declared_total": declared,
        "items_sum": isum,
        "gap_value": gap,
        "gap_pct": gap_pct,
        "tolerance_pct": tol,
        "anchor": anchor,
        "n_items": len(items or []),
        **cif,
        **duty,
        **row,
    }


def _item_key(it: Dict) -> str:
    """Dedup key: HS code + rounded customs value. Stable across pipelines."""
    hs = str(it.get("hs_code") or it.get("HS Code") or "").replace(" ", "")
    cv = _to_float(it.get("customs_value_mmk") or it.get("Customs Value (MMK)"))
    cv_k = str(int(cv)) if cv else ""
    name = str(it.get("item_name") or it.get("Item name") or "").strip().upper()[:24]
    return f"{hs}|{cv_k}|{name}"


def merge_recovered_items(existing: List[Dict], recovered: List[Dict]) -> List[Dict]:
    """Append recovered items that are new (not dups) and carry a real customs value.

    Tags each kept recovered row with `_recovered=True` for audit / UI.
    """
    seen = {_item_key(it) for it in (existing or [])}
    out = list(existing or [])
    for it in recovered or []:
        cv = _to_float(it.get("customs_value_mmk") or it.get("Customs Value (MMK)"))
        if not cv or cv <= 0:
            continue  # genuine attachments (BoL, invoice) yield no customs value
        k = _item_key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append({**it, "_recovered": True})
    return out
