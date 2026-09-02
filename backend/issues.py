"""Job issue derivation — one plain-English list of everything wrong/missing on
an extraction, with the reason and what to do, shown in the review UI and the
Excel export so a user complaint can be traced to a reason instead of a blank cell.

Written for NON-TECHNICAL users: no jargon, no symbols, short sentences.

Derived from stored data (jobs/declarations/items), so it works for every past
job and every engine (Atlas V14, ROVER PRO, legacy) — no migration needed.

Each issue: {code, title, severity: error|warn|info, field, detail, cause, fix}
`title` is the user-facing name; `code` stays stable for machines.
"""
from typing import Any, Dict, List, Optional

import numeric


def _num(x: Any) -> Optional[float]:
    # An amount printed with its currency used to read as None here, which made
    # `build_issues` report a present value as missing.
    return numeric.to_float(x)


def _blank(v: Any) -> bool:
    return v is None or str(v).strip() in ("", "-", "—", "None", "null")


# Header fields worth flagging when empty:
# (db key, plain name, plain reason it is usually empty)
_HEADER_FIELDS = [
    ("consignor_name", "Sender (consignor)",
     "the paper itself shows a dash here, or a stamp covers it"),
    ("invoice_number", "Invoice number",
     "it is not printed on the main page of this document"),
    ("invoice_price", "Invoice amount",
     "the number is too blurry or handwritten, so we did not guess"),
    ("exchange_rate", "Exchange rate",
     "the rate line is blank on the paper"),
    ("total_customs_value", "Total value",
     "the total is too blurry to read, so we did not guess"),
    ("freight_value", "Freight cost",
     "most of these documents leave this blank (just a dash)"),
    ("insurance_value", "Insurance cost",
     "most of these documents leave this blank (just a dash)"),
    ("import_export_customs_duty", "Customs duty",
     "the tax table is stamped over or on a page we could not read"),
    ("declaration_no", "Declaration number",
     "the number is hard to read — we never guess this one"),
    ("release_order_date", "Release order date",
     "customs has not stamped the release decision yet, or the decision page is missing from the PDF"),
]


