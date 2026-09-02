"""A lane that never read the declaration does not get to contribute items.

Phase 3.9 keeps the typed lane's items deliberately: misrouted item pages are
the reason attachment pages get read at all. That is correct when the stray
pages are continuation sheets of the same declaration. It is wrong when they
are an IMPORT LICENCE, because a licence carries its own goods table — same HS
codes, same product names, licence quantities, and its own CIF total.

On `0259100560` the typed lane read pages 6-8 (licence) and the vision lane read
pages 3-4 (CUSDEC). Both sets of items were merged, so a four-item declaration
stored nineteen rows, seven of them Belgian chocolate that is not in the
shipment.

The duplication was not the dangerous part. The licence's eleven lines sum
EXACTLY to the licence's own total, which the header had also taken — so fixing
only the dedup would have produced eleven tidy rows reconciling to the penny
against a total from the same wrong page, with no gate left to fail. `label`
cannot separate them: an Import Licence is machine-printed, so it is honestly
TYPED. Only `document` can.

This file pins the decision logic. It reimplements nothing — `_scope_items`
is imported from the workflow so a test cannot pass against a copy that has
drifted from what actually runs.
"""
import pytest

from v11.workflow import _scope_items


def _page(n, document, label="TYPED"):
    return {"page": n, "label": label, "document": document}


# The real bundle: 12 pages, CUSDEC on 3-4, licence on 6-8.
BUNDLE = (
    [_page(1, "OTHER"), _page(2, "OTHER")]
    + [_page(3, "DECLARATION", "HANDWRITTEN"), _page(4, "DECLARATION", "HANDWRITTEN")]
    + [_page(5, "OTHER", "HANDWRITTEN")]
    + [_page(6, "LICENCE"), _page(7, "LICENCE"), _page(8, "LICENCE")]
    + [_page(9, "INVOICE"), _page(10, "PACKING_LIST")]
    + [_page(11, "OTHER", "HANDWRITTEN"), _page(12, "OTHER", "HANDWRITTEN")]
)

CHOCOLATE = [{"item_name": "DARK FINEST BELGIAN CHOCOLATE", "hs_code": "1806209000"}] * 7
DASHI = [{"item_name": "BONITO DASHI", "hs_code": "2103902900"}] * 4


class TestTheRealFailure:

    def test_the_licence_lane_loses_its_items(self):
        typed = {"items": list(CHOCOLATE), "declaration": {"total_customs_value": 95707461.09}}
        vision = {"items": list(DASHI), "declaration": {}}
        out = {"trace": []}

        _scope_items(BUNDLE, typed, vision, [6, 7, 8], [3, 4, 5, 11, 12], out)

        assert typed["items"] == [], "the licence's goods table must not reach the merge"
        assert len(vision["items"]) == 4, "the lane that read the CUSDEC keeps its items"
        assert "items_off_declaration" in out["sanity_flags"]

    def test_it_also_takes_that_lanes_header(self):
        """A lane that owns no items owns no header.

        The total is the dangerous field: the licence's own CIF total is what
        made the wrong item list reconcile. Phase 3.9 would not have caught it —
        that guard fires on `cusdec_page_digital is False`, which requires triage
        to have LOCATED the CUSDEC by text, and every page of this bundle has
        zero extractable characters.
        """
        typed = {"items": list(CHOCOLATE),
                 "declaration": {"total_customs_value": 95707461.09,
                                 "invoice_price": 1542922.152,
                                 "importer_name": "PREMIUM DISTRIBUTION COMPANY LIMITED"}}
        out = {"trace": []}

        _scope_items(BUNDLE, typed, {"items": list(DASHI)}, [6, 7, 8], [3, 4], out)

        assert typed["declaration"]["total_customs_value"] is None
        assert typed["declaration"]["invoice_price"] is None
        # Not everything: the importer is the same on both forms, and blanking a
        # field the licence agrees on buys nothing.
        assert typed["declaration"]["importer_name"] == "PREMIUM DISTRIBUTION COMPANY LIMITED"


class TestItDoesNotOverreach:

    def test_a_lane_that_read_the_declaration_is_untouched(self):
        typed = {"items": list(DASHI), "declaration": {"total_customs_value": 1.0}}
        out = {"trace": []}
        _scope_items(BUNDLE, typed, None, [3, 4, 6], [], out)
        assert len(typed["items"]) == 4
        assert typed["declaration"]["total_customs_value"] == 1.0

    def test_unknown_pages_are_not_proof_of_anything(self):
        """UNKNOWN means the classifier could not tell, not that the page is
        foreign. Dropping items on an absence of evidence would delete real rows
        from every document the classifier finds hard."""
        pages = [_page(1, "DECLARATION"), _page(2, "UNKNOWN"), _page(3, "UNKNOWN")]
        typed = {"items": list(DASHI), "declaration": {}}
        out = {"trace": []}
        _scope_items(pages, typed, None, [2, 3], [], out)
        assert len(typed["items"]) == 4

    def test_other_alone_is_not_proof_either(self):
        pages = [_page(1, "DECLARATION"), _page(2, "OTHER")]
        typed = {"items": list(DASHI), "declaration": {}}
        out = {"trace": []}
        _scope_items(pages, typed, None, [2], [], out)
        assert len(typed["items"]) == 4

    def test_nothing_happens_when_no_declaration_page_was_identified(self):
        """Legacy behaviour, on purpose. With no anchor there is nothing to scope
        TO, and the gates downstream are then the only defence — which is where
        this codebase was before today."""
        pages = [_page(1, "LICENCE"), _page(2, "OTHER")]
        typed = {"items": list(CHOCOLATE), "declaration": {}}
        out = {"trace": []}
        _scope_items(pages, typed, None, [1, 2], [], out)
        assert len(typed["items"]) == 7
        assert any(t.get("skipped") for t in out["trace"])


class TestTheFailSafes:

    def test_it_never_empties_the_job(self):
        """When the foreign lane is the ONLY source of items, keep them.

        An empty item list is a known-bad outcome — the ROSETTA retry guard
        exists because a declaration with no rows has no sum to fail. And a
        reviewer can delete a wrong row; they cannot recover a dropped one. So
        the rows survive, flagged, rather than vanishing.
        """
        typed = {"items": list(CHOCOLATE), "declaration": {}}
        out = {"trace": []}
        _scope_items(BUNDLE, typed, {"items": []}, [6, 7, 8], [3, 4], out)

        assert len(typed["items"]) == 7, "must not leave the job with zero items"
        assert out["needs_review"] is True
        assert "items_only_from_licence" in out["sanity_flags"]

    def test_a_lane_with_no_page_list_is_left_alone(self):
        """Whole-document fallback reads everything, so it read the declaration."""
        typed = {"items": list(CHOCOLATE), "declaration": {}}
        out = {"trace": []}
        _scope_items(BUNDLE, typed, None, [], [], out)
        assert len(typed["items"]) == 7

    def test_malformed_pages_do_not_raise(self):
        out = {"trace": []}
        _scope_items([{"page": None}, {}, None], {"items": list(DASHI)}, None, [1], [], out)
        _scope_items(None, {"items": list(DASHI)}, None, [1], [], out)

    def test_a_lane_that_returned_nothing_is_skipped(self):
        out = {"trace": []}
        _scope_items(BUNDLE, None, None, [6, 7, 8], [3, 4], out)
        assert out["trace"] == [] or all("error" not in t for t in out["trace"])
