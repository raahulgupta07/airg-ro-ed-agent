"""A warning that fires on every correct document is not a warning.

The checks panel opened with "Freight cost is empty" and "Insurance cost is
empty" on all seven documents of the last complaint round — and on all seven the
form prints a dash for both, which the issue's own reason line admits: "most of
these documents leave this blank (just a dash)". Two permanent entries at the top
of a checklist teach a reviewer to skip the checklist, which costs more than the
entries are worth.

They are still worth naming in the one state that asks for them: when the CIF
identity does not close, a missing build-up line is a candidate explanation.
"""
from __future__ import annotations

import issues


BASE_DECL = {
    "declaration_no": "100329052130",
    "total_customs_value": 109138893.66,
    "exchange_rate": 64.642,
    "invoice_price": 1603800,
    "invoice_number": "MR260101",
    "consignor_name": "ASIATIC MART HOLDING PTE LTD",
    "import_export_customs_duty": 0,
    "commercial_tax_ct": 5456944,
    "release_order_date": "2026-05-26",
    "freight_value": None,
    "insurance_value": None,
}
ITEMS = [{"customs_value_mmk": 109138893.66, "hs_code": "2106.90.99",
          "quantity": 684, "invoice_unit_price": 1380}]


def _codes(job, decl=None):
    return [(i["code"], i.get("field")) for i in issues.build_issues(job, decl or BASE_DECL, ITEMS)]


class TestABlankBuildUpIsNotAFinding:
    def test_a_reconciling_document_says_nothing_about_freight(self):
        got = _codes({"review_status": "pending_review"})
        assert ("FIELD_EMPTY", "freight_value") not in got
        assert ("FIELD_EMPTY", "insurance_value") not in got

    def test_the_panel_is_quiet_on_a_clean_document(self):
        """The whole point: nothing to look at when nothing is wrong."""
        assert _codes({"review_status": "pending_review"}) == []


class TestItStillFiresWhereItMatters:
    def test_a_broken_cif_identity_asks_for_the_build_up(self):
        """`cif_ok is False` is the state where a missing freight line explains
        the arithmetic, so there the warning earns its place."""
        got = _codes({"review_status": "pending_review", "cif_ok": False})
        assert ("FIELD_EMPTY", "freight_value") in got
        assert ("FIELD_EMPTY", "insurance_value") in got

    def test_a_cif_sanity_flag_counts_as_a_broken_identity(self):
        got = _codes({"review_status": "pending_review", "sanity_flags": "cif_mismatch"})
        assert ("FIELD_EMPTY", "freight_value") in got

    def test_unknown_is_not_broken(self):
        """No verdict is not a failed verdict. A job with nothing to check
        against must not resurrect the two permanent warnings."""
        got = _codes({"review_status": "pending_review", "cif_ok": None})
        assert ("FIELD_EMPTY", "freight_value") not in got


class TestTheOtherWarningsAreUntouched:
    def test_a_genuinely_missing_field_is_still_reported(self):
        """The negative control for the change itself: silencing freight must
        not silence the row next to it."""
        decl = dict(BASE_DECL, invoice_number=None)
        got = _codes({"review_status": "pending_review"}, decl)
        assert ("FIELD_EMPTY", "invoice_number") in got

    def test_an_item_sum_gap_is_still_reported(self):
        decl = dict(BASE_DECL, total_customs_value=200000000.0)
        got = [c for c, _f in _codes({"review_status": "pending_review"}, decl)]
        assert "ITEM_SUM_GAP" in got or "SUM_MISMATCH" in got or got, (
            "a real arithmetic gap must still raise something")
