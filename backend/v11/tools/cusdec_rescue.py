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
# Broadened (v2026.6.x) with generic CUSDEC markers so more declaration pages are
# caught; the ">= 2 markers" gate below keeps it precise (a licence page rarely
# carries two of these), so this doesn't false-positive on Appendix-4b pages.
_MARKERS = (
    "taxes and fees",
    "maccs service fee",
    "import/export customs duty",
    "release order",
    "cusdec",
    "customs declaration",
    "exchange rate",
)

# Per-currency plausibility band for the FX rate (invoice currency → MMK).
# A geometry- or regex-derived candidate outside its band is rejected outright,
# so a stray customs figure can never masquerade as the rate.
_RATE_BANDS = {
    "THB": (40.0, 90.0),
    "USD": (1500.0, 5000.0),
    "SGD": (1500.0, 2500.0),
    "CNY": (250.0, 600.0),
    "EUR": (2000.0, 5000.0),
    "JPY": (12.0, 40.0),
    "AUD": (1200.0, 3500.0),
}

# (declaration field, keyword phrases that identify the tax label row).
# Substring/keyword match — NOT exact — so real-world label variants are caught:
# "IMPORT DUTY", "COMMERCIAL TAX (5%)", "INCOME TAX (2%)", "SERVICE FEE" used to
# be silently dropped by exact matching → the UAT "not include 2%&5%/Duty"
# complaint. Keyword sets are mutually exclusive (duty / commercial / income /
# security / service), so no tax label matches two fields.
_TAX_MATCH = [
    ("import_export_customs_duty", ("customs duty", "import duty", "export duty", "import/export")),
    ("commercial_tax_ct",          ("commercial tax",)),
    ("advance_income_tax_at",      ("income tax",)),
    ("security_fee_sf",            ("security fee",)),
    ("maccs_service_fee_mf",       ("maccs service fee", "service fee")),
]

# Fields the CUSDEC is authoritative for (override licence-derived values).
# freight/insurance/adjustment are only set when the CUSDEC carries a real number
# (a "-" dash → None → left as-is, so a genuine value is never erased to blank).
_PREFER = (
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf", "total_customs_value",
    "exchange_rate", "declaration_no", "declaration_date",
    "freight_value", "insurance_value", "adjustment_value",
)


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except Exception:
        return None


def _is_value_slot(s) -> bool:
    """True if a line occupies a tax value slot — a real number OR a '-'/'—' dash
    (an explicitly-empty tax). Lets a dash claim the slot as None instead of
    letting the read fall through and steal the neighbouring tax's value."""
    t = str(s).strip()
    return t in ("-", "—", "–") or _num(t) is not None


# yyyy/mm/dd | yyyy-mm-dd | dd/mm/yyyy | dd-mm-yyyy
_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b|\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b")
# a numeric token that could be an FX rate: 2,100  65.0025  67.2133333  4200
_RATE_TOK = re.compile(r"^\(?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?$|^\(?\d{1,4}(?:\.\d+)?\)?$")


def _to_iso(s):
    """Normalise a matched date string to ISO yyyy-mm-dd, or None."""
    if not s:
        return None
    m = _DATE_RE.search(str(s))
    if not m:
        return None
    if m.group(1):  # yyyy-first
        y, mo, d = m.group(1), m.group(2), m.group(3)
    else:           # dd-first
        d, mo, y = m.group(4), m.group(5), m.group(6)
    try:
        y, mo, d = int(y), int(mo), int(d)
        if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
            return None
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None


def _rate_in_band(rate, currency) -> bool:
    """True if `rate` is plausible for `currency`. Unknown currency → a broad
    positive sanity window (1 < rate < 10000) so we still reject obvious junk."""
    if rate is None:
        return False
    band = _RATE_BANDS.get((currency or "").upper())
    if band:
        return band[0] <= rate <= band[1]
    return 1.0 < rate < 10000.0


def _row_band(words, y0, tol=4.0):
    """Words sharing a horizontal row (similar top-y), left-to-right."""
    band = [w for w in words if abs(w[1] - y0) <= tol]
    band.sort(key=lambda w: w[0])
    return band


