"""Phase 4.38 must not delete a tax that is genuinely zero.

The guard blanks a tax field holding, verbatim, another field's value — a label
was matched and the nearest number taken. That is a real failure and the guard
stays. But equality between two ZEROS carries no information: on a clean
declaration the duty and the Exemption/Reduction are both printed as 0, and the
old comparison read that as a copied neighbour.

Blanking there is worse than the bug it guards against. A stored NULL means
"nobody could read this"; the form says zero. A reviewer can tell those apart
only if we keep them apart.

Measured on two of the team's own documents (2 Sep 2026): the text layer read
`import_export_customs_duty = 0.0` from page 1 and this guard removed it, so
100325461351 and 100329052130 shipped a NULL duty against a printed 0.

The test drives the REAL comparison out of workflow.py rather than a copy — a
copied predicate would pass no matter what the shipped code does.
"""
import ast
import pathlib
import pytest

_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / "v11" / "workflow.py"


def _load_same():
    """Extract the `_same` closure from Phase 4.38 and return it callable.

    It is defined inside `run()`, so it cannot be imported. Walk the AST for the
    function named `_same`, compile that node alone, and execute it — the real
    source, not a paraphrase of it.
    """
    tree = ast.parse(_WORKFLOW.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_same":
            mod = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(mod)
            ns = {}
            exec(compile(mod, str(_WORKFLOW), "exec"), ns)
            return ns["_same"]
    pytest.fail("_same not found in workflow.py — Phase 4.38 was renamed or removed")


same = _load_same()


class TestAZeroIsAReading:
    def test_two_zeros_are_not_a_copied_neighbour(self):
        # The regression. Duty 0 and Exemption 0 are both printed on the form.
        assert same(0, 0) is False
        assert same(0.0, 0.0) is False
        assert same("0", "0.0") is False

    def test_a_zero_against_a_real_figure_is_still_not_a_match(self):
        assert same(0, 20000) is False
        assert same(20000, 0) is False


class TestTheGuardStillGuards:
    def test_a_repeated_money_figure_is_still_caught(self):
        # 100325137140: Exemption/Reduction 20,000 turned up in Security.
        assert same(20000, 20000) is True
        assert same(6127151.0, 6127151.0) is True

    def test_the_cent_tolerance_survives(self):
        assert same(1000.00, 1000.005) is True
        assert same(1000.00, 1000.5) is False

    def test_a_repeated_identifier_is_still_caught(self):
        # invoice_number holding the declaration number.
        assert same("100325461351", "100325461351") is True
        assert same(" ma0259/100560 ", "MA0259/100560") is True

    def test_absent_never_matches(self):
        assert same(None, None) is False
        assert same("", "") is False
        assert same(None, 0) is False
        assert same(0, None) is False

    def test_different_values_never_match(self):
        assert same(20000, 30000) is False
        assert same("PPG2602-1", "PPG2602-2") is False
