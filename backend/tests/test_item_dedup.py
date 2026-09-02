"""Two lanes reading the same printed line must produce one row.

Job `JOB_20260804_060507_662ec932_0259100560_` stored the same four dashi
products three times over. Nothing in the dedup key held:

    HS       "2103.90.29 00"   "2103902900"   ""          (formatting, then absent)
    name     "...( 150 g/Carton box)" vs "... (150 G/CARTON BOX)"   (spacing)
    name     "FISH FLAVORED"   vs  "FISH FLAVOURED"                 (US vs UK)
    quantity  3303             vs   3168                            (two documents)

HS normalisation already handled the first line — `_norm_hs` strips non-digits.
The name was doing the damage: `.strip().upper()` leaves layout differences in
place, and exact equality was a precondition for every other check, so two reads
of one carton never met.

THIS FILE IS DELIBERATELY THE LAST FIX IN THE SEQUENCE. Applied on its own it
would have made the failure WORSE: nineteen rows would have collapsed to the
eleven lines of the Import Licence, which sum to the licence's own total to
within 456 kyat — a tidy, self-consistent, entirely wrong answer with `suspect`
empty and no gate left to fail. The document scoping in `workflow._scope_items`
and the `_document_check` clause in `reconcile` had to land first. Collapsing
duplicates is cosmetic; knowing which paper they came off is not.

Over-collapse is the danger this file guards. Under-collapse shows a reviewer two
rows and wastes a minute; over-collapse deletes a product and the item sum then
under-reports the consignment.
"""
import pytest

from v11.agents.merger import merge_results


def _row(name, hs="", qty="", value=1.0):
    return {"item_name": name, "hs_code": hs, "quantity": qty,
            "customs_value_mmk": value}


def _merge(a, b):
    return merge_results({"declaration": {}, "items": a},
                         {"declaration": {}, "items": b})["items"]


class TestTheRealDuplicates:

    def test_spacing_and_bracketing_collapse(self):
        got = _merge(
            [_row("BONITO DASHI (FISH FLAVORED SEASONING)( 150 g/Carton box)",
                  "2103.90.29 00", "3312.0")],
            [_row("BONITO DASHI (FISH FLAVORED SEASONING) (150 G/CARTON BOX)",
                  "2103902900", "3312.0")])
        assert len(got) == 1

    def test_dotted_and_plain_hs_are_the_same_code(self):
        got = _merge([_row("KOMBU DASHI 1 KG CARTON BOX", "2103.90.29 00", "5664.0")],
                     [_row("KOMBU DASHI 1 KG CARTON BOX", "2103902900", "5664.0")])
        assert len(got) == 1

    def test_british_and_american_spelling_collapse(self):
        """The lane that read the CUSDEC wrote FLAVOURED; the lane that read the
        licence wrote FLAVORED. Same carton, same HS, same quantity."""
        got = _merge(
            [_row("BONITO DASHI (FISH FLAVORED SEASONING) (150 G/CARTON BOX)",
                  "2103902900", "3312.0")],
            [_row("BONITO DASHI (FISH FLAVOURED SEASONING) (150 G/CARTON BOX)",
                  "", "3312.0")])
        assert len(got) == 1

    def test_a_missing_hs_does_not_block_a_match(self):
        got = _merge([_row("KOMBU DASHI 150 G CARTON BOX", "2103902900", "3168.0")],
                     [_row("KOMBU DASHI 150 G CARTON BOX", "", "3168.0")])
        assert len(got) == 1


class TestItDoesNotOverCollapse:

    def test_different_pack_sizes_stay_apart(self):
        """The 150 g and 1 kg cartons are different products with the same name
        stem, and both are genuinely on this declaration."""
        got = _merge([_row("KOMBU DASHI (150 G/CARTON BOX)", "2103902900", "3168.0")],
                     [_row("KOMBU DASHI (1 KG/CARTON BOX)", "2103902900", "5664.0")])
        assert len(got) == 2

    def test_same_product_different_quantity_stays_apart(self):
        """3,303 KG is the licence's permitted quantity, 3,168 KG is what
        shipped. Both documents are correct; collapsing them would silently pick
        one and lose the discrepancy a reviewer needs to see."""
        got = _merge([_row("KOMBU DASHI 150 G CARTON BOX", "2103902900", "3303.0")],
                     [_row("KOMBU DASHI 150 G CARTON BOX", "2103902900", "3168.0")])
        assert len(got) == 2

    def test_different_hs_codes_stay_apart(self):
        got = _merge([_row("FINEST BELGIAN CHOCOLATE", "1806209000", "600.0")],
                     [_row("FINEST BELGIAN CHOCOLATE", "2103902900", "600.0")])
        assert len(got) == 2

    def test_two_similar_names_with_nothing_else_to_agree_on_stay_apart(self):
        """The near-match needs corroboration. With no HS, no pack and no
        quantity on either row, spelling similarity is all there is — and that is
        not enough to delete a product."""
        got = _merge([_row("FINEST BELGIAN CHOCOLATE 823 E4 U71")],
                     [_row("FINEST BELGIAN CHOCOLATE 823 E4 U72")])
        assert len(got) == 2

    def test_genuinely_different_products_stay_apart(self):
        got = _merge([_row("BONITO DASHI FISH FLAVORED SEASONING", "2103902900", "3312.0")],
                     [_row("KOMBU DASHI SEAWEED SEASONING", "2103902900", "3312.0")])
        assert len(got) == 2

    def test_the_seven_chocolate_lines_survive_as_seven(self):
        """Similar names, same HS, same supplier — the shape most at risk from a
        loosened key."""
        names = [
            "DARK FINEST BELGIAN CHOCOLATE (811-E4-U71)(2.5 Kg/multilayer pack)",
            "CHOCOLATE STICKS 8cm (TB-55-8-356) (1.60 Kg/paper box)",
            "FINEST BELGIAN CHOCOLATE (823-E4-U71)(2.5 Kg/multilayer pack)",
            "DARK COUVERTURE CHOCOLATE (MIN;COCOA; 70.5%)(70-30-38-E4-U71) (2.5 Kg/multilayer pack)",
            "FINEST BELGIAN CHOCOLATE(W2-E4-U71) (2.5 Kg/multilayer pack)",
            "DARK FINEST BELGIAN CHOCOLATE (VH-9401-E4-U70)(2.5 Kg/multilayer pack)",
            "SWEETENED HAZELNUT PASTE (PRA-T14)(5 Kg/HDPE Bottle)",
        ]
        qtys = ["2400.0", "360.0", "260.0", "760.0", "600.0", "50.0", "50.0"]
        rows = [_row(n, "1806209000", q) for n, q in zip(names, qtys)]
        assert len(_merge(rows, [])) == 7

    def test_the_two_fifty_kilo_chocolates_are_not_merged(self):
        """Same HS, same quantity, similar names — everything agrees except the
        product. The names differ by more than a spelling variant, and must not
        be treated as one."""
        got = _merge(
            [_row("DARK FINEST BELGIAN CHOCOLATE (VH-9401-E4-U70)(2.5 Kg/multilayer pack)",
                  "1806209000", "50.0")],
            [_row("SWEETENED HAZELNUT PASTE (PRA-T14)(5 Kg/HDPE Bottle)",
                  "1806209000", "50.0")])
        assert len(got) == 2
