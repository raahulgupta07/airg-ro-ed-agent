"""The three invoice columns must hold three agreed quantities, not three guesses.

`fields.py` already defines them:

  invoice_price       total invoice amount IN THE INVOICE CURRENCY — the team's
                      ledger column, both Excel writers, and the signed Beta v3
                      requirement form all read it this way
  invoice_price_fc    "'Invoice price' as printed on the foreign-currency line"
  invoice_price_mmk   "'Invoice price' as printed on the (MMK) line"

On the seven documents the team filed complaints about (2 Sep 2026) the model
returned three DIFFERENT quantities under those three names on the same
declaration:

    invoice_price      1,603,800.0      correct — the form's own figure
    invoice_price_fc   1,688,358.864    the price PLUS the adjustment
    invoice_price_mmk  109,138,893.66   the total customs value

The form carries one figure: `Invoice price A - CIF - THB - 1,603,800`.

Why nothing caught it: `reconcile._cif_closure` reads `invoice_price_fc` FIRST
(`_first_present(decl, "invoice_price_fc", "invoice_price", ...)`), so a
build-up smuggled into that column double-counts the adjustment and the CIF
identity still closes. Same blind spot as the original unit regression — the one
gate that could see it was reading the other column.

Two fixes, pinned here:
  1. `textlayer_header` reads the figure off the page, so a measurement beats the
     model's answer wherever the page has characters.
  2. Phase 4.38 blanks `invoice_price_mmk` when it equals `total_customs_value`.
"""
import ast
import pathlib
import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_TEXTLAYER = _BACKEND / "v11" / "textlayer_header.py"
_WORKFLOW = _BACKEND / "v11" / "workflow.py"
_RECONCILE = _BACKEND / "v11" / "tools" / "reconcile.py"


class TestTheFigureIsRead:
    """The label sits on the page; a deterministic reader must take it."""

    def test_invoice_price_fc_is_in_the_spec(self):
        import sys
        sys.path.insert(0, str(_BACKEND))
        from v11.textlayer_header import _SPEC

        fields = {row[1] for row in _SPEC}
        assert "invoice_price_fc" in fields, (
            "textlayer_header does not read the invoice price; the model's answer "
            "stands even on a page whose characters carry the figure"
        )

    def test_it_is_anchored_on_the_longer_label(self):
        """`Invoice` alone also starts `Invoice price` — the spec must use the full one."""
        import sys
        sys.path.insert(0, str(_BACKEND))
        from v11.textlayer_header import _SPEC

        row = next(r for r in _SPEC if r[1] == "invoice_price_fc")
        assert row[0] == "Invoice price"

    def test_the_currency_words_do_not_defeat_the_numeric_match(self):
        """`A - CIF - THB - 1,603,800`: only the last token is numeric."""
        import sys
        sys.path.insert(0, str(_BACKEND))
        from v11.textlayer_header import _NUM

        for token in ("A", "-", "CIF", "THB"):
            assert not _NUM.match(token), f"{token!r} would be taken as the value"
        assert _NUM.match("1,603,800")


class TestTheTotalIsNotAnInvoicePrice:
    """Phase 4.38 must reject the customs total wearing the MMK column's name."""

    def test_the_guard_names_both_fields(self):
        src = _WORKFLOW.read_text()
        start = src.find("Phase 4.38")
        end = src.find("Phase 4.4", start)
        assert start != -1 and end != -1, "Phase 4.38 block not found"
        block = src[start:end]
        assert "invoice_price_mmk" in block
        assert "total_customs_value" in block

    def test_the_check_is_a_call_not_a_comment(self):
        """Walk the AST for a real `_same(invoice_price_mmk, total_customs_value)`."""
        src = _WORKFLOW.read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "_same"):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "invoice_price_mmk" in seg and "total_customs_value" in seg:
                found = True
                break
        assert found, (
            "no _same() call compares invoice_price_mmk against the customs total"
        )


class TestTheGateStillReadsFcFirst:
    """Documents the blind spot, so a change to it is deliberate rather than silent."""

    def test_cif_closure_prefers_invoice_price_fc(self):
        src = _RECONCILE.read_text()
        assert '"invoice_price_fc", "invoice_price"' in src, (
            "the CIF gate's field order changed; if it no longer reads "
            "invoice_price_fc first, the reason this column must be exact has "
            "changed too and this test's premise needs revisiting"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
