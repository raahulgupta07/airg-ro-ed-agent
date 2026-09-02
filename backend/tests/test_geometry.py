"""Geometry tier + multi-declaration bundle guard.

Pure tests need no PDF. The corpus-backed tests assert the numbers that were
verified by hand against the printed forms, and skip cleanly when the PDFs are
not present (they live outside the repo — see tests/README.md).
"""
import os

import pytest

from rover import geometry as geo
from rover import supervisor as sup
from rover.schema import Cell


def pdf_dir() -> str:
    """Corpus location. `tests/` is a package, so conftest is not importable by
    name — read the same env var it uses."""
    return os.environ.get("RO_ED_TEST_PDFS", "")


# --------------------------------------------------------------------------- #
# pure — no PDF required
# --------------------------------------------------------------------------- #
def test_declaration_id_pattern_ignores_mobile_numbers():
    """Myanmar mobiles are 11 digits starting 09 and appear on the broker block of
    most bundles. A \\d{11,12} pattern treats them as declaration ids and every
    doc looks like a bundle."""
    assert geo._DECL_ID.findall("Tel 09977879601 decl 100313488550") == ["100313488550"]
    assert geo._DECL_ID.findall("09775777756 / 09977879601") == []


def test_tonum_handles_form_punctuation():
    assert geo._tonum("198,450,000") == 198450000.0
    assert geo._tonum("44,612.82") == 44612.82
    assert geo._tonum("3,969,000*") == 3969000.0     # trailing asterisk marker
    assert geo._tonum("-") is None
    assert geo._tonum("(1)") is None
    assert geo._tonum("USD") is None


def test_plausible_rejects_tax_larger_than_customs_value():
    ctx = {"total_customs_value": 100_000_000.0}
    assert geo._plausible("commercial_tax_ct", 5_000_000.0, ctx) is True
    # a tax bigger than the value it is assessed on is a mis-bind, not a tax
    assert geo._plausible("commercial_tax_ct", 109_708_753.0, ctx) is False


def test_plausible_rate_uses_currency_band():
    assert geo._plausible("exchange_rate", 2100.0, {"_ccy": "USD"}) is True
    assert geo._plausible("exchange_rate", 65.0, {"_ccy": "USD"}) is False
    assert geo._plausible("exchange_rate", 65.0, {"_ccy": "THB"}) is True


def test_cif_identity_includes_the_printed_adjustment():
    """The uplift is printed on the form and is load-bearing: bare FC x rate is
    wrong on 4 of the 5 text-layer docs in the corpus."""
    cells = {
        "invoice_price_fc": Cell(column="invoice_price_fc", value=49887.18),
        "exchange_rate": Cell(column="exchange_rate", value=2100.0),
        "adjustment_value": Cell(column="adjustment_value", value=44612.82),
        "total_customs_value": Cell(column="total_customs_value", value=198450000.0),
    }
    out = geo.cif_identity(cells)
    assert out["expected"] == pytest.approx(198450000.0, rel=1e-9)
    assert out["gap_pct"] == pytest.approx(0.0, abs=1e-9)
    assert out["bare"] == pytest.approx(104763078.0, rel=1e-9)   # the wrong answer


def test_cif_identity_none_when_inputs_missing():
    assert geo.cif_identity({}) is None


# --------------------------------------------------------------------------- #
# bundle guard
# --------------------------------------------------------------------------- #
def _rec(decl="100313488550"):
    return {"declaration_no": Cell(column="declaration_no", value=decl, status="ok")}


def test_bundle_flag_marks_identity_suspect_and_keeps_the_other_id():
    rec = _rec()
    notes = sup.flag_multi_declaration(rec, ["100313488550", "100319576711"])
    c = rec["declaration_no"]
    assert c.status == "suspect"
    assert "100319576711" in c.alternates
    assert notes and "2 declarations" in notes[0]


def test_single_declaration_is_not_flagged():
    rec = _rec()
    assert sup.flag_multi_declaration(rec, ["100313488550"]) == []
    assert rec["declaration_no"].status == "ok"
    assert sup.flag_multi_declaration(_rec(), []) == []


def test_compile_forces_review_on_a_bundle():
    det = {}
    vis = {"declaration_no": Cell(column="declaration_no", value="100313488550",
                                  status="ok", confidence=0.99)}
    _, suspect, _, needs_review = sup.compile(det, vis,
                                              ["100313488550", "100319576711"])
    assert "declaration_no" in suspect
    assert needs_review is True


