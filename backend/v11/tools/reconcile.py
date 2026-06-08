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


def reconcile(declaration: Dict, items: List[Dict]) -> Dict:
    """Check items value sum against the declared customs-value total.

    Returns a verdict dict:
        {
          "checked": bool,        # False when no usable anchor (can't judge)
          "balanced": bool,       # |gap| within tolerance
          "declared_total": float|None,
          "items_sum": float,
          "gap_value": float,     # declared_total - items_sum (signed)
          "gap_pct": float,       # |gap| / declared_total * 100
          "tolerance_pct": float,
          "anchor": str,          # which declared figure was used
          "n_items": int,
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
        }

    gap = round(declared - isum, 2)
    gap_pct = round(abs(gap) / declared * 100, 2)
    return {
        "checked": True,
        "balanced": gap_pct <= tol,
        "declared_total": declared,
        "items_sum": isum,
        "gap_value": gap,
        "gap_pct": gap_pct,
        "tolerance_pct": tol,
        "anchor": anchor,
        "n_items": len(items or []),
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