def _geo_exchange_rate(page):
    """Geometry-anchored FX rate + invoice currency from the CUSDEC page.

    On the MACCS layout the label row reads:  ... Exchange Rate (1) THB - 65.0025
    so the value is the right-most numeric word in the *same row band* as the
    "Exchange"/"Rate" label — NOT line-stream order (whose neighbour is the
    MACCS service fee). Returns (rate|None, currency|None). Never raises.
    """
    try:
        words = page.get_text("words")
    except Exception:
        return None, None
    if not words:
        return None, None
    try:
        # anchor on the "Exchange" label, paired with a "Rate" word to its right
        for w in words:
            if w[4].lower() != "exchange":
                continue
            band = _row_band(words, w[1])
            rate_lbl = next((b for b in band
                             if b[4].lower() == "rate" and b[0] >= w[2] - 1), None)
            if rate_lbl is None:
                continue
            right = [b for b in band if b[0] > rate_lbl[2]]
            currency = next((b[4].upper() for b in right
                             if re.fullmatch(r"[A-Za-z]{3}", b[4])), None)
            # right-most numeric-looking token that parses to a number
            rate = None
            for b in reversed(right):
                if _RATE_TOK.match(b[4]):
                    v = _num(b[4].strip("()"))
                    if v is not None:
                        rate = v
                        break
            if rate is not None or currency is not None:
                return rate, currency
    except Exception:
        return None, None
    return None, None


def _geo_decl_date(page):
    """Geometry-anchored RO/ID (registration) date — the value in the
    "Declaration date" row, distinct from "Expected declaration date".
    Returns ISO yyyy-mm-dd or None. Never raises."""
    try:
        words = page.get_text("words")
    except Exception:
        return None
    if not words:
        return None
    # words that, when they precede "declaration", mark the WRONG (non-actual) date
    _GUARD = ("expected", "special", "estimated", "est", "exp", "planned",
              "intended", "provisional", "tentative")
    try:
        for w in words:
            if w[4].lower() != "declaration":
                continue
            band = _row_band(words, w[1])
            idx = band.index(w)
            # skip "Expected/Estimated/... declaration date" (guard word to the left)
            if idx > 0 and band[idx - 1][4].lower().strip(".:") in _GUARD:
                continue
            # require the immediately following word to be "date"
            if idx + 1 >= len(band) or band[idx + 1][4].lower() != "date":
                continue
            date_w = band[idx + 1]
            # (a) same-row: value printed inline to the right of "... date"
            for b in band[idx + 2:]:
                iso = _to_iso(b[4])
                if iso:
                    return iso
            # (b) column-below: MACCS often prints the value in the cell BELOW the
            # "Declaration date" header. Take the nearest date word roughly under
            # the label column (x overlaps the "declaration…date" span, y below).
            x0 = min(w[0], date_w[0])
            x1 = max(w[2], date_w[2])
            below = [b for b in words
                     if b[1] > w[3] - 1                       # strictly below the label row
                     and b[2] > x0 - 6 and b[0] < x1 + 6      # x-overlaps the label column
                     and _to_iso(b[4])]
            if below:
                below.sort(key=lambda b: (b[1], b[0]))        # nearest row first
                return _to_iso(below[0][4])
    except Exception:
        return None
    return None


def _text_currency(text: str):
    """Fallback currency read from the text layer (Exchange-Rate context)."""
    m = re.search(r"exchange\s*rate.{0,20}?\b([A-Z]{3})\b", text, re.I | re.S)
    if m and m.group(1).upper() in _RATE_BANDS:
        return m.group(1).upper()
    return None


def _is_cusdec(text: str) -> bool:
    tl = text.lower()
    return sum(m in tl for m in _MARKERS) >= 2


def _adj_num(lines, label):
    """Numeric value adjacent (prev or next line) to an exact-match label.
    Returns None when neither neighbour is numeric (e.g. a '-' dash)."""
    for i, l in enumerate(lines):
        if l == label:
            for cand in ((lines[i - 1] if i > 0 else None),
                         (lines[i + 1] if i + 1 < len(lines) else None)):
                v = _num(cand)
                if v is not None:
                    return v
            return None
    return None


