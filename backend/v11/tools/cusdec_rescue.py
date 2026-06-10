"""Deterministic MACCS-CUSDEC tax/total rescue.

Bundled release-order PDFs carry both Import-Licence (Appendix 4b) pages and the
authoritative MACCS customs declaration (CUSDEC). The LLM tends to anchor the
header on the licence pages, which lack the tax block — so duty/CT/AT/SF/MF and
the real total go missing. This reads the CUSDEC text layer directly and fills
those fields (CUSDEC wins, it is the legal source).

All parsing is deterministic over the page text layer; never raises.
"""
import re

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

# A page is the MACCS CUSDEC when it carries the tax block + release-order markers.
_MARKERS = (
    "taxes and fees",
    "maccs service fee",
    "import/export customs duty",
    "release order",
)

# (declaration field, label as printed on the CUSDEC tax block)
_TAXES = [
    ("import_export_customs_duty", "IMPORT/EXPORT CUSTOMS DUTY"),
    ("commercial_tax_ct",          "COMMERCIAL TAX"),
    ("advance_income_tax_at",      "ADVANCED INCOME TAX"),
    ("security_fee_sf",            "SECURITY FEE"),
    ("maccs_service_fee_mf",       "MACCS SERVICE FEE"),
]

# Fields the CUSDEC is authoritative for (override licence-derived values).
_PREFER = (
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf", "total_customs_value",
    "exchange_rate", "declaration_no",
)


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _is_cusdec(text: str) -> bool:
    tl = text.lower()
    return sum(m in tl for m in _MARKERS) >= 2


def _parse(text: str) -> dict:
    """Pull the tax block + total/rate/decl-no from one CUSDEC page's text.

    The MACCS text layer extracts in a scrambled order, so each tax amount is
    taken as the numeric line immediately before (or after) its label line.
    """
    lines = [x.strip() for x in text.split("\n")]
    out: dict = {}
    for field, name in _TAXES:
        for i, line in enumerate(lines):
            if line.upper() == name:
                prev = _num(lines[i - 1]) if i > 0 else None
                nxt = _num(lines[i + 1]) if i + 1 < len(lines) else None
                out[field] = prev if prev is not None else nxt
                break
    # Exchange rate: a small decimal (1 < x < 10000) — the THB→MMK rate.
    rates = [float(x) for x in re.findall(r"\b(\d{1,3}\.\d{2,4})\b", text)
             if 1 < float(x) < 10000]
    out["exchange_rate"] = rates[0] if rates else None
    # Total customs value (MMK): the largest 7+ digit decimal on the page.
    big = sorted({_num(x) for x in re.findall(r"\b[\d,]{7,}\.\d{2}\b", text) if _num(x)},
                 reverse=True)
    out["total_customs_value"] = big[0] if big else None
    # MACCS declaration number: 12 consecutive digits.
    dm = re.search(r"\b(\d{12})\b", text)
    out["declaration_no"] = dm.group(1) if dm else None
    return out


def _before(lines, label):
    """Value on the line immediately before `label` (MACCS prints value→label)."""
    for i, l in enumerate(lines):
        if l.strip().lower() == label.lower():
            return lines[i - 1].strip() if i > 0 else None
    return None


def _is_item_page(text: str) -> bool:
    tl = text.lower()
    return "item name" in tl and "quantity (1)" in tl and "customs value" in tl


def _parse_item(text: str) -> dict:
    L = [x.strip() for x in text.split("\n")]
    hs = _before(L, "HS")
    hs = re.sub(r"[^\d]", "", hs) if hs else None  # 0406.30.00 00 -> 0406300000
    return {
        "item_no": _before(L, "No."),
        "item_name": _before(L, "Item name"),
        "hs_code": hs,
        "quantity": _num(_before(L, "Quantity (1)")),
        "invoice_unit_price": _num(_before(L, "Invoice unit price")),
        "customs_value_mmk": _num(_before(L, "Customs value")),
    }


def cusdec_items(pdf_path: str):
    """All consolidated item lines from the CUSDEC item-detail pages, or []."""
    if not fitz or not pdf_path:
        return []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    out = []
    for pg in range(doc.page_count):
        try:
            t = doc[pg].get_text()
        except Exception:
            continue
        if _is_item_page(t):
            it = _parse_item(t)
            if it.get("customs_value_mmk") is not None:
                out.append(it)
    return out


def cusdec_fields(pdf_path: str):
    """Authoritative CUSDEC fields from the text layer, or None if no CUSDEC page."""
    if not fitz or not pdf_path:
        return None
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return None
    for pg in range(doc.page_count):
        try:
            t = doc[pg].get_text()
        except Exception:
            continue
        if _is_cusdec(t):
            return _parse(t)
    return None


def apply_cusdec(decl: dict, pdf_path: str, items=None):
    """Fill `decl` with CUSDEC header values (total/taxes/rate/decl_no), and —
    when the CUSDEC item lines self-reconcile against that total but the current
    `items` do NOT — replace `items` with the authoritative CUSDEC items.

    Returns (decl, items, used: bool). `items` is returned unchanged (or as
    passed, possibly None) when no item rescue applies. Never raises.
    """
    used = False
    try:
        fields = cusdec_fields(pdf_path)
    except Exception:
        fields = None
    if fields:
        for k in _PREFER:
            if fields.get(k) is not None:
                decl[k] = fields[k]
        used = True

    # Item rescue — only when CUSDEC items reconcile with the (now authoritative)
    # total and the current items don't, so we never clobber a good extraction.
    try:
        total = _num(decl.get("total_customs_value"))
        cus_items = cusdec_items(pdf_path)
        if items is not None and total and total > 0 and cus_items:
            cur_sum = sum(_num(i.get("customs_value_mmk")) or 0 for i in items)
            cus_sum = sum(_num(i.get("customs_value_mmk")) or 0 for i in cus_items)
            cus_ok = abs(cus_sum - total) / total <= 0.05
            cur_ok = bool(items) and abs(cur_sum - total) / total <= 0.05
            if cus_ok and not cur_ok:
                items = cus_items
                used = True
    except Exception:
        pass

    return decl, items, used