def build_issues(job: Dict, decl: Optional[Dict], items: List[Dict]) -> List[Dict]:
    issues: List[Dict] = []
    decl = decl or {}
    items = [i for i in (items or []) if not i.get("is_deleted")]

    # ── 1. Item-sum gap — the big one behind "Excel totals are wrong" ──
    total = _num(decl.get("total_customs_value"))
    item_sum = sum(v for v in (_num(i.get("customs_value_mmk")) for i in items) if v is not None)
    if total and total > 0:
        gap = total - item_sum
        gap_pct = abs(gap) / total * 100
        if gap_pct > 5:
            # A shortfall whose ratio IS the exchange rate is not a missing-products problem:
            # every row was read, but the invoice-currency amount landed in the MMK column.
            # Reported as "N% of the list was not read", it sends a reviewer hunting for rows
            # that are all present. Name the real fault instead.
            rate = _num(decl.get("exchange_rate"))
            unit_mismatch = bool(
                rate and rate > 1 and item_sum > 0
                and abs(item_sum * rate - total) / total * 100 <= 2
            )
            if unit_mismatch:
                issues.append({
                    "code": "ITEM_VALUE_WRONG_UNIT",
                    "title": "Product values are in the wrong currency",
                    "severity": "error", "field": "product_items",
                    "detail": f"All {len(items)} products were read, but their values add up to "
                              f"{item_sum:,.0f} while the paper says {total:,.0f} MMK — exactly "
                              f"{rate:g} times smaller, which is the exchange rate on this "
                              f"document. The product values were stored in the invoice currency "
                              f"instead of kyats.",
                    "cause": "the invoice-currency 'Item value' was saved into the kyat column "
                             "instead of the assessed 'Customs value'",
                    "fix": "no products are missing — do not retype them. Re-run this document; "
                           "if it repeats, report it as a system fault",
                })
            else:
                issues.append({
                    "code": "ITEM_SUM_GAP", "title": "Products do not add up to the total",
                    "severity": "error", "field": "product_items",
                    "detail": f"The products we found add up to {item_sum:,.0f} MMK, "
                              f"but the paper says the total is {total:,.0f} MMK. "
                              f"About {gap_pct:.0f}% of the product list was not read.",
                    "cause": "the product pages are handwritten, stamped over, or too blurry to read",
                    "fix": "run this document again, or type in the missing products, before approving",
                })
    elif total is None:
        issues.append({
            "code": "NO_DECLARED_TOTAL", "title": "Total amount not found",
            "severity": "error", "field": "total_customs_value",
            "detail": "We could not read the total amount on the paper, "
                      "so we cannot check if the products add up.",
            "cause": "the total is too blurry to read, or hidden under a stamp",
            "fix": "look at the PDF, type the total into the Total value box",
        })

    # ── 2. No items at all ──
    if not items:
        issues.append({
            "code": "NO_ITEMS", "title": "No products found",
            "severity": "error", "field": "product_items",
            "detail": "We could not read any product lines from this document.",
            "cause": "the product list is handwritten or the scan is too poor to read",
            "fix": "run this document again, or type the products in by hand",
        })

    # ── 3. Tax block completeness ──
    # Three of these named columns that do not exist — the DB has `advance_income_tax_at`,
    # `security_fee_sf`, `maccs_service_fee_mf`. `decl.get()` returned None for all three,
    # so they always counted as blank and this check has been running on two of five
    # taxes since it was written. Both spellings are accepted because the raw engine
    # dicts use the short names before the Phase-4 merge renames them.
    taxes = [decl.get(k) or decl.get(a) for k, a in (
        ("import_export_customs_duty", "customs_duty"),
        ("commercial_tax_ct", "commercial_tax"),
        ("advance_income_tax_at", "advance_income_tax"),
        ("security_fee_sf", "security_fee"),
        ("maccs_service_fee_mf", "maccs_service_fee"))]
    if total and all(_blank(t) for t in taxes):
        issues.append({
            "code": "TAXES_MISSING", "title": "All tax amounts are missing",
            "severity": "error", "field": "taxes",
            "detail": "The total was read, but none of the 5 tax amounts "
                      "(duty, commercial tax, income tax, fees) could be read.",
            "cause": "the tax table is stamped over, or sits on a page we could not read",
            "fix": "copy the tax numbers from the page with the PASS stamp",
        })

    # ── 4. Empty header fields (warn each, with its usual plain reason) ──
    for key, label, cause in _HEADER_FIELDS:
        if key == "total_customs_value" and total is None:
            continue  # already covered above
        if _blank(decl.get(key)):
            issues.append({
                "code": "FIELD_EMPTY", "title": f"{label} is empty",
                "severity": "warn", "field": key,
                "detail": f"{label} has no value, so this box is also empty in the Excel file. "
                          "This is usually normal, not a mistake.",
                "cause": cause,
                "fix": "look at the PDF — if you can see a value there, click the box and type it in",
            })

    # ── 5. Item-level holes ──
    if items:
        n = len(items)
        no_hs = sum(1 for i in items if _blank(i.get("hs_code")))
        no_price = sum(1 for i in items if _blank(i.get("invoice_unit_price")))
        no_qty = sum(1 for i in items if _blank(i.get("quantity")))
        if no_hs:
            issues.append({
                "code": "ITEMS_NO_HS", "title": "Some products have no HS code",
                "severity": "warn", "field": "hs_code",
                "detail": f"{no_hs} of {n} products are missing the HS code.",
                "cause": "the HS column is squeezed or handwritten in the table",
                "fix": "copy the codes from the product table in the PDF",
            })
        if no_price:
            issues.append({
                "code": "ITEMS_NO_PRICE", "title": "Some products have no unit price",
                "severity": "warn", "field": "invoice_unit_price",
                "detail": f"{no_price} of {n} products are missing the price per unit.",
                "cause": "the paper only shows line totals, not a price per unit",
                "fix": "divide the line total by the quantity, or copy from the invoice",
            })
        if no_qty:
            issues.append({
                "code": "ITEMS_NO_QTY", "title": "Some products have no quantity",
                "severity": "warn", "field": "quantity",
                "detail": f"{no_qty} of {n} products are missing the quantity.",
                "cause": "the quantity is mixed into the product name or handwritten",
                "fix": "copy the quantity from the product table in the PDF",
            })

    # ── 6. Verification / accuracy signals ──
    if job.get("cross_val_passed") in (0, False) or decl.get("cross_val_passed") in (0, False):
        issues.append({
            "code": "CROSS_VAL_FAILED", "title": "The numbers do not check out",
            "severity": "warn", "field": "verification",
            "detail": "Our automatic math check found numbers that do not match each other, "
                      "so this document was sent to you instead of being approved by itself.",
            "cause": "one or more of the problems listed above",
            "fix": "fix the red problems above first, then approve",
        })
    acc = _num(job.get("accuracy_percent"))
    if acc is not None and acc < 90:
        issues.append({
            "code": "LOW_CONFIDENCE", "title": "A person needs to check this one",
            "severity": "info", "field": "accuracy",
            "detail": f"We are {acc:.0f}% sure about this document. "
                      "Below 90% we always ask a person to look at it.",
            "cause": "some boxes above are empty or could not be double-checked",
            "fix": "nothing is broken — just check the points above and approve",
        })

    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda x: order.get(x["severity"], 3))
    return issues
