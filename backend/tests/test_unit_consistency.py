"""Two columns that are the same amount in different currencies must agree.

`invoice_price` silently changed meaning — from the invoice-currency amount to
the kyat one — and the score against the team's manual ledger fell from 54/60 to
44/60. Separately, `items.customs_value_mmk` was filled from the invoice-currency
"Item value" printed on the same row. Neither failed anything: a float column got
a valid float in both cases, and the reviewer sees a number under a heading that
does not say which currency it is in.

The arithmetic gates could not catch either one. The CIF gate reads
`invoice_price_fc` FIRST, so when `invoice_price` was wrong the identity still
closed on the *other* column and the document shipped with `suspect=[]`. That is
the specific hole this file fills: a check on the relationship BETWEEN the two
legs, rather than on whichever leg happens to be present.

The invariant, on a document whose exchange rate is not 1:

    invoice_price_fc x exchange_rate ~= invoice_price_mmk
    item invoice-currency line value != item assessed kyat value

Both directions matter. The first catches a leg that holds the wrong number; the
second catches a leg that holds the *right* number in the wrong unit, which is
what happened, and which no equality or range check can see.

WHAT THIS CANNOT PROVE:
  * That either figure was read correctly off the form. Two numbers can be
    consistent with each other and both wrong — `_cif_closure` in
    `v11/tools/reconcile.py` and the item-sum gate are what compare against the
    declared total, and even they were beaten once by a fabricated adjustment
    that made a wrong total self-consistent (UAT P10).
  * Anything at all when a document prints only one of the two figures, or no
    rate. `test_when_there_is_nothing_to_check` pins that the check reports
    "unchecked" rather than "fine" in that case — an unknown is not a pass.
  * That the DATABASE kept the digits. A consistent pair can still be truncated
    on write; that is `test_numeric_precision_roundtrip.py`.
"""
import pytest

import numeric
from v11.tools import reconcile
from v11.workflow import invoice_price_fields


# ── real figures, declaration 100306922661 (UAT P1) ──────────────────────────
# The form prints the invoice price twice, once in each currency, with the rate
# between them:
#     Invoice price   THB   556,226.5044
#     Exchange rate         58.3322
#     (MMK)                 32,445,915.69
P1_FC = 556226.5044
P1_RATE = 58.3322
P1_MMK = 32445915.69

# One item row off the same declaration:
#     unit price 33.7650 / KG   x   quantity 236.16 KG  =  7,973.9424  (THB)
#     assessed customs value                               465,137.6   (MMK)
P1_ITEM_UNIT_PRICE = 33.7650
P1_ITEM_QUANTITY = 236.16
P1_ITEM_LINE_FC = 7973.9424
P1_ITEM_VALUE_MMK = 465137.6

# The printed kyat figure is rounded to the nearest 0.01, and a rate is printed
# to four decimals, so exact equality is not available. 0.5% is far tighter than
# any plausible rounding and far looser than the ~58x a unit swap produces.
TOLERANCE_PCT = 0.5


def units_agree(fc, rate, mmk, tol_pct=TOLERANCE_PCT):
    """`(checked, gap_pct)` for `fc x rate ~= mmk`.

    `checked` is False when any leg is missing — deliberately separate from the
    result, so a caller cannot read an unknown as a pass. That conflation is what
    let a wrong invoice ship: the CIF gate returns `cif_ok=True` when it has
    nothing to check, which is right for the gate and wrong as a unit check.
    """
    if fc in (None, 0) or rate in (None, 0) or mmk in (None, 0):
        return False, None
    expected = fc * rate
    return True, abs(expected - mmk) / abs(mmk) * 100


class TestTheDeclarationLegsReconcileThroughTheRate:
    def test_the_real_figures_agree(self):
        checked, gap = units_agree(P1_FC, P1_RATE, P1_MMK)
        assert checked
        assert gap < TOLERANCE_PCT

    def test_they_agree_to_far_better_than_the_tolerance(self):
        # Recorded so a later loosening of the tolerance is a visible decision:
        # the real documents close to about 3e-8 %, not to 0.5%.
        _checked, gap = units_agree(P1_FC, P1_RATE, P1_MMK)
        assert gap < 1e-6

    def test_a_swapped_pair_is_detected(self):
        # The exact regression shape: the kyat figure lands in the FC column and
        # vice versa. The product is then off by the square of the rate.
        checked, gap = units_agree(P1_MMK, P1_RATE, P1_FC)
        assert checked
        assert gap > TOLERANCE_PCT

    def test_the_kyat_leg_holding_the_invoice_currency_amount_is_detected(self):
        # `invoice_price_mmk` filled from the FC figure — one leg wrong, not both.
        checked, gap = units_agree(P1_FC, P1_RATE, P1_FC)
        assert checked
        assert gap == pytest.approx((P1_RATE - 1) * 100, abs=1)

    def test_the_fc_leg_holding_the_kyat_amount_is_detected(self):
        checked, gap = units_agree(P1_MMK, P1_RATE, P1_MMK)
        assert checked
        assert gap > TOLERANCE_PCT

    def test_a_document_whose_rate_is_one_may_legitimately_have_equal_legs(self):
        # A kyat-denominated invoice. The check must not manufacture a defect
        # out of a document that is simply not in a foreign currency.
        checked, gap = units_agree(1394615.0, 1.0, 1394615.0)
        assert checked
        assert gap < TOLERANCE_PCT


class TestWhenThereIsNothingToCheck:
    """An unknown must report as unknown, never as fine."""

    @pytest.mark.parametrize("fc,rate,mmk", [
        (None, P1_RATE, P1_MMK),
        (P1_FC, None, P1_MMK),
        (P1_FC, P1_RATE, None),
        (0, P1_RATE, P1_MMK),
    ])
    def test_a_missing_leg_makes_the_check_unavailable(self, fc, rate, mmk):
        checked, gap = units_agree(fc, rate, mmk)
        assert checked is False
        assert gap is None


