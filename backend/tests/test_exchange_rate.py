"""Golden regression suite — exchange-rate extraction + math gates.

Guards the exchange-rate bug family that Atlas V14 just fixed:

  * digital CUSDEC pages must extract a plausible, in-band FX rate (not a stray
    customs figure — e.g. the old `500`/`636` artefacts), including 4-digit USD
    rates (the `\\d{1,3}` → `\\d{1,4}` regex fix);
  * the reconcile math gate must flag a rate that disagrees with the value the
    invoice × rate ≈ total math implies, and must NOT flag a consistent one;
  * the tax-completeness gate must flag a total with no CORE tax present.

PDF-backed tests use a real corpus that lives OUTSIDE the repo (see README);
they SKIP cleanly when it is absent. The unit tests (math gates, no PDF) always
run, so CI on a machine without the corpus still guards the gate logic.
"""
import json
import os
import re

import pytest

from v11.tools.cusdec_rescue import cusdec_fields, _rate_in_band, _RATE_BANDS
from v11.tools.reconcile import reconcile

# --- corpus + ground truth ---------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLDEN_PATH = os.path.join(_HERE, "golden_truth.json")

if not os.path.isfile(_GOLDEN_PATH):  # version-controlled — should always exist
    pytest.skip("golden_truth.json missing", allow_module_level=True)

with open(_GOLDEN_PATH, "r", encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)

TOL_PCT = float(GOLDEN.get("tol_pct", 3))
DOCS = GOLDEN["docs"]
DIGITAL = {k: v for k, v in DOCS.items() if v.get("type") == "digital"}

_DEFAULT_PDF_DIR = (
    "/private/tmp/claude-501/-Users-rahulgupta/"
    "7e2d3e61-b924-439a-8907-69d9d052f043/scratchpad/pdfs"
)
PDF_DIR = os.environ.get("RO_ED_TEST_PDFS", _DEFAULT_PDF_DIR)

try:
    import fitz  # noqa: F401
    _HAVE_FITZ = True
except Exception:  # pragma: no cover
    _HAVE_FITZ = False


def _pdf_path(doc_id: str) -> str:
    return os.path.join(PDF_DIR, f"{doc_id}.pdf")


def _corpus_ready(doc_id: str) -> bool:
    return _HAVE_FITZ and os.path.isdir(PDF_DIR) and os.path.isfile(_pdf_path(doc_id))


# Skip a PDF-backed test cleanly when the external corpus (or PyMuPDF) is absent.
requires_corpus = pytest.mark.skipif(
    not (_HAVE_FITZ and os.path.isdir(PDF_DIR)),
    reason=f"PDF corpus not available at {PDF_DIR} (set RO_ED_TEST_PDFS)",
)


# =============================================================================
# 1. Digital docs extract a correct, in-band FX rate (and NOT the old artefact).
# =============================================================================
@requires_corpus
@pytest.mark.parametrize("doc_id", sorted(DIGITAL), ids=sorted(DIGITAL))
def test_digital_rate_in_band(doc_id):
    meta = DIGITAL[doc_id]
    if not _corpus_ready(doc_id):
        pytest.skip(f"{doc_id}.pdf not in corpus")

    fields = cusdec_fields(_pdf_path(doc_id))
    assert fields is not None, f"{doc_id}: cusdec_fields returned None (no CUSDEC page found)"

    rate = fields.get("exchange_rate")
    currency = meta["currency"]
    assert rate is not None, f"{doc_id}: exchange_rate not extracted"

    # In the currency plausibility band.
    assert _rate_in_band(rate, currency), (
        f"{doc_id}: rate {rate} outside {currency} band {_RATE_BANDS.get(currency)}"
    )

    # Within tolerance of the manual/ground-truth rate when one is known.
    if "rate_approx" in meta:
        approx = meta["rate_approx"]
        gap_pct = abs(rate - approx) / approx * 100
        assert gap_pct <= TOL_PCT, (
            f"{doc_id}: rate {rate} vs approx {approx} = {gap_pct:.2f}% > {TOL_PCT}%"
        )

    # The OLD wrong value the bug produced must NOT come back.
    wrong = meta.get("code_grabbed_wrong")
    if wrong is not None:
        assert abs(rate - wrong) / wrong > 0.10, (
            f"{doc_id}: regression — extracted rate {rate} matches the old bug value {wrong}"
        )


