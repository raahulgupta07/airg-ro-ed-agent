"""V10 PRO — Field shape validation. Catches truncated/hallucinated reads."""
import re
from typing import Dict, List

# Field constraints
FIELD_RULES = {
    "declaration_no":     {"min_digits": 11, "max_digits": 14, "must_be_numeric": True},
    "exchange_rate":      {"min_value": 0.5, "max_value": 7000},  # any plausible MMK rate
    "invoice_price":      {"min_value": 0, "must_be_numeric": True},
    "total_customs_value":{"min_value": 0, "must_be_numeric": True},
}

# Currency rate plausibility bands
CCY_BANDS = {
    "THB": (40, 110),  "USD": (1300, 5500), "EUR": (1500, 6500),
    "KRW": (1.0, 5.0), "JPY": (10, 50),     "CNY": (200, 900),
    "SGD": (1500, 4500), "GBP": (2000, 7000),
}


def _digits_only(v) -> str:
    return re.sub(r"\D", "", str(v))


def _to_float(v):
    try: return float(str(v).replace(",", ""))
    except Exception: return None


def validate_field(field: str, value, context: Dict = None) -> Dict:
    """Return {"valid": bool, "issue": str|None, "suggested_action": str}."""
    if value in (None, "", "None"):
        return {"valid": True, "issue": None, "suggested_action": None}
    rule = FIELD_RULES.get(field)
    if not rule:
        return {"valid": True, "issue": None, "suggested_action": None}

    if rule.get("must_be_numeric"):
        digits = _digits_only(value)
        if not digits:
            return {"valid": False, "issue": "no digits found",
                    "suggested_action": "re_zoom"}
        if "min_digits" in rule and len(digits) < rule["min_digits"]:
            return {"valid": False,
                    "issue": f"only {len(digits)} digits (need >={rule['min_digits']})",
                    "suggested_action": "re_zoom_with_length_hint"}
        if "max_digits" in rule and len(digits) > rule["max_digits"]:
            return {"valid": False,
                    "issue": f"{len(digits)} digits (max {rule['max_digits']})",
                    "suggested_action": "re_zoom"}

    if "min_value" in rule:
        f = _to_float(value)
        if f is not None and f < rule["min_value"]:
            return {"valid": False, "issue": f"value {f} < min {rule['min_value']}",
                    "suggested_action": "re_zoom"}
    if "max_value" in rule:
        f = _to_float(value)
        if f is not None and f > rule["max_value"]:
            return {"valid": False, "issue": f"value {f} > max {rule['max_value']}",
                    "suggested_action": "re_zoom"}
    return {"valid": True, "issue": None, "suggested_action": None}


def validate_currency_rate(currency: str, rate) -> Dict:
    """Cross-check that rate falls in plausible band for currency."""
    cur = (currency or "").upper().strip()
    r = _to_float(rate)
    if not cur or r is None:
        return {"valid": True, "issue": None}
    if cur not in CCY_BANDS:
        return {"valid": True, "issue": f"unknown currency {cur}"}
    lo, hi = CCY_BANDS[cur]
    if lo <= r <= hi:
        return {"valid": True, "issue": None, "band": [lo, hi]}
    return {"valid": False,
            "issue": f"rate {r} outside {cur} band [{lo}, {hi}]",
            "suggested_action": "re_zoom_currency_and_rate",
            "band": [lo, hi]}


def validate_declaration(declaration: Dict) -> Dict:
    """Validate every critical field. Returns flags + summary."""
    flags = []
    for fld, val in declaration.items():
        v = validate_field(fld, val)
        if not v["valid"]:
            flags.append({"field": fld, "value": str(val)[:40], **v})
    cur = declaration.get("currency")
    rate = declaration.get("exchange_rate")
    if cur and rate:
        v = validate_currency_rate(cur, rate)
        if not v["valid"]:
            flags.append({"field": "currency_rate_consistency",
                          "currency": cur, "rate": rate, **v})
    return {"all_valid": len(flags) == 0,
            "flags": flags,
            "n_issues": len(flags)}