def test_compile_signature_is_backward_compatible():
    """decl_ids is optional — existing callers must keep working unchanged."""
    vis = {"declaration_no": Cell(column="declaration_no", value="100313488550",
                                  status="ok", confidence=0.99)}
    rec, suspect, notes, needs = sup.compile({}, vis)
    assert isinstance(rec, dict)


# --------------------------------------------------------------------------- #
# corpus-backed — hand-verified against the printed forms
# --------------------------------------------------------------------------- #
_EXPECTED = {
    # doc: (rate, total_customs_value, invoice_price_fc, adjustment_value)
    "100313488550": (2100.0, 198450000.0, 49887.18, 44612.82),
    "100313868761": (65.0025, 72802797.39, 1118431.8, 1568.16),
    "100313870641": (65.0025, 98773433.29, 1172853.4954, 346679.3426),
    "100314743761": (64.408, 130449386.88, 2025360.0, None),
    "100319699762": (67.2133333, 46487178.29, 481406.664, 210229.5936),
}


def _find(name):
    for base in (pdf_dir(), "/Users/rahulgupta/Downloads/PDFs/Customs-Import-Docs"):
        p = os.path.join(base, f"{name}.pdf")
        if os.path.exists(p):
            return p
    return None


@pytest.mark.parametrize("doc,expected", sorted(_EXPECTED.items()))
def test_geometry_binds_the_printed_money_block(doc, expected):
    path = _find(doc)
    if not path:
        pytest.skip(f"corpus PDF not available: {doc}")
    cells = geo.read(path)
    rate, total, fc, adj = expected
    assert cells["exchange_rate"].value == pytest.approx(rate, rel=1e-6)
    assert cells["total_customs_value"].value == pytest.approx(total, rel=1e-9)
    assert cells["invoice_price_fc"].value == pytest.approx(fc, rel=1e-9)
    if adj is None:
        assert "adjustment_value" not in cells
    else:
        assert cells["adjustment_value"].value == pytest.approx(adj, rel=1e-9)
    # bound values must outrank vision at merge time
    assert cells["exchange_rate"].confidence >= 0.95


@pytest.mark.parametrize("doc", sorted(_EXPECTED))
def test_cif_identity_closes_on_every_text_layer_doc(doc):
    path = _find(doc)
    if not path:
        pytest.skip(f"corpus PDF not available: {doc}")
    out = geo.cif_identity(geo.read(path))
    assert out is not None
    assert abs(out["gap_pct"]) < 1e-6, f"{doc}: CIF identity did not close"


def test_the_one_real_bundle_in_the_corpus():
    path = _find("100313488550")
    if not path:
        pytest.skip("corpus PDF not available")
    ids = geo.declaration_ids(path)
    assert ids == ["100313488550", "100319576711"]


@pytest.mark.parametrize("doc", ["100313868761", "100313870641",
                                 "100314743761", "100319699762"])
def test_single_declaration_docs_are_not_false_flagged(doc):
    path = _find(doc)
    if not path:
        pytest.skip(f"corpus PDF not available: {doc}")
    assert geo.declaration_ids(path) == [doc]


def test_scanned_cusdec_returns_no_cells_rather_than_guessing():
    """11 of the 16 corpus docs have no text layer on the CUSDEC page. Geometry
    must stay silent there so vision keeps ownership — an empty read is correct,
    a guessed one is not."""
    path = _find("100304950542")
    if not path:
        pytest.skip("corpus PDF not available")
    assert geo.read(path) == {}


def test_read_never_raises_on_a_bad_path():
    assert geo.read("/nonexistent/nope.pdf") == {}
    assert geo.declaration_ids("/nonexistent/nope.pdf") == []


# --------------------------------------------------------------------------- #
# coordinates — what the review UI needs to point at the number on the page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("doc", sorted(_EXPECTED))
def test_bound_cells_carry_the_coordinates_they_were_bound_by(doc):
    """Binding already knows the exact words that produced the value. Keeping the
    rect is free; re-searching the page for the formatted number later is not
    (and fails on '46,487,178.29' vs 46487178.29)."""
    path = _find(doc)
    if not path:
        pytest.skip(f"corpus PDF not available: {doc}")
    cells = geo.read(path)
    for col in ("exchange_rate", "total_customs_value", "invoice_price_fc"):
        c = cells[col]
        assert c.page and c.page >= 1, f"{doc}/{col}: no page"
        assert c.bbox and len(c.bbox) == 4, f"{doc}/{col}: no bbox"
        x0, y0, x1, y1 = c.bbox
        assert x1 > x0 and y1 > y0, f"{doc}/{col}: degenerate bbox {c.bbox}"
        # the row spans at least the value it contains
        rx0, ry0, rx1, ry1 = c.row_bbox
        assert rx0 <= x0 and rx1 >= x1, f"{doc}/{col}: value not inside its row"


