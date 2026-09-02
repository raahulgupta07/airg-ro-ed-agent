"""Tier 0.5 — geometry-anchored deterministic read of the CUSDEC money block. $0, no LLM.

`deterministic.py` scans the text layer as a flat list of LINES. On these MACCS
CUSDECs the text layer is column-scrambled, so a label and its value land several
lines apart and line-adjacency guesses wrong (this is the old 636.2576-vs-67.21
exchange-rate failure class).

This module instead uses the WORD COORDINATES: find the label's rectangle, collect
every word whose vertical centre sits in the same row band, and take the first
numeric token to the RIGHT of the label. That is how the form is actually laid out,
so the binding is exact rather than inferred:

    (09) Adjustment value AD - USD - 44,612.82   ->  adjustment_value = 44612.82
    (10) Total customs value      198,450,000    ->  total_customs_value = 198450000

Verified against the 16-doc UAT corpus: on all 5 documents whose CUSDEC page carries
a text layer, the CIF identity (FC + freight + insurance + adjustment) x rate ==
printed total closes to +0.00%. The other 11 have a scanned CUSDEC page and return
empty cells here — vision owns those, unchanged.

Fail-safe by contract: never raises, returns empty Cells when anything is missing.
"""
import re
from typing import Dict, List, Optional

try:
    import fitz  # PyMuPDF
except Exception:                                    # pragma: no cover
    fitz = None

from .schema import Cell

# A customs declaration id: 12 digits starting 100. Deliberately NOT \d{11,12} —
# that also matches Myanmar mobile numbers (09xxxxxxxxx), which appear on the
# consignee/broker blocks of most of these bundles.
_DECL_ID = re.compile(r"\b100\d{9}\b")
_MA_ID = re.compile(r"\bMA\s*\d{4}\s*[/-]?\s*\d{6}\b", re.I)

_NUM_TOKEN = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_CCY = re.compile(r"\b(THB|USD|SGD|CNY|EUR|JPY|AUD)\b")

# Currency -> plausible MMK-per-unit band (mirrors deterministic._RATE_BANDS and
# supervisor.BANDS — a printed rate outside its band is a mis-bind, not a rate).
_RATE_BANDS = {
    "THB": (40, 90), "USD": (1500, 5000), "SGD": (1500, 2500),
    "CNY": (250, 600), "EUR": (2000, 5000), "JPY": (12, 40), "AUD": (1200, 3500),
}

# column -> the label(s) printed on the form, most specific first.
_LABELS = {
    "invoice_price_fc": ["Invoice price"],
    "freight_value": ["Freight"],
    "insurance_value": ["Insurance"],
    "adjustment_value": ["Adjustment value"],
    "total_customs_value": ["Total customs value"],
    "import_export_customs_duty": ["CUSTOMS DUTY", "Customs Duty"],
    "commercial_tax_ct": ["COMMERCIAL TAX", "Commercial Tax"],
    "advance_income_tax_at": ["ADVANCED INCOME TAX", "ADVANCE INCOME TAX",
                              "Advanced Income Tax"],
    # "SECURITY FEE" in full, never bare "SECURITY": the right-hand exemption
    # column prints its own "Security" row (a deferral amount, usually 0) ABOVE
    # the real 'SF SECURITY FEE' line, so a bare-word search binds the wrong
    # column and reports 0 for a fee that was actually charged.
    "security_fee_sf": ["SECURITY FEE"],
    "maccs_service_fee_mf": ["MACCS SERVICE FEE"],
    "exemption_reduction": ["Exemption/Reduction"],
}

_ROW_TOL = 5.0          # points; a CUSDEC row is ~10pt tall
_CONF_BOUND = 0.97      # >= 0.95 so supervisor.merge lets a bound value beat vision
_CONF_WEAK = 0.85       # bound but failed its plausibility check -> fill-only


