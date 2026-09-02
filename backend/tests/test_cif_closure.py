"""The CIF closure gate: basis x exchange_rate ~= total_customs_value.

This gate is the only check that can see a wrong exchange rate — the item-sum
invariant does not contain the rate at all. The UAT notes recorded it as
"false-flagging on uplift-heavy docs", implying the tolerance was too tight.

Measuring the stored corpus (21 distinct declarations, best reading of each)
says otherwise: 9 fail at a 4% tolerance and 7 still fail at 30%, because the
failures are not near-misses. They land on clean multiples — four separate
declarations sit at a ratio of exactly 1.500, one at 10.654. Widening the
tolerance would have silenced real disagreements without fixing one of them.

So the gate keeps its thresholds. What changed is that it now reports the ratio
(so the flag says *how* the numbers disagree) and prefers the foreign-currency
invoice as the basis, which is the figure the identity is actually stated in.
"""

import pytest

from v11.tools import reconcile


def _decl(**kw):
    base = {"invoice_price": None, "invoice_price_fc": None,
            "exchange_rate": None, "freight_value": None,
            "insurance_value": None, "adjustment_value": None}
    base.update(kw)
    return base


class TestTheBasis:
    def test_the_foreign_currency_invoice_is_preferred(self):
        # The form prints both "THB 481,406.664" and "(MMK) 32,356,946.56".
        # The identity is stated in the invoice currency, so the FC figure wins;
        # feeding it the MMK one would compute a total ~65x too large.
        out = reconcile._cif_closure(
            _decl(invoice_price=32356946.56, invoice_price_fc=481406.664,
                  exchange_rate=96.5649),
            46487178.29)
        assert out["cif_checked"] is True
        assert out["cif_ok"] is True
        assert out["cif_gap_pct"] < 1

    def test_invoice_price_is_the_fallback_when_fc_is_missing(self):
        # Most stored rows have no invoice_price_fc; those must still check.
        out = reconcile._cif_closure(
            _decl(invoice_price=1237473.0, exchange_rate=57.3984), 71028968.0)
        assert out["cif_checked"] is True
        assert out["cif_ok"] is True

    def test_the_buildup_is_added_to_the_basis(self):
        out = reconcile._cif_closure(
            _decl(invoice_price_fc=1000.0, freight_value=100.0,
                  insurance_value=50.0, adjustment_value=50.0,
                  exchange_rate=10.0),
            12000.0)
        assert out["cif_basis"] == 1200.0
        assert out["cif_ok"] is True

    def test_nothing_to_check_without_a_rate(self):
        out = reconcile._cif_closure(_decl(invoice_price_fc=1000.0), 12000.0)
        assert out["cif_checked"] is False
        # Unknown must not read as failed — an unchecked doc is not a bad doc.
        assert out["cif_ok"] is True


class TestTheGateStillCatchesWhatItIsFor:
    def test_a_wrong_exchange_rate_trips_it(self):
        # 100279686400 was stored once with rate 1.2023 and once with 57.3984.
        # The first is wrong by ~48x and must not pass.
        out = reconcile._cif_closure(
            _decl(invoice_price=1237473.0, exchange_rate=1.2023), 71028968.0)
        assert out["cif_ok"] is False

    def test_the_exact_1_500_cluster_still_fails(self):
        # Four separate declarations sit here. Whatever the cause — a missed
        # invoice, the wrong one of two printed figures — the numbers disagree
        # and the reviewer needs to know.
        out = reconcile._cif_closure(
            _decl(invoice_price=669704.38, exchange_rate=64.274), 64566868.0)
        assert out["cif_ok"] is False
        assert out["cif_gap_pct"] == pytest.approx(33.3, abs=0.5)

    def test_a_correct_document_passes_clean(self):
        out = reconcile._cif_closure(
            _decl(invoice_price=2258280.0, exchange_rate=64.374), 145374512.0)
        assert out["cif_ok"] is True
        assert out["cif_gap_pct"] < 1


class TestTheRatioTellsTheReviewerWhatIsWrong:
    def test_the_ratio_names_the_multiple(self):
        out = reconcile._cif_closure(
            _decl(invoice_price=669704.38, exchange_rate=64.274), 64566868.0)
        # "the total is 1.5x the invoice basis" is actionable; "33.3% off" is not.
        assert out["cif_ratio"] == pytest.approx(1.500, abs=0.005)

    def test_a_ratio_below_one_means_the_basis_is_too_large(self):
        out = reconcile._cif_closure(
            _decl(invoice_price=1351440.0, exchange_rate=61.77), 1537337.38)
        assert out["cif_ratio"] < 1
        assert out["cif_ok"] is False

    def test_a_clean_document_reports_a_ratio_of_one(self):
        out = reconcile._cif_closure(
            _decl(invoice_price=2258280.0, exchange_rate=64.374), 145374512.0)
        assert out["cif_ratio"] == pytest.approx(1.0, abs=0.001)

    def test_an_unchecked_document_has_no_ratio(self):
        out = reconcile._cif_closure(_decl(), 12000.0)
        assert out["cif_ratio"] is None


class TestTolerancesAreUnchanged:
    def test_the_loose_default_still_applies_without_a_buildup(self):
        assert reconcile.cif_tolerance_pct() == 15.0

    def test_the_tight_default_still_applies_with_one(self):
        assert reconcile.cif_tight_tolerance_pct() == 4.0

    def test_supplying_the_buildup_tightens_the_gate(self):
        loose = reconcile._cif_closure(
            _decl(invoice_price_fc=1000.0, exchange_rate=10.0), 10800.0)
        tight = reconcile._cif_closure(
            _decl(invoice_price_fc=1000.0, exchange_rate=10.0,
                  freight_value=0.0), 10800.0)
        assert loose["cif_tol_pct"] == 15.0 and loose["cif_ok"] is True
        assert tight["cif_tol_pct"] == 4.0 and tight["cif_ok"] is False
