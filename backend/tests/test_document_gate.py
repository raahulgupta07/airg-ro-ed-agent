"""Arithmetic cannot tell you which paper it was read from.

Every other clause of `balanced` is a sum: items against the declared total, the
CIF build-up against the assessed value, the tax block, the exchange rate. An
Import Licence defeats all four at once, because it is a complete and internally
consistent document — its own goods table sums to its own CIF total. On
`0259100560` the licence's eleven lines came to 95,707,004.71 against a declared
95,707,461.09 it had also supplied: a gap of 456 kyat, 0.0005%, comfortably
inside a 5% tolerance.

The declaration had four items.

So this is the one clause that asks WHICH DOCUMENT rather than WHETHER IT ADDS
UP. `workflow._scope_items` drops foreign rows where it can; this gate covers
the cases it deliberately cannot, where the rows are present on purpose.
"""
import pytest

from v11.tools.reconcile import _document_check, reconcile


def _item(value, doc=None):
    it = {"item_name": "X", "customs_value_mmk": value}
    if doc:
        it["_src_doc"] = doc
    return it


class TestTheGateItself:

    def test_licence_items_are_not_ok(self):
        r = _document_check([_item(1, "LICENCE"), _item(2, "LICENCE")])
        assert r["doc_checked"] is True
        assert r["doc_ok"] is False
        assert r["foreign_documents"] == ["LICENCE"]

    def test_declaration_items_are_ok(self):
        r = _document_check([_item(1, "DECLARATION"), _item(2, "DECLARATION")])
        assert r["doc_checked"] is True
        assert r["doc_ok"] is True

    def test_one_foreign_row_among_good_ones_is_enough(self):
        r = _document_check([_item(1, "DECLARATION"), _item(2, "INVOICE")])
        assert r["doc_ok"] is False
        assert r["foreign_documents"] == ["INVOICE"]

    @pytest.mark.parametrize("doc", ["UNKNOWN", "NONE"])
    def test_unidentified_is_not_foreign(self, doc):
        """'Could not tell' is not evidence. Treating it as foreign would fail
        every bundle the classifier finds hard."""
        assert _document_check([_item(1, doc)])["doc_ok"] is True


class TestItStaysQuietWhereItHasNoInformation:

    def test_untagged_items_are_not_checked(self):
        """Every job extracted before `document` existed, every reviewer-added
        row and every recovered slice is untagged. Treating unstamped as foreign
        would flag the whole corpus and the gate would be turned off within a day.
        """
        r = _document_check([_item(1), _item(2)])
        assert r["doc_checked"] is False
        assert r["doc_ok"] is True

    def test_no_items_at_all(self):
        assert _document_check([])["doc_checked"] is False
        assert _document_check(None)["doc_checked"] is False


class TestItActuallyFlipsBalanced:
    """The point of the exercise: perfect arithmetic must not be enough."""

    DECL = {
        "total_customs_value": 95707461.09,
        "exchange_rate": 62.0301,
        "currency": "THB",
        "import_export_customs_duty": 15825908.0,
        "commercial_tax_ct": 6066598.0,
    }

    def test_a_perfectly_reconciling_licence_read_is_refused(self):
        items = [_item(95707004.71, "LICENCE")]
        v = reconcile(dict(self.DECL), items)
        assert v["checked"] is True
        assert v["gap_pct"] < 1.0, "the arithmetic really does close"
        assert v["doc_ok"] is False
        assert v["balanced"] is False, (
            "an item list that adds up perfectly but came off the import licence "
            "must not pass — this is the silent-ship shape")

    def test_the_same_numbers_off_the_declaration_are_fine(self):
        items = [_item(95707004.71, "DECLARATION")]
        v = reconcile(dict(self.DECL), items)
        assert v["gap_pct"] < 1.0
        assert v["doc_ok"] is True
        assert v["balanced"] is True, (
            "the gate must key on the document, not quietly fail everything")

    def test_untagged_items_behave_exactly_as_before(self):
        """The compatibility guarantee, stated as a test: an existing job's
        verdict must not change because this gate was added."""
        items = [_item(95707004.71)]
        v = reconcile(dict(self.DECL), items)
        assert v["doc_checked"] is False
        assert v["balanced"] is True
