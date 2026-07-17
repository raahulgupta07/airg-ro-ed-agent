"""Deterministic CUSDEC date + tax-label extraction — the UAT #1 (Wrong Date)
and "not include 2%&5%/Duty" complaint classes.

No PDFs needed: exercises the pure text/line helpers with synthetic CUSDEC
snippets that reproduce the real failure layouts (Expected-vs-actual date,
column-below date, tax labels printed as "INCOME TAX (2%)" etc.).
"""
from v11.tools import cusdec_rescue as cr


# ---- tax-label tolerance (missing 2%&5%/Duty) --------------------------------

def test_taxes_exact_labels():
    # Real MACCS text order is value-then-label (scrambled) → prev-preferred.
    text = "\n".join([
        "1394615", "IMPORT/EXPORT CUSTOMS DUTY",
        "2394089", "COMMERCIAL TAX",
        "929743",  "ADVANCED INCOME TAX",
        "20000",   "SECURITY FEE",
        "30000",   "MACCS SERVICE FEE",
    ])
    out = cr._parse(text, page=None)
    assert out["import_export_customs_duty"] == 1394615
    assert out["commercial_tax_ct"] == 2394089
    assert out["advance_income_tax_at"] == 929743
    assert out["security_fee_sf"] == 20000
    assert out["maccs_service_fee_mf"] == 30000


def test_taxes_variant_labels():
    # Real forms print percentages / short forms; exact-match used to DROP these
    # (the "not include 2%&5%/Duty" UAT complaint). Tolerant matching must catch them.
    text = "\n".join([
        "1000",  "IMPORT DUTY",
        "5000",  "COMMERCIAL TAX (5%)",
        "2000",  "INCOME TAX (2%)",       # NOT "ADVANCED INCOME TAX"
        "20000", "SECURITY FEE",
        "30000", "SERVICE FEE",           # NOT "MACCS SERVICE FEE"
    ])
    out = cr._parse(text, page=None)
    assert out["import_export_customs_duty"] == 1000, "duty label variant dropped"
    assert out["commercial_tax_ct"] == 5000, "5% CT label variant dropped"
    assert out["advance_income_tax_at"] == 2000, "2% income tax label variant dropped"
    assert out["security_fee_sf"] == 20000
    assert out["maccs_service_fee_mf"] == 30000


def test_taxes_no_cross_contamination():
    # "COMMERCIAL TAX" keyword must not swallow "INCOME TAX"; distinct values.
    text = "\n".join(["5000", "COMMERCIAL TAX (5%)", "2000", "INCOME TAX (2%)"])
    out = cr._parse(text, page=None)
    assert out["commercial_tax_ct"] == 5000
    assert out["advance_income_tax_at"] == 2000


def test_dash_tax_is_none():
    text = "\n".join(["-", "COMMERCIAL TAX", "20000", "SECURITY FEE"])
    out = cr._parse(text, page=None)
    assert out["commercial_tax_ct"] is None
    assert out["security_fee_sf"] == 20000


# ---- line-stream date fallback (no page geometry) ----------------------------

def test_line_date_value_before_label():
    text = "2025-06-29\nDeclaration date\n"
    out = cr._parse(text, page=None)
    assert out["declaration_date"] == "2025-06-29"


def test_line_date_value_after_label():
    # MACCS sometimes prints label then value on the next line.
    text = "Declaration date\n2025-06-29\n"
    out = cr._parse(text, page=None)
    assert out["declaration_date"] == "2025-06-29"


def test_line_date_ignores_expected():
    # "Expected declaration date" must NOT satisfy the "Declaration date" read.
    text = "\n".join([
        "Expected declaration date", "2025-06-25",   # the WRONG date (UAT signature)
        "2025-06-29", "Declaration date",            # the RIGHT date
    ])
    out = cr._parse(text, page=None)
    assert out["declaration_date"] == "2025-06-29", "grabbed Expected date"


# ---- geometry-anchored reads (FakePage — no PDF/fitz needed) -----------------

class _FakePage:
    """Minimal fitz-page stand-in: get_text('words') → list of
    (x0, y0, x1, y1, text, block, line, word_no) tuples."""
    def __init__(self, words):
        self._words = words

    def get_text(self, kind):
        assert kind == "words"
        return self._words


def _w(x0, y0, x1, y1, t):
    return (x0, y0, x1, y1, t, 0, 0, 0)


def test_geo_decl_date_column_below():
    # "Declaration date" header with the value in the CELL BELOW (MACCS grid).
    page = _FakePage([
        _w(10, 100, 60, 110, "Declaration"),
        _w(62, 100, 90, 110, "date"),
        _w(12, 115, 80, 125, "2025-06-29"),   # directly under the label column
    ])
    assert cr._geo_decl_date(page) == "2025-06-29"


def test_geo_decl_date_skips_expected_row():
    # First "declaration" is under "Expected" → guard must skip it and take the
    # real "Declaration date" row's value.
    page = _FakePage([
        _w(5,  60,  40, 70,  "Expected"),
        _w(42, 60,  92, 70,  "declaration"),
        _w(94, 60,  120, 70, "date"),
        _w(12, 75,  80, 85,  "2025-06-25"),    # WRONG (expected) date
        _w(10, 100, 60, 110, "Declaration"),
        _w(62, 100, 90, 110, "date"),
        _w(12, 115, 80, 125, "2025-06-29"),    # RIGHT date
    ])
    assert cr._geo_decl_date(page) == "2025-06-29"


def test_geo_exchange_rate_row_band():
    # "Exchange Rate (1) THB - 65.0025" — value is right-most numeric in the row.
    page = _FakePage([
        _w(10,  200, 60,  210, "Exchange"),
        _w(62,  200, 92,  210, "Rate"),
        _w(94,  200, 110, 210, "(1)"),
        _w(120, 200, 145, 210, "THB"),
        _w(150, 200, 156, 210, "-"),
        _w(170, 200, 210, 210, "65.0025"),
    ])
    rate, cur = cr._geo_exchange_rate(page)
    assert rate == 65.0025
    assert cur == "THB"


# ---- commercial invoice cleaner (Wrong-Inv-No class) -------------------------

def test_invoice_strip_section_prefix():
    from pipeline.assembler import clean_invoice_no_commercial
    assert clean_invoice_no_commercial("A - AM-PD-018/2024") == "AM-PD-018/2024"


def test_invoice_keeps_trailing_suffix():
    from pipeline.assembler import clean_invoice_no_commercial
    # real content trails the pattern → must NOT be silently dropped
    assert clean_invoice_no_commercial("AM-PD-018/2024-REV2") == "AM-PD-018/2024-REV2"
