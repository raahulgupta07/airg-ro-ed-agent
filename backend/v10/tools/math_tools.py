"""V9 — Arithmetic and sanity-check tools (pure Python, no LLM)."""
from typing import Dict, Optional
from agno.tools import tool

# Lazy import so module loads even if v9.config not yet on path
def _bands():
    try:
        from v10.config import CCY_BANDS
        return CCY_BANDS
    except ImportError:
        return {"THB": (40, 110), "USD": (1300, 5500), "EUR": (1500, 6500),
                "KRW": (1.0, 5.0), "JPY": (10, 50), "CNY": (200, 900),
                "SGD": (1500, 4500), "GBP": (2000, 7000)}


@tool(show_result=False)
def arithmetic_audit(declaration: Dict) -> Dict:
    """Validate cross-field arithmetic on a customs declaration.

    Checks (each tolerance ±5%):
    - CV ÷ rate ≈ Invoice Price
    - 15% × CV ≈ Customs Duty
    - 5% × (CV + Duty) ≈ Commercial Tax
    - 2% × CV ≈ MACCS Service Fee

    Input keys (snake_case): total_customs_value, exchange_rate, invoice_price,
    customs_duty, commercial_tax, maccs_service_fee.

    Returns:
      {"ok": bool, "flags": [{"field", "expected", "got", "rule", "delta_pct"}]}"""
    flags = []
    cv = float(declaration.get("total_customs_value") or 0)
    rate = float(declaration.get("exchange_rate") or 0)
    inv = float(declaration.get("invoice_price") or 0)
    duty = float(declaration.get("customs_duty") or 0)
    ct = float(declaration.get("commercial_tax") or 0)
    mf = float(declaration.get("maccs_service_fee") or 0)

    def _check(field, expected, got, rule, tol=0.05):
        if expected and got:
            delta = abs(expected - got) / max(expected, 1)
            if delta > tol:
                flags.append({"field": field, "expected": round(expected, 2),
                              "got": round(got, 2), "rule": rule,
                              "delta_pct": round(delta * 100, 1)})

    if cv and rate:
        _check("invoice_price", cv / rate, inv, "CV÷rate", tol=0.02)
    if cv:
        _check("customs_duty_15pct", cv * 0.15, duty, "15%×CV", tol=0.10)
        _check("maccs_service_fee", cv * 0.02, mf, "2%×CV", tol=0.05)
    if cv and duty is not None:
        _check("commercial_tax", (cv + duty) * 0.05, ct, "5%×(CV+Duty)", tol=0.05)

    return {"ok": len(flags) == 0, "flags": flags}


@tool(show_result=False)
def currency_band_check(currency: str, exchange_rate: float) -> Dict:
    """Verify exchange rate falls in plausible band for the currency.

    Used to catch currency misreads (e.g. THB labeled USD).
    Returns {"in_band": bool, "currency", "rate", "band": [lo, hi], "note"}."""
    bands = _bands()
    cur = (currency or "").upper().strip()
    rate = float(exchange_rate or 0)
    if cur not in bands:
        return {"in_band": None, "currency": cur, "rate": rate,
                "band": None, "note": f"unknown currency {cur!r}"}
    lo, hi = bands[cur]
    in_band = lo <= rate <= hi
    note = "ok" if in_band else f"rate {rate} outside [{lo}, {hi}] — currency or rate misread"
    return {"in_band": in_band, "currency": cur, "rate": rate,
            "band": [lo, hi], "note": note}


@tool(show_result=False)
def deterministic_postfix(declaration: Dict) -> Dict:
    """Apply pure-rule fills for missing fee fields.

    - MACCS Fee = round(2% × CV) if blank
    - Commercial Tax = round(5% × (CV + Duty)) if blank
    Does NOT override existing values.

    Returns {"declaration": <updated>, "applied": [rule names]}."""
    d = dict(declaration)
    applied = []
    cv = float(d.get("total_customs_value") or 0)
    duty = float(d.get("customs_duty") or 0)
    if cv > 0:
        if not d.get("maccs_service_fee"):
            d["maccs_service_fee"] = round(cv * 0.02)
            applied.append("MACCS = 2% × CV")
        if not d.get("commercial_tax"):
            d["commercial_tax"] = round((cv + duty) * 0.05)
            applied.append("CT = 5% × (CV+Duty)")
    return {"declaration": d, "applied": applied}
