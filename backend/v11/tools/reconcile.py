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
    """Allowed |(invoice build-up)×rate − total| as percent when freight/insurance/
    adjustment are NOT given (default 15) so genuine CIF additions don't flag, but a
    wrong exchange rate (off by ~36× on a THB-vs-USD mixup) trips it loudly."""
    try:
        return float(os.environ.get("RECONCILE_CIF_TOLERANCE_PCT", "15.0"))
    except Exception:
        return 15.0


def cif_tight_tolerance_pct() -> float:
    """Tighter tolerance used when freight/insurance/adjustment ARE present — the
    CIF build-up is now fully explained, so a small (default 4%) gap is all we allow."""
    try:
        return float(os.environ.get("RECONCILE_CIF_TIGHT_TOLERANCE_PCT", "4.0"))
    except Exception:
        return 4.0


def _first_present(decl: Dict, *keys):
    """First key that parses to a number, keeping an explicit 0.0.

    `decl.get(a) or decl.get(b)` is the obvious form and is wrong here: a
    zero-valued fee is a real reading, and treating it as absent changes which
    tolerance the CIF gate applies.
    """
    for k in keys:
        v = _to_float(decl.get(k))
        if v is not None:
            return v
    return None


def _cif_closure(decl: Dict, declared) -> Dict:
    """Secondary invariant: CIF build-up × exchange_rate ≈ total_customs_value, where
        CIF build-up = invoice_price + freight + insurance + adjustment   (invoice currency)

    Catches a wrong exchange_rate, which the item-sum check cannot see (the rate
    isn't in that math). When freight/insurance/adjustment are supplied the build-up
    is complete, so the tolerance tightens. Returns
    {cif_checked, cif_ok, cif_gap_pct, cif_basis, cif_tol_pct}.
    """
    # The identity is stated in the INVOICE currency, so the foreign-currency
    # invoice is the correct basis when the engine captured it. `invoice_price`
    # is whichever of the form's two figures the read landed on (the form prints
    # both "THB 481,406.664" and "(MMK) 32,356,946.56"), so it is the fallback.
    inv = _first_present(decl, "invoice_price_fc", "invoice_price", "Invoice Price")
    rate = _first_present(decl, "exchange_rate", "Exchange Rate")
    if not (inv and rate and declared and declared > 0):
        return {"cif_checked": False, "cif_ok": True, "cif_gap_pct": 0.0,
                "cif_basis": None, "cif_tol_pct": 0.0, "cif_ratio": None}
    # `a or b` would discard an explicit 0.0 — and a declared zero freight is
    # information, not a missing value. It says the CIF build-up is complete,
    # which is exactly what decides whether the tolerance below tightens to 4%.
    frt = _first_present(decl, "freight_value", "Freight")
    ins = _first_present(decl, "insurance_value", "Insurance")
    adj = _first_present(decl, "adjustment_value", "Adjustment")
    has_buildup = any(v is not None for v in (frt, ins, adj))

    # Each build-up line is printed with its OWN currency code, and they differ on
    # the same form — "Insurance E - MMK - 267,383.37" beside
    # "Adjustment value AD - CNY - 1,051.894". Adding an already-MMK figure into an
    # invoice-currency basis and multiplying the lot by the rate inflated one real
    # declaration from 24,886,695 to 103,859,443 and would fail a correct document.
    # A line whose stated currency is MMK is added AFTER conversion, not before.
    # No stated currency -> invoice currency, which is how every stored row behaves.
    def _is_mmk(*keys) -> bool:
        for k in keys:
            v = decl.get(k)
            if v:
                return str(v).strip().upper() in ("MMK", "MMK.", "KS", "KYAT", "KYATS")
        return False

    fc_parts, mmk_parts = [], []
    for val, cur_keys in ((frt, ("freight_currency", "Freight Currency")),
                          (ins, ("insurance_currency", "Insurance Currency")),
                          (adj, ("adjustment_currency", "Adjustment Currency"))):
        if val is None:
            continue
        (mmk_parts if _is_mmk(*cur_keys) else fc_parts).append(val)

    basis = inv + sum(fc_parts)
    tol = cif_tight_tolerance_pct() if has_buildup else cif_tolerance_pct()
    computed = basis * rate + sum(mmk_parts)
    gap_pct = round(abs(computed - declared) / declared * 100, 2)
    # How the declared total relates to the computed one, reported alongside the
    # gap. Measured over the stored corpus, the failures are not near-misses that
    # a wider tolerance would absorb — they cluster on clean multiples (four
    # separate declarations sit at exactly 1.500, one at 10.654). A ratio names
    # that: "the total is 1.5x the invoice basis" points at a missed invoice or
    # the wrong one of two printed figures, where a bare percentage does not.
    ratio = round(declared / computed, 4) if computed else None
    return {"cif_checked": True, "cif_ok": gap_pct <= tol,
            "cif_gap_pct": gap_pct, "cif_basis": round(basis, 2),
            "cif_tol_pct": tol, "cif_ratio": ratio}


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