def _tonum(token: str) -> Optional[float]:
    t = token.strip().rstrip("*").rstrip(":")
    if not _NUM_TOKEN.match(t):
        return None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def _row_words(words, rect, tol: float = _ROW_TOL):
    """Words sharing the label's row band, left to right."""
    cy = (rect.y0 + rect.y1) / 2
    band = [w for w in words if abs((w[1] + w[3]) / 2 - cy) < tol]
    band.sort(key=lambda w: w[0])
    return band


def _value_right_of(words, rect) -> Optional[tuple]:
    """First numeric token to the RIGHT of the label on its row. Returns
    (value, row_text, value_bbox, row_bbox) or None. Parenthesised tokens like
    '(1)' are field-index markers on this form, never values.

    The two rects are the whole point of binding by coordinate rather than by
    line adjacency: we already know exactly which words on the page produced the
    number, so the reviewer can be shown that spot instead of the whole page.

    The row is clipped to LABEL -> VALUE, not the full band. This form prints two
    label/value columns side by side, so one physical row carries two unrelated
    fields: the band holding 'Exchange Rate (1) THB - 67.2133333' also holds
    'AT ADVANCED INCOME TAX 929,743'. A crop of the whole band would show the
    reviewer two labels and two numbers and ask them to confirm one."""
    band = _row_words(words, rect)
    for i, w in enumerate(band):
        if w[0] < rect.x1 - 1:
            continue
        tok = w[4]
        if tok.startswith("(") or tok.endswith(")"):
            continue
        v = _tonum(tok)
        if v is None:
            continue
        span = [x for x in band[:i + 1] if x[2] > rect.x0 - 1]
        return (v, " ".join(x[4] for x in span),
                [float(w[0]), float(w[1]), float(w[2]), float(w[3])],
                _union(span))
    return None


def _union(band) -> Optional[List[float]]:
    """Bounding box spanning a row of words, [x0, y0, x1, y1] in PDF points."""
    if not band:
        return None
    return [float(min(w[0] for w in band)), float(min(w[1] for w in band)),
            float(max(w[2] for w in band)), float(max(w[3] for w in band))]


def cusdec_page_index(doc) -> Optional[int]:
    """The assessment page — the one printing 'Total customs value'. That single
    anchor is what keeps tax labels from binding to rows on invoice/packing pages."""
    for i in range(len(doc)):
        if "Total customs value" in (doc[i].get_text() or ""):
            return i
    return None


def declaration_ids(pdf_path: str) -> List[str]:
    """Every customs declaration id printed anywhere in the file, in page order.

    More than one means the PDF is a BUNDLE of separate declarations. That is a
    real case in the corpus: 100313488550.pdf also carries 100319576711, and the
    engine happily read the second declaration's money block while filing it under
    the first one's number — every value right, wrong document identity.
    """
    if fitz is None:
        return []
    ids: List[str] = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    try:
        for i in range(len(doc)):
            txt = doc[i].get_text() or ""
            for m in _DECL_ID.findall(txt):
                if m not in ids:
                    ids.append(m)
            for m in _MA_ID.findall(txt):
                v = re.sub(r"\s+", "", m).upper()
                if v not in ids:
                    ids.append(v)
    finally:
        doc.close()
    return ids


def _currency_on(words, rect) -> Optional[str]:
    row = " ".join(w[4] for w in _row_words(words, rect)).upper()
    m = _CCY.search(row)
    return m.group(1) if m else None


def _plausible(col: str, val: float, ctx: Dict[str, float]) -> bool:
    """Per-field sanity. A bound-but-implausible number is kept as fill-only
    (low confidence) rather than trusted over vision."""
    if col == "exchange_rate":
        band = _RATE_BANDS.get(ctx.get("_ccy")) or (12, 5000)
        return band[0] <= val <= band[1]
    if col in ("total_customs_value", "invoice_price_fc"):
        return val > 0
    if col in ("import_export_customs_duty", "commercial_tax_ct",
               "advance_income_tax_at", "security_fee_sf", "maccs_service_fee_mf",
               "exemption_reduction"):
        total = ctx.get("total_customs_value")
        # A tax larger than the customs value it is assessed on is a mis-bind.
        return val >= 0 and (total is None or val <= total)
    return True


