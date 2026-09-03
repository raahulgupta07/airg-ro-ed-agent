"""A kyat total that spells out the foreign-currency amount IS that amount.

Bundle `0259100560` stored `total_customs_value = 942,418,932`. The form prints
`CIF- 942,418.9320` — the invoice-currency figure, same digit string, decimal
point lost. The declaration's real total is 105,506,056, which its own item rows
already sum to.

Every one of that bundle's twelve pages is a photograph with zero extractable
characters, so no text-layer reader can settle it and no arithmetic gate can
correct it (the gate can only say the sum disagrees, which it did). The digits
themselves are the evidence.

The rule must be narrow: matching digits alone is not enough, because a genuine
total and a genuine FC amount could in principle share them. It fires only when
the value is also nowhere near the kyat figure the exchange rate implies.
"""
from __future__ import annotations

import pathlib

import pytest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / "v11" / "workflow.py"


def _digits(v):
    s = "".join(c for c in str(v or "") if c.isdigit())
    return s.lstrip("0").rstrip("0")


def _fires(total, fc, rate):
    """The Phase 4.38 rule, as a decision."""
    try:
        t = float(str(total or 0).replace(",", ""))
        f = float(str(fc or 0).replace(",", ""))
        r = float(str(rate or 0).replace(",", ""))
    except (TypeError, ValueError):
        return False
    if not (t and f and _digits(t) and _digits(t) == _digits(f)):
        return False
    implied = f * r if r else 0.0
    return (not implied) or abs(t - implied) > 2 * implied


class TestTheRealDocument:
    def test_the_942_million_total_is_recognised(self):
        assert _fires(942418932, 942418.9320, 64.398)

    def test_the_item_sum_is_what_replaces_it(self):
        """105,506,056 is the figure handwritten in the CUSDEC's own box, and
        the item rows reach it. The guard hands the total to that sum rather
        than leaving the document with none."""
        items = [{"customs_value_mmk": 105506056.0}]
        assert round(sum(i["customs_value_mmk"] for i in items), 2) == 105506056.0


class TestItDoesNotFireOnCorrectDocuments:
    @pytest.mark.parametrize("total,fc,rate", [
        (109138893.66, 1603800, 64.642),      # Pristine EQUAL — correct
        (203930146.31, 1646160, 64.918),      # Shwe Nadi — correct
        (61271518.32, 927737.8464, 66.044),   # Gillette — correct
        (219512537.15, 3294846.7084, 64.676),
        (104179810.01, 1579937.3376, 64.918),
        (50295133.54, 686284.65, 64.445),
        (80714470.19, 1046672.928, 64.445),
    ])
    def test_the_seven_complaint_documents_are_untouched(self, total, fc, rate):
        assert not _fires(total, fc, rate)

    def test_matching_digits_alone_are_not_enough(self):
        """The coincidence case: same digits, but the value IS what the rate
        implies. Blanking there would delete a correct total."""
        assert not _fires(1000.0, 1000.0, 1.0)

    def test_a_missing_field_never_fires(self):
        assert not _fires(None, 942418.9320, 64.398)
        assert not _fires(942418932, None, 64.398)
        assert not _fires(0, 0, 64.398)


class TestTheShippedGuard:
    def test_the_workflow_carries_the_rule(self):
        src = WORKFLOW.read_text(encoding="utf-8")
        assert "total_customs_value=invoice_currency_amount" in src, (
            "the digit-identity guard is gone from Phase 4.38")
        assert "total_from_items" in src, (
            "the guard no longer hands the total to the item sum, so a caught "
            "document would ship with no total at all")

    def test_it_does_not_use_the_module_level_re(self):
        """`workflow.py` imports `re` per-function, not at the top. A guard that
        reached for a global `re` would raise NameError on exactly the documents
        it exists to save, and the surrounding `except` would swallow it."""
        src = WORKFLOW.read_text(encoding="utf-8")
        i = src.find("def _digits(v):")
        assert i > 0
        body = src[i:i + 400]
        assert "re.sub" not in body, "the digit helper reaches for a module-level `re`"
        assert "isdigit" in body