def rate_tolerance_pct() -> float:
    """Allowed |extracted_rate − derived_rate| as percent of the derived rate before
    the exchange rate is called suspect. The extracted rate comes from a fragile
    regex/OCR upstream, so we cross-check it against an independent MATH derivation."""
    try:
        return float(os.environ.get("RATE_TOLERANCE_PCT", "8.0"))
    except Exception:
        return 8.0


# Broad plausibility band for a THB/USD→MMK exchange rate. Anything outside is a
# parse artefact (a stray count, a total, a dash) rather than a real rate.
_RATE_LO, _RATE_HI = 0.5, 6000.0

# Per-currency plausibility bands (invoice currency → MMK). Mirrors cusdec_rescue
# ._RATE_BANDS; kept local so reconcile stays a pure-math module with no fitz import.
# The PRIMARY rate signal — this is what catches the UAT failures (500 for USD, 636
# for THB) that a math cross-check alone can miss when the CIF basis is incomplete.
_CCY_BANDS = {
    "THB": (40.0, 90.0), "USD": (1500.0, 5000.0), "SGD": (1500.0, 2500.0),
    "CNY": (250.0, 600.0), "EUR": (2000.0, 5000.0), "JPY": (12.0, 40.0),
    "AUD": (1200.0, 3500.0),
}