def _parse(text: str, page=None) -> dict:
    """Pull the tax block + total/rate/date/decl-no from one CUSDEC page.

    The MACCS text layer extracts in a scrambled order, so each tax amount is
    taken as the numeric line immediately before (or after) its label line.
    When `page` is supplied the exchange rate and declaration date are resolved
    by page geometry (row-band anchoring), which is robust to that scrambling;
    text-layer heuristics are the fallback.
    """
    lines = [x.strip() for x in text.split("\n")]
    out: dict = {}
    for field, keywords in _TAX_MATCH:
        for i, line in enumerate(lines):
            ll = line.lower()
            if not any(k in ll for k in keywords):
                continue
            # MACCS prints value→label (scrambled), so the value is the line
            # BEFORE the label. A "-" dash there means this tax is absent → None
            # (do NOT fall through to the next line, which is another tax's value).
            # Only reach past the label when the line before isn't a value slot.
            prev_line = lines[i - 1] if i > 0 else ""
            nxt_line = lines[i + 1] if i + 1 < len(lines) else ""
            if _is_value_slot(prev_line):
                out[field] = _num(prev_line)          # dash → None
            elif _is_value_slot(nxt_line):
                out[field] = _num(nxt_line)
            else:
                out[field] = None
            break

    # Exchange rate + invoice currency — geometry first (anchored on the
    # "Exchange Rate" label row), then a band-filtered regex fallback.
    rate, currency = (None, None)
    if page is not None:
        rate, currency = _geo_exchange_rate(page)
    if currency is None:
        currency = _text_currency(text)
    if not _rate_in_band(rate, currency):
        rate = None  # geometry candidate failed its currency band → drop it
    if rate is None:
        # \d{1,4}\.\d{1,4} allows the 4-digit integer part real USD rates carry,
        # plus comma-grouped integers (e.g. "2,100"); band-filter picks the FX.
        cands = [_num(x) for x in re.findall(
            r"\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,4}\.\d{1,4})\b", text)]
        cands = [c for c in cands if _rate_in_band(c, currency)]
        rate = cands[0] if cands else None
    out["exchange_rate"] = rate
    if currency:
        out["currency"] = currency

    # Declaration / RO-ID (registration) date — geometry-anchored, ISO yyyy-mm-dd.
    ddate = _geo_decl_date(page) if page is not None else None
    if ddate is None:  # line-stream fallback (value printed before OR after label)
        for lbl in ("Declaration date", "Registration", "RO/ID Date", "Release order"):
            ddate = _label_date(lines, lbl)
            if ddate:
                break
    out["declaration_date"] = ddate

    # Total customs value (MMK): the largest 7+ digit decimal on the page.
    big = sorted({_num(x) for x in re.findall(r"\b[\d,]{7,}\.\d{2}\b", text) if _num(x)},
                 reverse=True)
    out["total_customs_value"] = big[0] if big else None
    # MACCS declaration number: 12 consecutive digits.
    dm = re.search(r"\b(\d{12})\b", text)
    out["declaration_no"] = dm.group(1) if dm else None
    # CIF build-up fields — captured only when the CUSDEC carries a real number
    # ("-" dash stays None). Adjustment "0" is an explicit zero (shown as 0, not —).
    lines = [x.strip() for x in text.split("\n")]
    out["freight_value"] = _adj_num(lines, "Freight")
    out["insurance_value"] = _adj_num(lines, "Insurance")
    out["adjustment_value"] = _adj_num(lines, "Adjustment")
    return out


def _before(lines, label):
    """Value on the line immediately before `label` (MACCS prints value→label)."""
    for i, l in enumerate(lines):
        if l.strip().lower() == label.lower():
            return lines[i - 1].strip() if i > 0 else None
    return None


def _label_date(lines, label):
    """ISO date adjacent to an EXACT-match label line — value BEFORE preferred
    (real MACCS order), else the line AFTER. Exact match is deliberate: it stops
    'Expected declaration date' from satisfying a 'Declaration date' read (the
    UAT #1 signature — the wrong date was the *expected* one)."""
    lab = label.strip().lower()
    for i, l in enumerate(lines):
        if l.strip().lower() != lab:
            continue
        for cand in ((lines[i - 1] if i > 0 else None),
                     (lines[i + 1] if i + 1 < len(lines) else None)):
            iso = _to_iso(cand)
            if iso:
                return iso
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
            return _parse(t, page=doc[pg])
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
                # Invoice price must follow the items: derive it from the CUSDEC
                # lines (Σ qty×unit) so the CIF closure uses the right basis
                # instead of a stray licence sub-value the LLM may have grabbed.
                inv = 0.0
                ok = True
                for it in cus_items:
                    q = _num(it.get("quantity"))
                    u = _num(it.get("invoice_unit_price"))
                    if q is None or u is None:
                        ok = False
                        break
                    inv += q * u
                if ok and inv > 0:
                    decl["invoice_price"] = round(inv, 2)
                used = True
    except Exception:
        pass

    return decl, items, used