# =============================================================================
# 2. USD 4-digit rate is representable (regression on \d{1,3} -> \d{1,4}).
# =============================================================================
@requires_corpus
def test_usd_four_digit_rate_representable():
    doc_id = "100313488550"  # USD, correct rate ~2100, bug grabbed 500
    if not _corpus_ready(doc_id):
        pytest.skip(f"{doc_id}.pdf not in corpus")

    fields = cusdec_fields(_pdf_path(doc_id))
    assert fields is not None, f"{doc_id}: cusdec_fields returned None"
    rate = fields.get("exchange_rate")
    assert rate is not None, f"{doc_id}: exchange_rate not extracted"
    # A 4-digit USD rate — the old \d{1,3} regex could never represent this.
    assert rate >= 1500, f"{doc_id}: USD rate {rate} < 1500 — 4-digit rate lost"
    assert abs(rate - 500) > 50, f"{doc_id}: regression — rate {rate} back near the old 500"


# =============================================================================
# 3. Math gate flags a wrong rate (unit test — no PDF).
# =============================================================================
def test_math_gate_flags_wrong_rate():
    # USD doc: the extracted rate is a bogus 500, far outside the USD band
    # (1500-5000) → the currency-band signal must catch it.
    decl = {
        "currency": "USD",
        "total_customs_value": 210_000_000.0,
        "invoice_price": 100_000.0,
        "exchange_rate": 500.0,
        "import_export_customs_duty": 5_000_000.0,  # a core tax present
    }
    items = [{"customs_value_mmk": 210_000_000.0,
              "invoice_unit_price": 100_000.0, "quantity": 1}]
    result = reconcile(decl, items)

    assert result["rate_suspect"] is True
    assert result["balanced"] is False
    assert result["derived_rate"] is not None
    assert result["extracted_rate"] == 500.0


# =============================================================================
# 4. Correct rate is NOT flagged (unit test — no PDF).
# =============================================================================
def test_math_gate_accepts_correct_rate():
    # USD doc, rate 2100 inside the USD band → consistent, not flagged.
    decl = {
        "currency": "USD",
        "total_customs_value": 210_000_000.0,
        "invoice_price": 100_000.0,
        "exchange_rate": 2100.0,
        "import_export_customs_duty": 5_000_000.0,
    }
    items = [{"customs_value_mmk": 210_000_000.0,
              "invoice_unit_price": 100_000.0, "quantity": 1}]
    result = reconcile(decl, items)

    assert result["rate_suspect"] is False
    assert result["extracted_rate"] == 2100.0


# =============================================================================
# 4b. A CORRECT CIF rate with NO build-up captured must NOT be flagged.
# Regression guard: total ÷ invoice OVER-estimates the rate when freight/insurance
# aren't captured (the CIF uplift), so a correct in-band rate must survive — the
# math mismatch alone must not "correct" a right rate to a wrong one.
# =============================================================================
def test_math_gate_accepts_correct_cif_rate_without_buildup():
    decl = {
        "currency": "THB",
        "total_customs_value": 112_097_371.76,  # includes freight/commission uplift
        "invoice_price": 1_229_641.95,          # bare invoice, no build-up captured
        "exchange_rate": 63.642,                # CORRECT THB rate (in 40-90 band)
        "import_export_customs_duty": 3_362_921.0,
    }
    result = reconcile(decl, [])
    # total/invoice ≈ 91 (inflated), but the rate is in-band and the derivation is
    # NOT trustworthy (no build-up) → must not be flagged/corrected.
    assert result["rate_suspect"] is False
    assert result["derived_trustworthy"] is False


# =============================================================================
# 5. Tax-completeness core-tax rule (unit test — no PDF).
# =============================================================================
def test_taxes_missing_when_only_service_fee():
    # A total present but only the flat MACCS service fee — every CORE tax
    # (duty / CT / AT) is missing → an extraction miss, must flag.
    decl = {
        "total_customs_value": 50_000_000.0,
        "maccs_service_fee_mf": 30_000.0,
    }
    result = reconcile(decl, [])
    assert result["taxes_missing"] is True


def test_taxes_present_when_core_duty_present():
    decl = {
        "total_customs_value": 50_000_000.0,
        "import_export_customs_duty": 4_000_000.0,
    }
    result = reconcile(decl, [])
    assert result["taxes_missing"] is False


# =============================================================================
# 6. Declaration date extracted as ISO yyyy-mm-dd (digital docs).
# =============================================================================
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@requires_corpus
@pytest.mark.parametrize("doc_id", sorted(DIGITAL), ids=sorted(DIGITAL))
def test_digital_declaration_date_iso(doc_id):
    if not _corpus_ready(doc_id):
        pytest.skip(f"{doc_id}.pdf not in corpus")
    fields = cusdec_fields(_pdf_path(doc_id))
    assert fields is not None, f"{doc_id}: cusdec_fields returned None"
    ddate = fields.get("declaration_date")
    if ddate is not None:
        assert _ISO_DATE.match(ddate), f"{doc_id}: declaration_date {ddate!r} not ISO yyyy-mm-dd"