def _rate_check(decl: Dict, declared, items: List[Dict]) -> Dict:
    """Independent cross-check on the extracted exchange_rate — the field most often
    wrong (bad regex / OCR upstream) and the one the item-sum gate cannot see.

    Derive the rate two independent ways from figures the item-sum math already
    trusts, and compare to the extracted rate:

        rate_from_basis = total_customs_value / (invoice + freight + insurance + adjustment)
        rate_from_items = total_customs_value / Σ(item invoice_unit_price × quantity)

    Prefer the basis derivation, fall back to items. `rate_suspect` when the extracted
    rate disagrees beyond RATE_TOLERANCE_PCT, is missing while a derivation exists, or
    is outside the plausibility band. Pure report — never mutates the decl; the
    workflow layer acts on `derived_rate`. Accepts both key spaces (the CIF build-up
    fields carry the same names pre/post merge; item keys likewise).

    Returns {rate_suspect, derived_rate, extracted_rate}.
    """
    extracted_rate = _to_float(decl.get("exchange_rate") or decl.get("Exchange Rate"))
    currency = str(decl.get("currency") or decl.get("Currency") or "").strip().upper()[:3]
    band = _CCY_BANDS.get(currency)

    # Derive the rate from figures the item-sum math already trusts. The derivation is
    # only TRUSTWORTHY when the full CIF build-up is present — on a CIF/DAP doc the
    # customs value includes a freight/insurance/commission uplift, so total ÷ invoice
    # alone OVER-estimates the rate (e.g. 91 for a genuine 63.6 THB rate). Deriving off
    # an invoice-only basis would then "correct" a correct rate to a wrong one, so we
    # compute it for reference but never flag/auto-correct on it unless it's complete.
    derived_rate = None
    basis_complete = False
    if declared and declared > 0:
        inv = _to_float(decl.get("invoice_price") or decl.get("Invoice Price"))
        frt = _to_float(decl.get("freight_value") or decl.get("Freight"))
        ins = _to_float(decl.get("insurance_value") or decl.get("Insurance"))
        adj = _to_float(decl.get("adjustment_value") or decl.get("Adjustment"))
        build_up = (frt or 0.0) + (ins or 0.0) + (adj or 0.0)
        basis = (inv or 0.0) + build_up
        if basis > 0:
            derived_rate = declared / basis
            basis_complete = build_up > 0  # freight/ins/adj actually supplied
        else:
            item_basis = 0.0
            for it in items or []:
                price = _to_float(it.get("invoice_unit_price") or it.get("Invoice unit price"))
                qty = _to_float(it.get("quantity") or it.get("Quantity (1)"))
                if price and qty:
                    item_basis += price * qty
            if item_basis > 0:
                derived_rate = declared / item_basis  # still invoice-only → not trustworthy

    if derived_rate is not None:
        derived_rate = round(derived_rate, 4)
    derived_trustworthy = derived_rate is not None and derived_rate > 0 and basis_complete

    # Fail-closed rules. Primary signal is the per-currency band (this is what catches
    # the real UAT failures: 500 for a USD doc, 636 for a THB doc). The math mismatch
    # is only a trigger when the derivation is trustworthy, so a correct CIF rate whose
    # invoice-only derivation looks off is NOT falsely flagged.
    if extracted_rate is None:
        suspect = derived_rate is not None
    elif not (_RATE_LO <= extracted_rate <= _RATE_HI):
        suspect = True                                   # implausible parse artefact
    elif band and not (band[0] <= extracted_rate <= band[1]):
        suspect = True                                   # out of its currency band
    elif derived_trustworthy:
        gap_pct = abs(extracted_rate - derived_rate) / derived_rate * 100
        suspect = gap_pct > rate_tolerance_pct()
    else:
        suspect = False                                  # in band, no trustworthy check

    # A directly-extracted rate that sits inside its currency band is a value read
    # straight off the doc (e.g. the printed "Exchange Rate (1) = 2100"). It must
    # win over any math-derived rate — the derivation can be thrown off by a
    # misread basis (e.g. adjustment_value parsed as the field's code "2" instead
    # of 44,612.82), which would otherwise "correct" a correct printed rate. The
    # workflow uses this to overwrite ONLY when the extracted rate is missing or
    # out-of-band (the genuine UAT failures: 500 for USD, 636 for THB).
    extracted_in_band = bool(
        extracted_rate is not None
        and _RATE_LO <= extracted_rate <= _RATE_HI
        and (band is None or band[0] <= extracted_rate <= band[1])
    )
    return {"rate_suspect": bool(suspect), "derived_rate": derived_rate,
            "derived_trustworthy": bool(derived_trustworthy),
            "extracted_rate": extracted_rate,
            "extracted_in_band": extracted_in_band}


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