def test_value_box_sits_right_of_the_label_not_on_it():
    """The whole failure class this module exists to fix is picking up a number
    from the wrong column. The value must be to the RIGHT of the label text."""
    path = _find("100319699762")
    if not path:
        pytest.skip("corpus PDF not available")
    import fitz
    doc = fitz.open(path)
    cells = geo.read(path)
    c = cells["total_customs_value"]
    page = doc[c.page - 1]
    label = page.search_for("Total customs value")[0]
    doc.close()
    assert c.bbox[0] >= label.x1 - 1, "value box overlaps or precedes its label"


def test_row_is_clipped_to_its_own_label_not_the_neighbouring_column():
    """The CUSDEC prints two label/value pairs on one physical row. The band that
    holds 'Exchange Rate (1) THB - 67.2133333' also holds 'AT ADVANCED INCOME TAX
    929,743'. Caught by rendering the crop and looking at it: a full-band rect
    shows the reviewer two numbers and asks them to confirm one."""
    path = _find("100319699762")
    if not path:
        pytest.skip("corpus PDF not available")
    cells = geo.read(path)
    rate, at = cells["exchange_rate"], cells["advance_income_tax_at"]
    assert "INCOME TAX" not in rate.source.upper(), rate.source
    assert "929,743" not in rate.source, rate.source
    assert "EXCHANGE RATE" not in at.source.upper(), at.source
    # ...and the rects must not overlap either, or the crops collide
    assert rate.row_bbox[0] >= at.row_bbox[2] or at.row_bbox[0] >= rate.row_bbox[2], \
        f"row rects overlap: {at.row_bbox} vs {rate.row_bbox}"
    # each row still contains its own value
    for c in (rate, at):
        assert c.row_bbox[0] <= c.bbox[0] and c.row_bbox[2] >= c.bbox[2]


@pytest.mark.parametrize("doc", ["100313868761", "100313870641",
                                 "100314743761", "100319699762"])
def test_security_fee_reads_the_fee_row_not_the_exemption_column(doc):
    """The tax block prints 'SF SECURITY FEE 20,000' on the left and a separate
    'Security 0' deferral row on the right. A bare 'SECURITY' search matched the
    right-hand one first, so geometry reported a charged fee as 0 — at confidence
    0.97, which outranks vision. Found by rendering the boxes onto the page."""
    path = _find(doc)
    if not path:
        pytest.skip(f"corpus PDF not available: {doc}")
    c = geo.read(path).get("security_fee_sf")
    assert c is not None and c.value == 20000.0, f"{doc}: got {c and c.value}"
    assert "FEE" in c.source.upper(), c.source


def test_scanned_doc_yields_no_coordinates_at_all():
    """No text layer means no measurement. The absence of a box is the signal the
    UI uses to say 'location unknown' — inventing one would be worse than none."""
    path = _find("100304950542")
    if not path:
        pytest.skip("corpus PDF not available")
    assert geo.read(path) == {}


def test_bbox_export_matches_the_v11_field_bbox_contract():
    """pipeline_fast emits the same shape v11/tools/field_bbox.py does, so the
    existing jobs.field_bboxes_json column and the review UI reader work as-is."""
    from rover.pipeline_fast import _bboxes_from_record
    rec = {
        "total_customs_value": Cell(column="total_customs_value", value=1.0,
                                    page=2, bbox=[10.0, 20.0, 60.0, 30.0],
                                    row_bbox=[5.0, 20.0, 90.0, 30.0]),
        "currency": Cell(column="currency", value="THB"),          # no coords
    }
    out = _bboxes_from_record(rec)
    assert set(out) == {"declaration", "items"}
    assert "currency" not in out["declaration"], "unmeasured field must be absent"
    e = out["declaration"]["total_customs_value"]
    assert e == {"page": 2, "x": 10.0, "y": 20.0, "w": 50.0, "h": 10.0,
                 "row": {"x": 5.0, "y": 20.0, "w": 85.0, "h": 10.0}}


def test_bbox_export_tolerates_a_record_with_no_geometry():
    from rover.pipeline_fast import _bboxes_from_record
    assert _bboxes_from_record({}) == {"declaration": {}, "items": {}}
    assert _bboxes_from_record(
        {"currency": Cell(column="currency", value="THB")}
    ) == {"declaration": {}, "items": {}}