def _exchange_rate(page, words) -> Optional[Cell]:
    """The printed 'Exchange Rate (1) <CCY> - <n>' row. Read verbatim, never derived."""
    for rect in page.search_for("Exchange Rate"):
        got = _value_right_of(words, rect)
        if not got:
            continue
        val, row, vbox, rbox = got
        ccy = _currency_on(words, rect)
        band = _RATE_BANDS.get(ccy) or (12, 5000)
        if not (band[0] <= val <= band[1]):
            continue
        c = Cell(column="exchange_rate", value=val, model="geometry",
                 confidence=_CONF_BOUND, status="ok",
                 source=f"p{page.number + 1} row: {row[:120]}",
                 page=page.number + 1, bbox=vbox, row_bbox=rbox)
        if ccy:
            c.note = f"{ccy} rate (band {band[0]}-{band[1]})"
        return c
    return None


def read(pdf_path: str) -> Dict[str, Cell]:
    """Geometry-bound money cells from the CUSDEC page. {} when the page has no
    text layer (scanned) or the file cannot be opened — vision then owns the field."""
    out: Dict[str, Cell] = {}
    if fitz is None:
        return out
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return out
    try:
        ci = cusdec_page_index(doc)
        if ci is None:
            return out
        page = doc[ci]
        words = page.get_text("words")
        if not words:
            return out

        rate = _exchange_rate(page, words)
        if rate is not None:
            out["exchange_rate"] = rate

        ctx: Dict[str, float] = {"_ccy": None}
        for rect in page.search_for("Exchange Rate"):
            ctx["_ccy"] = _currency_on(words, rect)
            break

        # Total first — the tax plausibility check is relative to it.
        order = ["total_customs_value"] + [k for k in _LABELS if k != "total_customs_value"]
        for col in order:
            for label in _LABELS[col]:
                bound = None
                for rect in page.search_for(label):
                    got = _value_right_of(words, rect)
                    if got:
                        bound = got
                        break
                if bound is None:
                    continue
                val, row, vbox, rbox = bound
                ok = _plausible(col, val, ctx)
                c = Cell(column=col, value=val, model="geometry",
                         confidence=_CONF_BOUND if ok else _CONF_WEAK,
                         status="ok",
                         source=f"p{ci + 1} row: {row[:120]}",
                         page=ci + 1, bbox=vbox, row_bbox=rbox)
                if not ok:
                    c.note = "geometry-bound but failed plausibility — fill-only"
                out[col] = c
                if col == "total_customs_value":
                    ctx["total_customs_value"] = val
                break
        return out
    except Exception:
        return out
    finally:
        try:
            doc.close()
        except Exception:
            pass


def cif_identity(cells: Dict[str, Cell]) -> Optional[dict]:
    """(FC + freight + insurance + adjustment) x rate  vs  printed total.

    Closes to +0.00% on every text-layer CUSDEC in the corpus, WITH the adjustment
    included — the uplift is printed on the form and is load-bearing. Bare FC x rate
    is wrong on 4 of the 5. Returned for diagnostics/cross-check; this module never
    overwrites a printed value with a computed one.
    """
    def v(k):
        c = cells.get(k)
        return c.value if c is not None and isinstance(c.value, (int, float)) else None

    fc, rate, total = v("invoice_price_fc"), v("exchange_rate"), v("total_customs_value")
    if not (fc and rate and total):
        return None
    frt = v("freight_value") or 0.0
    ins = v("insurance_value") or 0.0
    adj = v("adjustment_value") or 0.0
    expected = (fc + frt + ins + adj) * rate
    return {"expected": expected, "printed": total,
            "gap_pct": (expected - total) / total if total else None,
            "bare": fc * rate}
