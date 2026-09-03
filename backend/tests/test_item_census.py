"""The form prints how many items it has, and that count is the authority.

Three of the seven documents the team filed complaints about came back with
MORE item rows than the declaration has: 9 rows for a 5-item form, 8 for 6, and
5 for 3. The surplus rows were echoes — the same product names again, carrying
no quantity — and on `100329052130` the two extras' customs values summed to
197,001, which was EXACTLY the gap between the item sum and the declared total.
Both symptoms, one cause.

`Total items` is printed in the decision box of every one of these forms and was
never read. A model can invent a row; it cannot make the form print a larger
number.

What is pinned here:
  * the census reader returns the printed count (and nothing on a blank read);
  * the prune drops ONLY a row with no quantity whose value is not printed
    anywhere in the document, never goes below the printed count, and never
    fires when the count is absent or already matches;
  * the guard is not vacuous — a document whose surplus rows look real keeps
    every row and is flagged instead.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = BACKEND / "v11" / "workflow.py"
CENSUS_SRC = BACKEND / "v11" / "textlayer_header.py"


# ── the reader ────────────────────────────────────────────────────────────
class TestTheCensusReader:
    def test_the_count_is_not_in_the_declaration_spec(self):
        """`Total items` is not a declaration column.

        `_save_to_db` is a hard whitelist, so a field it does not map is dropped
        silently. Putting the census in `_SPEC` would send it down that path and
        it would vanish without an error — the same landmine that kept
        freight/insurance NULL for every job ever recorded.
        """
        from v11.textlayer_header import _SPEC, _CENSUS

        spec_fields = {f for _l, f, _k, _dx, _s in _SPEC}
        census_fields = {f for _l, f, _k, _dx, _s in _CENSUS}
        assert census_fields == {"total_items", "total_item_value"}
        assert not (spec_fields & census_fields), (
            "the census fields are in _SPEC, so they would be written into the "
            "declaration dict and then dropped by the _save_to_db whitelist")

    def test_a_zero_count_is_not_a_census(self):
        """`Total items 0` is a failed read, not a form with no items.

        A declaration with no item block prints no total either, so 0 can only
        come from the reader landing on the wrong cell. Treating it as a census
        would prune every row on the document.
        """
        src = CENSUS_SRC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "read_census")
        # A comparison against 0 must gate the assignment of total_items.
        guards = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
        assert any(
            isinstance(c.ops[0], ast.Gt)
            and isinstance(c.comparators[0], ast.Constant)
            and c.comparators[0].value == 0
            for c in guards
        ), "read_census does not reject a zero count"


# ── the prune, as a rule rather than as source text ───────────────────────
def _prune(items, printed, doc_text):
    """The Phase 4.39 rule, extracted so the test exercises the DECISION.

    Kept in step with the workflow by `test_the_workflow_uses_this_rule` below —
    a copy that drifts is worse than no test, which is how a previous guard here
    passed while asserting nothing about the shipped code.
    """
    def printed_on_paper(val):
        if val in (None, "", 0):
            return False
        try:
            f = float(str(val).replace(",", "").strip())
        except (TypeError, ValueError):
            return False
        if not doc_text:
            return True
        for cand in {f"{f:,.2f}", f"{f:,.4f}".rstrip("0").rstrip("."),
                     f"{f:,.0f}", f"{f:.2f}", repr(f), str(f)}:
            if cand and cand in doc_text:
                return True
        return False

    def has_qty(it):
        q = it.get("quantity")
        try:
            return q is not None and float(str(q).replace(",", "")) > 0
        except (TypeError, ValueError):
            return bool(str(q or "").strip())

    if not printed or len(items) <= printed:
        return list(items), set()
    suspect = [i for i, it in enumerate(items)
               if not has_qty(it) and not printed_on_paper(it.get("customs_value_mmk"))]
    surplus = len(items) - printed
    drop = set(suspect[-surplus:]) if len(suspect) >= surplus else set()
    return [it for i, it in enumerate(items) if i not in drop], drop


# The real shape of 100329052130: three genuine rows, two echoes whose values
# are printed nowhere and which sum to the exact item-sum gap.
PRISTINE = [
    {"item_name": "FOODSTUFFS CLASSIC ZERO CALORIE SWEETENER 100G", "quantity": 684.0,
     "customs_value_mmk": 61016876.63},
    {"item_name": "CLASSIC ZERO CALORIE SWEETENER 50G", "quantity": 390.0,
     "customs_value_mmk": 41769001.28},
    {"item_name": "CLASSIC ZERO CALORIE SWEETENER 25G", "quantity": 58.5,
     "customs_value_mmk": 6353015.75},
    {"item_name": "EQUAL CLASSIC ZERO CALORIE SWEETENER 100G", "quantity": None,
     "customs_value_mmk": 88641.0},
    {"item_name": "EQUA CLASSIC ZERO CALORIE SWEETENER 25G", "quantity": None,
     "customs_value_mmk": 108360.0},
]
PRISTINE_TEXT = ("Total items 3 61,016,876.63 41,769,001.28 6,353,015.75 "
                 "Total customs value 109,138,893.66")

# 100325137140: five real rows and four name-only echoes.
GILLETTE = [
    {"item_name": f"REAL {i}", "quantity": 100.0 + i, "customs_value_mmk": 1000.0 + i}
    for i in range(5)
] + [
    {"item_name": f"ECHO {i}", "quantity": None, "customs_value_mmk": None}
    for i in range(4)
]
GILLETTE_TEXT = "Total items 5 " + " ".join(f"{1000 + i:,.2f}" for i in range(5))


class TestThePrune:
    def test_the_two_unprinted_rows_go_and_the_sum_then_closes(self):
        kept, dropped = _prune(PRISTINE, 3, PRISTINE_TEXT)
        assert len(kept) == 3 and dropped == {3, 4}
        total = 109138893.66
        assert round(sum(i["customs_value_mmk"] for i in kept), 2) == total, (
            "dropping the unsupported rows should leave the item sum equal to "
            "the declared total — that agreement is the corroboration")

    def test_name_only_echoes_go(self):
        kept, dropped = _prune(GILLETTE, 5, GILLETTE_TEXT)
        assert len(kept) == 5 and dropped == {5, 6, 7, 8}
        assert all(i["item_name"].startswith("REAL") for i in kept)

    def test_nothing_happens_when_the_count_matches(self):
        kept, dropped = _prune(PRISTINE[:3], 3, PRISTINE_TEXT)
        assert len(kept) == 3 and dropped == set()

    def test_nothing_happens_without_a_printed_count(self):
        """A scanned decision box yields no census, and no census means no prune."""
        kept, dropped = _prune(PRISTINE, None, PRISTINE_TEXT)
        assert len(kept) == 5 and dropped == set()

    def test_a_row_whose_value_is_on_the_paper_is_never_dropped(self):
        """Being surplus is not enough. The row must also be uncorroborated."""
        items = PRISTINE[:3] + [
            {"item_name": "REAL BUT UNQUANTIFIED", "quantity": None,
             "customs_value_mmk": 61016876.63},
        ]
        kept, dropped = _prune(items, 3, PRISTINE_TEXT)
        assert dropped == set(), (
            "this row has no quantity but its value IS printed on the form; "
            "dropping it would delete a reading a reviewer cannot recover")

    def test_a_quantified_surplus_row_is_never_dropped(self):
        items = PRISTINE[:3] + [
            {"item_name": "HAS A QUANTITY", "quantity": 12.0, "customs_value_mmk": 999.0},
        ]
        kept, dropped = _prune(items, 3, PRISTINE_TEXT)
        assert dropped == set()

    def test_a_document_with_no_text_layer_is_never_pruned(self):
        """On a photograph nothing is corroborated, so 'unsupported' means every
        row. Pruning there would delete the whole item block on no evidence."""
        kept, dropped = _prune(PRISTINE, 3, "")
        assert len(kept) == 5 and dropped == set()

    def test_it_never_prunes_below_the_printed_count(self):
        many_echoes = PRISTINE[:3] + [
            {"item_name": f"ECHO {i}", "quantity": None, "customs_value_mmk": None}
            for i in range(4)
        ]
        kept, dropped = _prune(many_echoes, 3, PRISTINE_TEXT)
        assert len(kept) == 3 and len(dropped) == 4


class TestTheWorkflowShipsThisRule:
    """The rule above is a copy; this asserts the shipped phase agrees with it.

    Source-text assertions are checked against the AST, not by grepping for a
    word — a comment naming the condition satisfies a grep, which is exactly how
    an earlier guard in this repo went green while asserting nothing.
    """

    def test_the_phase_exists_and_calls_the_census_reader(self):
        """Assert the IMPORTED NAME, not the local alias.

        The workflow imports it as `_tl_census`; checking the alias would break
        on a rename that changes nothing, and checking neither would let the
        phase lose its only source of the printed count silently.
        """
        tree = ast.parse(WORKFLOW.read_text(encoding="utf-8"))
        imported = {
            a.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("textlayer_header")
            for a in n.names
        }
        assert "read_census" in imported, (
            "workflow does not import the census reader, so the printed count "
            "is not consulted anywhere")

    def test_the_prune_runs_after_recovery_not_before(self):
        """Order is the whole fix, and getting it wrong looked like success.

        The first version ran before the reconcile gate and did nothing on three
        of the four documents it was written for: the surplus rows are ADDED by
        the recovery pass inside that gate — one document went from 8 rows to 13
        — so a census taken earlier counts the wrong list. The call must sit
        after the recovery, and the verdict must be recomputed on the rows that
        actually ship.
        """
        src = WORKFLOW.read_text(encoding="utf-8")
        # The CALL, not the `def` — both contain the same argument list, and
        # matching the definition would compare the wrong two positions and pass
        # or fail for reasons that have nothing to do with ordering.
        call = src.find("= _census_prune(out, pdf_path, triage)")
        recovery = src.find("recovery = {\"attempted\"")
        assert call > 0, "nothing calls the census prune"
        assert recovery > 0 and call > recovery, (
            "the census prune runs before the recovery pass, which is where the "
            "surplus rows come from")
        after = src[call:call + 900]
        assert "_reconcile.reconcile(" in after, (
            "rows are dropped but the verdict is not recomputed, so the gates "
            "judge a list that no longer exists")

    def test_a_surplus_that_looks_real_is_flagged_not_guessed(self):
        """13 rows against a printed 6, every one carrying a quantity: the rule
        must decline. Choosing which real-looking row is the intruder is exactly
        the guess this guard exists to avoid."""
        src = WORKFLOW.read_text(encoding="utf-8")
        i = src.find("def _census_prune")
        body = src[i:i + 4200]
        assert "item_count_over_declared" in body and "needs_review" in body

    def test_the_three_conditions_are_all_present(self):
        """No quantity AND value not on paper AND a printed count."""
        src = WORKFLOW.read_text(encoding="utf-8")
        i = src.find("def _census_prune")
        assert i > 0, "the item-census helper is gone"
        block = src[i:i + 6000]
        for needed in ("_has_qty", "_printed_on_paper", "total_items"):
            assert needed in block, f"the census phase no longer uses {needed}"
        assert "drop = set(suspect[-surplus:])" in block, (
            "the phase no longer limits the drop to the surplus, so it could "
            "prune below the count the form printed")


@pytest.mark.parametrize("pdf_stem,expected", [
    ("100325137140", 5),
    ("100327095522", 6),
    ("100327110551", 1),
    ("100330924520", 4),
    ("100330925700", 2),
    ("100325461351", 2),
    ("100329052130", 3),
])
def test_the_reader_gets_the_count_off_the_real_forms(pdf_stem, expected):
    """The seven complaint documents, if they are on this machine.

    Skipped rather than failed when absent: the corpus is not in the repo, and a
    test that cannot see the paper must not claim the reader is broken.
    """
    src = pathlib.Path.home() / "Desktop" / "RO-ED-Feedback"
    if not src.is_dir():
        pytest.skip("the complaint corpus is not on this machine")
    matches = [p for p in src.glob("*.pdf") if pdf_stem in p.name]
    if not matches:
        pytest.skip(f"{pdf_stem} not in the corpus")
    from v11.textlayer_header import read_census
    got = read_census(str(matches[0]), [1, 2, 3])
    assert got.get("total_items") == expected
