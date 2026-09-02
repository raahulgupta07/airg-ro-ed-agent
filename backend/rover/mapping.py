"""Mapping layer — fleet record -> the accountant's 'AI results' Excel schema.
Extraction stays doc-faithful; this layer only renames/derives/normalizes so the
row drops straight into their workbook with no manual reconciliation.

Handles the format quirks proven during testing:
  * invoice number split into customs ('A - <n>') and commercial ('<n>') forms
  * SECURITY and MACCS kept as separate columns (never collapsed)
  * TOTAL IMPORT derived = duty + CT + AT + security + MACCS
  * a value-consistency note when the doc's assessed CUSTOMS VALUE differs from the
    invoice-price MMK (the field the manual sheet sometimes books instead)
"""
import re
from typing import Dict

import numeric

# Order matches the 'AI results' sheet header (plus derived + audit columns).
SCHEMA = [
    "DECLARATION NO", "DATE", "IMPORTER",
    "INVOICE NUMBER", "INVOICE NUMBER (CUSTOM)", "INVOICE NUMBER (COMMERCIAL)",
    "CURRENCY", "EXCHANGE RATE",
    "INVOICE PRICE (MMK)", "CUSTOMS VALUE",
    "DUTY", "TAX", "INCOME TAX", "SECURITY", "MACCS", "EXEMPTION/REDUCTION",
    "TOTAL IMPORT", "REVIEW", "NOTE",
]


def _num(v):
    return numeric.to_float(v)


def _core_invoice(v):
    """Strip a leading 'A-', 'A - ', 'INV-' etc. to the core invoice number."""
    s = str(v or "").strip()
    return re.sub(r"^(A\s*-\s*|INV\s*-\s*)", "", s, flags=re.IGNORECASE).strip()


def to_accountant_row(result: Dict) -> Dict[str, object]:
    v = result.get("values", {})
    core = _core_invoice(v.get("invoice_number"))

    duty = _num(v.get("import_export_customs_duty")) or 0
    ct = _num(v.get("commercial_tax_ct")) or 0
    at = _num(v.get("advance_income_tax_at")) or 0
    sf = _num(v.get("security_fee_sf")) or 0
    mf = _num(v.get("maccs_service_fee_mf")) or 0
    total_import = duty + ct + at + sf + mf

    inv_mmk = _num(v.get("invoice_price_mmk"))
    cust = _num(v.get("total_customs_value"))
    note = []
    if inv_mmk is not None and cust is not None and abs(inv_mmk - cust) > 0.02 * max(cust, 1):
        note.append("invoice-price ≠ assessed customs value (uplift) — confirm which the ledger books")
    if result.get("suspect"):
        note.append("suspect: " + ",".join(result["suspect"]))

    return {
        "DECLARATION NO": v.get("declaration_no"),
        "DATE": v.get("declaration_date"),
        "IMPORTER": v.get("importer_name"),
        "INVOICE NUMBER": core,
        "INVOICE NUMBER (CUSTOM)": f"A - {core}" if core else None,
        "INVOICE NUMBER (COMMERCIAL)": core or None,
        "CURRENCY": v.get("currency"),
        "EXCHANGE RATE": _num(v.get("exchange_rate")),
        "INVOICE PRICE (MMK)": inv_mmk,
        "CUSTOMS VALUE": cust,
        "DUTY": duty or None,
        "TAX": ct,
        "INCOME TAX": at or None,
        "SECURITY": sf,
        "MACCS": mf or None,
        "EXEMPTION/REDUCTION": _num(v.get("exemption_reduction")) or 0,
        "TOTAL IMPORT": round(total_import, 2) if total_import else None,
        "REVIEW": "REVIEW" if result.get("needs_review") else "clean",
        "NOTE": " | ".join(note),
    }