def _document_check(items: List[Dict]) -> Dict:
    """Did every surviving item come off the declaration?

    A bundled release order carries an IMPORT LICENCE beside the CUSDEC, and a
    licence has its own goods table — same HS codes, same product names, licence
    quantities, and its own CIF total. When both reach the merge, the arithmetic
    can close on the WRONG document: on `0259100560` the licence's eleven lines
    summed to the licence's own total to within 456 kyat, while the declaration
    itself had four items. Item-sum cannot see that; both sides came from the
    same wrong page.

    `_src_doc` is stamped by `workflow._scope_items`, which drops foreign-document
    items outright when it can. This gate covers what it deliberately cannot: the
    case where no page was identified as the declaration (so there was nothing to
    scope to), and the case where the foreign lane was the ONLY source of items
    and was kept rather than leaving the job empty. In both, the rows are here on
    purpose and a human has to see them.

    Untagged items return `checked: False`. Every pre-`document` job, every
    reviewer-added row and every recovered slice is untagged, and treating
    "unstamped" as "foreign" would flag the entire corpus.

    NOT caught: a licence the classifier labelled DECLARATION. Its items are
    then stamped DECLARATION and this agrees with them. That is a limit of
    trusting the label, not of the arithmetic.
    """
    seen = {str(it.get("_src_doc")).upper() for it in (items or [])
            if it.get("_src_doc")}
    if not seen:
        return {"doc_checked": False, "doc_ok": True, "item_documents": []}
    foreign = sorted(d for d in seen if d not in ("DECLARATION", "UNKNOWN", "NONE"))
    return {"doc_checked": True,
            "doc_ok": not foreign,
            "item_documents": sorted(seen),
            "foreign_documents": foreign}


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
    rate = _rate_check(decl, declared, items)
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
            **rate,
        }

    gap = round(declared - isum, 2)
    gap_pct = round(abs(gap) / declared * 100, 2)
    # Tax-completeness: a MACCS declaration with a customs total but NO tax/fee
    # fields at all (CD/CT/AT/SF/MF) is an extraction miss, not a clean doc —
    # flag it and refuse "balanced" so value-balance alone cannot mask it.
    # Accept BOTH key spaces: the raw engine schema (Presto/Scribe emit
    # `customs_duty`, `commercial_tax`, …) and the DB-alias names the Phase-4
    # merge maps them onto. reconcile() runs on both — inside _call_typed/scribe
    # on the RAW dict, and again in Phase 4.4 on the merged one. Checking only
    # the aliases made taxes_missing=True for every pre-merge call, so the
    # engines' own math gate could never pass. (_duty_closure already does this.)
    # Only the CORE taxes count toward completeness: customs duty, commercial tax,
    # advance income tax. MACCS service fee (30000) and security fee (20000) are flat
    # fees almost always present, so an any()-over-all-keys gate passed even when every
    # core tax was dropped — an extraction miss that value-balance can't see. Require a
    # core tax present. Both key spaces (raw engine + DB alias), as _duty_closure does.
    _core_tax_keys = ("import_export_customs_duty", "customs_duty",
                      "commercial_tax_ct", "commercial_tax",
                      "advance_income_tax_at", "advance_income_tax")
    _core_taxes_present = any(_to_float(decl.get(k)) is not None for k in _core_tax_keys)
    taxes_missing = bool(declared) and declared > 0 and not _core_taxes_present
    # The CIF closure compares the INVOICE value against the ASSESSED customs value.
    # Customs routinely assesses above invoice — an uplift — so a large positive gap
    # is a normal declaration, not a misread. Measured on this corpus: a document
    # whose item sum matches its declared total EXACTLY (gap 0.0%) and whose tax
    # block is complete was still marked unbalanced on a 30.4% CIF gap, purely
    # because 481,406.664 x 67.2133 = 32.36M was assessed at 46.49M.
    #
    # Direction matters. Assessed ABOVE invoice is an uplift and is expected.
    # Assessed BELOW invoice is not, and still counts against the closure — that is
    # the shape a wrong exchange rate or a missed invoice produces.
    cif_uplift_only = (
        cif.get("cif_checked") and not cif.get("cif_ok")
        and (cif.get("cif_ratio") or 0) > 1.0        # declared exceeds computed
        and gap_pct <= tol                            # items still reconcile exactly
        and not taxes_missing
    )
    doc = _document_check(items)
    return {
        "checked": True,
        # Balanced requires the item-sum closure, the CIF closure, that the tax block
        # was captured, AND that the exchange rate survives the independent MATH
        # cross-check — a suspect rate forces downstream review.
        #
        # It also requires that the items came off the DECLARATION. Every other
        # clause here is arithmetic, and arithmetic is exactly what a licence
        # defeats: its own goods table sums to its own total, so a self-consistent
        # read of the wrong document passes all four. This is the only clause that
        # asks WHICH PAPER the numbers were read from.
        "balanced": ((gap_pct <= tol) and (cif["cif_ok"] or cif_uplift_only)
                     and not taxes_missing and not rate["rate_suspect"]
                     and doc["doc_ok"]),
        "cif_uplift_only": bool(cif_uplift_only),
        "taxes_missing": taxes_missing,
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
        **rate,
        **doc,
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
