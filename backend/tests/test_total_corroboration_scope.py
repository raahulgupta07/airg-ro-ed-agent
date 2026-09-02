"""Phase 4.365 corroborates a READ total; it must not overrule a MEASURED one.

The phase exists for the figure printed under the round customs PASS stamp. A
model misreads a digit inside it, and two votes misread the same pixels the same
way — so agreement there means consistency, not accuracy. The item block carries
the same figure with no stamp over it, so a sub-1% disagreement is treated as a
digit correction.

That reasoning holds only when the total was READ by a model. A deterministic
reader did not judge the number; it copied the characters the page carries. When
`textlayer_header` or the CUSDEC text rescue produced the total, the item rows
are the thing being checked, not the authority.

Measured on 100329052130 (2 Sep 2026): the text layer read
`(10) Total customs value 109,138,893.66` exactly; three item rows summed to
109,335,894.66 — 197,001 high, 0.18%, inside the window — and the phase replaced
the correct total. Reconcile then reported balanced, having compared the
replacement against the rows that produced it.

Guarded here at the level of the decision, since the phase is inline in `run()`
and cannot be imported.
"""
import ast
import pathlib
import pytest

_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / "v11" / "workflow.py"
_SRC = _WORKFLOW.read_text()

#: Writers that copy characters off the page rather than judging pixels.
DETERMINISTIC = ("textlayer", "cusdec_text")


def _phase_source() -> str:
    """The Phase 4.365 block, from its banner to the start of Phase 4.37."""
    start = _SRC.find("Phase 4.365")
    assert start != -1, "Phase 4.365 banner is gone — was the phase renamed?"
    end = _SRC.find("Phase 4.37", start)
    assert end != -1, "Phase 4.37 banner is gone — cannot bound 4.365"
    return _SRC[start:end]


class TestTheGuardIsPresentAndReal:
    def test_the_phase_reads_the_field_writer(self):
        """It must consult _field_engine — a rule that never looks cannot skip."""
        src = _phase_source()
        assert "_field_engine" in src
        assert "total_customs_value" in src

    def test_both_deterministic_writers_are_named(self):
        src = _phase_source()
        for writer in DETERMINISTIC:
            assert f'"{writer}"' in src, (
                f"{writer!r} is not treated as measured in Phase 4.365; a total it "
                f"read can still be overwritten by the item sum"
            )

    def test_the_skip_is_a_branch_not_a_comment(self):
        """Walk the AST: a real `if` must exist, not prose describing one.

        A guard asserted by grepping source text can be satisfied by the comment
        explaining the fix. This looks for the branch itself.
        """
        tree = ast.parse(_SRC)
        run = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "run"), None)
        assert run is not None, "run() not found in workflow.py"

        # Inspect the CONDITION of each `if`, not its body: the overwrite lives in
        # this statement's own else-branch, so the whole segment always mentions
        # the field. The claim is that the decision consults the writer.
        found = False
        for node in ast.walk(run):
            if not isinstance(node, ast.If):
                continue
            cond = ast.get_source_segment(_SRC, node.test) or ""
            if "_measured" in cond:
                found = True
                break
        assert found, (
            "no branch guards the overwrite on whether the total was measured"
        )


class TestTheDecisionItself:
    """The rule, exercised directly: skip iff a total exists AND was measured."""

    @staticmethod
    def _skips(decl_total, writer):
        return bool(decl_total) and writer in DETERMINISTIC

    def test_a_text_layer_total_is_kept(self):
        assert self._skips(109138893.66, "textlayer") is True

    def test_a_cusdec_text_total_is_kept(self):
        assert self._skips(64691431.29, "cusdec_text") is True

    def test_a_vision_total_is_still_corrected(self):
        # The case the phase was built for: a stamped cell, read by a model.
        assert self._skips(64691681.2, "vision_cusdec") is False

    def test_a_presto_total_is_still_corrected(self):
        assert self._skips(45791072.0, "presto") is False

    def test_an_unknown_writer_is_still_corrected(self):
        # Absence of a stamp is not proof of measurement — behave as before.
        assert self._skips(45791072.0, None) is False

    def test_a_blank_total_still_adopts_the_item_sum(self):
        # Nothing was measured, so nothing is destroyed — adopt whatever the
        # writer, including a deterministic one that found no total at all.
        assert self._skips(None, "textlayer") is False
        assert self._skips(0, "textlayer") is False
        assert self._skips(0.0, "cusdec_text") is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