class TestTheRealBridgeProducesConsistentLegs:
    """Exercise `v11.workflow.invoice_price_fields`, not a copy of it.

    The first version of the unit test for this mapping copied the mapping into
    the test, so it would have passed no matter what the production code did.
    """

    def _mapped(self, vals):
        return invoice_price_fields(vals, numeric.keep_if_unparseable)

    def test_the_mapped_legs_reconcile_through_the_printed_rate(self):
        d = self._mapped({"invoice_price_fc": "556,226.5044",
                          "invoice_price_mmk": "32,445,915.69"})
        checked, gap = units_agree(d["invoice_price_fc"], P1_RATE,
                                   d["invoice_price_mmk"])
        assert checked
        assert gap < TOLERANCE_PCT

    def test_the_exported_column_is_the_foreign_currency_leg(self):
        # The unit swap itself. `invoice_price` is what the team's ledger, both
        # Excel writers and the signed requirement form read as THB.
        d = self._mapped({"invoice_price_fc": "556,226.5044",
                          "invoice_price_mmk": "32,445,915.69"})
        assert d["invoice_price"] == d["invoice_price_fc"]
        assert d["invoice_price"] != d["invoice_price_mmk"]

    def test_and_it_is_the_leg_that_multiplies_up_to_the_kyat_figure(self):
        # Stated as arithmetic rather than as column identity, so the assertion
        # survives a rename: whatever `invoice_price` is, times the rate, is the
        # kyat amount. If it ever holds kyats again this fails by ~58x.
        d = self._mapped({"invoice_price_fc": "556,226.5044",
                          "invoice_price_mmk": "32,445,915.69"})
        checked, gap = units_agree(d["invoice_price"], P1_RATE,
                                   d["invoice_price_mmk"])
        assert checked and gap < TOLERANCE_PCT


class TestItemRowsHoldTheCurrencyTheirColumnNameClaims:
    """`customs_value_mmk` was filled from the invoice-currency line value."""

    def test_the_line_value_is_the_product_of_the_printed_unit_price_and_quantity(self):
        assert (P1_ITEM_UNIT_PRICE * P1_ITEM_QUANTITY
                == pytest.approx(P1_ITEM_LINE_FC, abs=0.0001))

    def test_the_assessed_kyat_value_is_not_the_invoice_currency_line_value(self):
        # The defect, stated directly. On a document whose rate is not 1 these
        # two numbers cannot be equal, and equality is the signature of the
        # invoice-currency figure having been copied into the kyat column.
        assert P1_ITEM_VALUE_MMK != pytest.approx(P1_ITEM_LINE_FC, rel=0.005)

    def test_the_ratio_between_them_is_the_declaration_exchange_rate(self):
        # And this is why it is safe to assert the inequality: the relationship
        # is not merely "different", it is exactly the rate.
        assert (P1_ITEM_VALUE_MMK / P1_ITEM_LINE_FC
                == pytest.approx(P1_RATE, rel=1e-4))

    def test_a_row_whose_kyat_column_holds_the_invoice_figure_is_detected(self):
        checked, gap = units_agree(P1_ITEM_LINE_FC, P1_RATE, P1_ITEM_LINE_FC)
        assert checked
        assert gap > TOLERANCE_PCT

    def test_a_correct_row_passes(self):
        checked, gap = units_agree(P1_ITEM_LINE_FC, P1_RATE, P1_ITEM_VALUE_MMK)
        assert checked
        assert gap < TOLERANCE_PCT

    def test_the_check_is_skipped_on_a_kyat_denominated_document(self):
        # rate == 1: line value and assessed value are genuinely the same number,
        # and flagging it would be a false positive on every domestic invoice.
        checked, gap = units_agree(P1_ITEM_LINE_FC, 1.0, P1_ITEM_LINE_FC)
        assert checked and gap < TOLERANCE_PCT


class TestWhyTheExistingGateCouldNotCatchThis:
    """Not a hypothetical: the CIF gate reads the other column.

    `_cif_closure` prefers `invoice_price_fc` and falls back to `invoice_price`.
    That preference is correct — the FC figure is the one the CIF identity is
    stated in — but it means the gate is blind to `invoice_price` holding the
    wrong unit whenever the FC column is populated. Which is exactly when the
    regression happened.
    """

    def _decl(self, **kw):
        base = {"invoice_price": None, "invoice_price_fc": None,
                "exchange_rate": None, "freight_value": None,
                "insurance_value": None, "adjustment_value": None}
        base.update(kw)
        return base

    def test_the_gate_passes_a_document_whose_invoice_price_holds_kyats(self):
        out = reconcile._cif_closure(
            self._decl(invoice_price=P1_MMK,        # the regression: kyats here
                       invoice_price_fc=P1_FC,
                       exchange_rate=P1_RATE),
            P1_MMK)
        assert out["cif_checked"] is True
        assert out["cif_ok"] is True                # green, and the column is wrong

    def test_while_the_unit_check_in_this_file_flags_it(self):
        checked, gap = units_agree(P1_MMK, P1_RATE, P1_MMK)
        assert checked
        assert gap > TOLERANCE_PCT

    def test_the_gate_only_notices_when_the_fc_column_is_absent(self):
        # With no FC leg the gate falls back to `invoice_price`, and only then
        # does the wrong unit show up as a ~58x miss.
        out = reconcile._cif_closure(
            self._decl(invoice_price=P1_MMK, exchange_rate=P1_RATE), P1_MMK)
        assert out["cif_ok"] is False
