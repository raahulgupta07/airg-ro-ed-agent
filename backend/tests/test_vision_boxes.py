"""Coordinates reported by the vision model on a photographed declaration.

Half the corpus is a photograph: `field_bbox.py` searches the text layer for a
stored value, a scanned CUSDEC has no text layer, so it locates nothing and the
review screen says "location not known" for every field. The model reading that
page is looking straight at the values, so it is asked where it read them.

Everything here tests the one property that matters: **a box is never invented**.
The model is allowed to say nothing, and saying nothing has to survive all the
way to storage as nothing — not as page 1, not as the whole page, not as a
rectangle nudged into range. A box on the wrong number sends a reviewer to
confirm a customs figure against a figure that is not it, and it looks
deliberate.

No network. Every model response here is synthetic.
"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

vr = pytest.importorskip("v11.tools.vision_rescue")
fitz = pytest.importorskip("fitz")


# A read that yields real values, so `_shape` keeps it.
def _parsed(**over):
    base = {
        "exchange_rate": 61.95,
        "currency": "THB",
        "declaration_no": "100304950542",
        "total_customs_value": 2208855.96,
        "customs_duty": 110442.8,
    }
    base.update(over)
    return base


# ─── _norm_box: the rejection rules ─────────────────────────────────

class TestNormBox:
    def test_a_plausible_box_survives(self):
        assert vr._norm_box([0.62, 0.31, 0.78, 0.335]) == (0.62, 0.31, 0.78, 0.335)

    def test_the_dict_spelling_is_accepted(self):
        assert vr._norm_box({"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.22}) == \
            (0.1, 0.2, 0.3, 0.22)

    @pytest.mark.parametrize("raw", [
        None, "0.1,0.2,0.3,0.4", [], [0.1, 0.2], [0.1, 0.2, 0.3, 0.4, 0.5],
        [0.1, 0.2, 0.3, "x"], {"x0": 0.1},
    ])
    def test_a_malformed_box_is_not_repaired(self, raw):
        assert vr._norm_box(raw) is None

    @pytest.mark.parametrize("raw", [
        [-0.01, 0.2, 0.3, 0.22],     # off the left edge
        [0.1, 0.2, 1.01, 0.22],      # off the right edge
        [0.1, -1, 0.3, 0.22],
    ])
    def test_out_of_range_is_rejected_not_clamped(self, raw):
        # Clamping would turn a coordinate the model got wrong into one that
        # looks measured. There is no way to tell the two apart afterwards.
        assert vr._norm_box(raw) is None

    @pytest.mark.parametrize("raw", [
        [0.5, 0.2, 0.4, 0.22],       # x inverted
        [0.1, 0.5, 0.3, 0.4],        # y inverted
        [0.3, 0.2, 0.3, 0.22],       # zero width
        [0.1, 0.2, 0.3, 0.2],        # zero height
        [0.1, 0.2, 0.102, 0.21],     # narrower than any printed value
    ])
    def test_degenerate_and_inverted_are_rejected(self, raw):
        assert vr._norm_box(raw) is None

    @pytest.mark.parametrize("raw,why", [
        ([0.0, 0.0, 1.0, 1.0], "the whole page"),
        ([0.05, 0.1, 0.95, 0.6], "the whole tax table"),
        ([0.1, 0.1, 0.9, 0.2], "a full-width band"),
    ])
    def test_a_shrug_is_not_a_location(self, raw, why):
        # A box round the form is the model gesturing, not pointing. Storing it
        # would put a highlight over half the sheet and call it evidence.
        assert vr._norm_box(raw) is None, why

    def test_nan_and_infinity_are_rejected(self):
        assert vr._norm_box([float("nan"), 0.2, 0.3, 0.22]) is None
        assert vr._norm_box([0.1, 0.2, float("inf"), 0.22]) is None


# ─── _extract_boxes / _shape: a box never outlives its value ────────

class TestBoxFollowsValue:
    def test_a_box_is_kept_for_a_field_that_was_read(self):
        out = vr._shape(_parsed(boxes={"total_customs_value": [0.6, 0.3, 0.75, 0.32]}), {})
        assert out["_boxes_norm"] == {"total_customs_value": (0.6, 0.3, 0.75, 0.32)}

    def test_no_boxes_key_means_no_boxes(self):
        out = vr._shape(_parsed(), {})
        assert "_boxes_norm" not in out

    def test_a_box_without_a_value_is_dropped(self):
        # Nothing is being claimed at that position, so nothing may be drawn.
        out = vr._shape(_parsed(freight_value=None,
                                boxes={"freight_value": [0.6, 0.4, 0.7, 0.42]}), {})
        assert "freight_value" not in (out.get("_boxes_norm") or {})

    def test_a_value_dropped_by_a_sanity_gate_loses_its_box(self):
        # An exchange rate outside the currency band is junk and `_shape` nulls
        # it. Its coordinate would point at exactly the figure the pipeline just
        # decided not to trust.
        out = vr._shape(_parsed(exchange_rate=2100.0, currency="THB",
                                boxes={"exchange_rate": [0.6, 0.2, 0.7, 0.22]}), {})
        assert out["exchange_rate"] is None
        assert "exchange_rate" not in (out.get("_boxes_norm") or {})

    def test_a_box_for_a_field_nobody_asked_about_is_ignored(self):
        out = vr._shape(_parsed(boxes={"nonsense_field": [0.1, 0.1, 0.2, 0.12]}), {})
        assert "_boxes_norm" not in out

    def test_boxes_are_keyed_by_the_db_spelling(self):
        # The prompt asks for `customs_duty`; the column is
        # `import_export_customs_duty`, and that is what the review screen looks
        # a box up under.
        out = vr._shape(_parsed(boxes={"customs_duty": [0.6, 0.5, 0.72, 0.52]}), {})
        assert set(out["_boxes_norm"]) == {"import_export_customs_duty"}

    def test_the_switch_turns_the_whole_thing_off(self, monkeypatch):
        monkeypatch.setattr(vr, "VISION_RESCUE_BOXES", False)
        out = vr._shape(_parsed(boxes={"total_customs_value": [0.6, 0.3, 0.75, 0.32]}), {})
        assert "_boxes_norm" not in out


# ─── _vote: two reads must agree on the place, not just the value ───

class TestVote:
    def _reads(self, b1, b2, **over):
        a = vr._shape(_parsed(boxes=b1), {})
        b = vr._shape(_parsed(**over, boxes=b2), {})
        return [a, b]

    def test_agreeing_reads_keep_the_box(self):
        voted = vr._vote(self._reads(
            {"total_customs_value": [0.600, 0.300, 0.750, 0.320]},
            {"total_customs_value": [0.604, 0.302, 0.752, 0.322]}))
        assert "total_customs_value" in voted["_boxes_norm"]

    def test_reads_pointing_at_different_rows_keep_nothing(self):
        # Same value, two different places: one of them is wrong and there is no
        # way to tell which. Averaging would produce a box on neither.
        voted = vr._vote(self._reads(
            {"total_customs_value": [0.60, 0.30, 0.75, 0.32]},
            {"total_customs_value": [0.60, 0.44, 0.75, 0.46]}))
        assert not voted.get("_boxes_norm")

    def test_a_box_only_one_read_offered_is_dropped(self):
        voted = vr._vote(self._reads(
            {"total_customs_value": [0.60, 0.30, 0.75, 0.32]}, {}))
        assert not voted.get("_boxes_norm")

    def test_a_disagreed_value_takes_its_box_with_it(self):
        # The vote blanks a field the reads disagreed on. Keeping its box would
        # leave a highlight on a figure the document no longer claims.
        voted = vr._vote(self._reads(
            {"total_customs_value": [0.60, 0.30, 0.75, 0.32]},
            {"total_customs_value": [0.601, 0.301, 0.751, 0.321]},
            total_customs_value=9999999.0))
        assert voted["total_customs_value"] is None
        assert not voted.get("_boxes_norm")

    def test_a_single_read_is_returned_unchanged(self):
        one = vr._shape(_parsed(boxes={"total_customs_value": [0.6, 0.3, 0.75, 0.32]}), {})
        assert vr._vote([one])["_boxes_norm"]


# ─── _to_page_rect: the page's real size, and the right page ────────

def _doc(width=595.0, height=842.0, pages=3):
    d = fitz.open()
    for _ in range(pages):
        d.new_page(width=width, height=height)
    return d


class TestPageRect:
    def test_fractions_become_points_on_this_page(self):
        d = _doc(width=612.0, height=1008.0)          # US Legal, not A4
        try:
            r = vr._to_page_rect((0.25, 0.5, 0.5, 0.55), d[0])
        finally:
            d.close()
        assert r["x"] == pytest.approx(153.0, abs=0.5)
        assert r["y"] == pytest.approx(504.0, abs=0.5)
        assert r["w"] == pytest.approx(153.0, abs=0.5)
        assert r["h"] == pytest.approx(50.4, abs=0.5)

    def test_a4_is_not_assumed(self):
        # The same fractions on two page sizes must not give the same points.
        a, b = _doc(width=595.0), _doc(width=1224.0)
        try:
            ra = vr._to_page_rect((0.5, 0.5, 0.6, 0.52), a[0])
            rb = vr._to_page_rect((0.5, 0.5, 0.6, 0.52), b[0])
        finally:
            a.close(); b.close()
        assert ra["x"] != rb["x"] and ra["w"] != rb["w"]

    def test_the_page_number_is_the_page_that_was_read(self):
        d = _doc(pages=5)
        try:
            assert vr._to_page_rect((0.1, 0.1, 0.2, 0.12), d[3])["page"] == 4
        finally:
            d.close()

    def test_the_box_lands_inside_the_page(self):
        d = _doc()
        try:
            page = d[0]
            r = vr._to_page_rect((0.05, 0.9, 0.4, 0.95), page)
        finally:
            d.close()
        assert r["x"] >= 0 and r["y"] >= 0
        assert r["x"] + r["w"] <= 595.0 + 0.01
        assert r["y"] + r["h"] <= 842.0 + 0.01

    def test_it_says_a_model_reported_it(self):
        # `provenance` / `routes.evidence` report `exact` only for the geometry
        # tier. A reported box must be distinguishable from a measured one for
        # the rest of its life, including after `relocate-boxes` reruns.
        d = _doc()
        try:
            assert vr._to_page_rect((0.1, 0.1, 0.2, 0.12), d[0])["source"] == "vision"
        finally:
            d.close()

    def test_the_shape_matches_what_the_text_locator_produces(self):
        from v11.tools.field_bbox import compute_field_bboxes  # noqa: F401
        d = _doc()
        try:
            r = vr._to_page_rect((0.1, 0.1, 0.2, 0.12), d[0])
        finally:
            d.close()
        assert {"page", "x", "y", "w", "h"} <= set(r)
        assert all(isinstance(r[k], (int, float)) for k in ("x", "y", "w", "h"))


# ─── the prompt ─────────────────────────────────────────────────────

class TestPrompt:
    def test_the_value_half_is_untouched_when_boxes_are_off(self, monkeypatch):
        monkeypatch.setattr(vr, "VISION_RESCUE_BOXES", False)
        assert vr._prompt() == vr.PROMPT

    def test_asking_for_boxes_only_appends(self, monkeypatch):
        monkeypatch.setattr(vr, "VISION_RESCUE_BOXES", True)
        p = vr._prompt()
        assert "boxes" in p
        assert vr._PROMPT_HEAD in p

    def test_the_model_is_told_to_omit_rather_than_guess(self):
        # The prompt is the first place a box can be invented, and the only one
        # this codebase cannot validate its way out of.
        assert "guess" in vr._BOX_SECTION
        assert "Omit" in vr._BOX_SECTION or "left out" in vr._BOX_SECTION


# ─── the tier the reviewer is shown ─────────────────────────────────

class TestLocatedTier:
    """A reported box must never present itself as a measured one.

    `routes.evidence._located` already had three tiers waiting for this, and the
    distinction it draws is the same rule as the no-invented-box rule one step
    further on: `exact` is a claim that the printed string was found at those
    coordinates, and nobody found anything on a photograph. The frontend renders
    `estimated` with its own wording ("the reader reported this spot; it has not
    been measured"), so getting this wrong would put a false claim on screen.
    """

    def _cell(self, writer, box):
        from v11.tools.provenance import build_evidence
        ev = build_evidence({"total_customs_value": 2208855.96},
                            {"total_customs_value": writer}, [],
                            {"declaration": {"total_customs_value": box}})
        return ev["total_customs_value"]

    VISION_BOX = {"page": 1, "x": 368.9, "y": 463.1, "w": 83.3, "h": 18.52,
                  "source": "vision"}

    def test_a_vision_box_is_estimated(self):
        from routes.evidence import _located
        cell = self._cell("vision_cusdec", self.VISION_BOX)
        assert cell["model"] == "vision_cusdec"     # NOT "geometry"
        assert cell["page"] == 1 and cell["bbox"]
        assert _located(cell) == "estimated"

    def test_a_text_layer_box_is_still_exact(self):
        from routes.evidence import _located
        cell = self._cell("textlayer", {"page": 2, "x": 1, "y": 1, "w": 9, "h": 9})
        assert _located(cell) == "exact"

    def test_no_box_is_still_unknown(self):
        from routes.evidence import _located
        from v11.tools.provenance import build_evidence
        ev = build_evidence({"exchange_rate": 61.95},
                            {"exchange_rate": "vision_cusdec"}, [], {})
        assert _located(ev["exchange_rate"]) == "unknown"

    def test_the_queue_row_carries_the_tier(self):
        from routes.evidence import _row
        row = _row("total_customs_value", self._cell("vision_cusdec", self.VISION_BOX))
        assert row["located"] == "estimated" and row["page"] == 1

    def test_the_vision_writer_is_not_a_geometry_writer(self):
        # The one line that decides it. If `vision_cusdec` were ever added to
        # this set, every reported box would start claiming it was measured.
        from v11.tools import provenance
        assert "vision_cusdec" not in provenance._GEOMETRY_WRITERS


# ─── the seam into the pipeline ─────────────────────────────────────

class TestWorkflowSeam:
    def test_only_fields_the_rescue_wrote_get_a_box(self):
        # `workflow` filters `_boxes` by the fields it actually FILLED. If a
        # deterministic read won the field, the stored number came off another
        # page and the vision coordinate points at a different figure.
        src = (BACKEND / "v11/workflow.py").read_text(encoding="utf-8")
        assert 'out["vision_boxes"]' in src
        assert "if k in set(_filled)" in src

    def test_vision_boxes_only_fill_gaps(self):
        src = (BACKEND / "v11/workflow.py").read_text(encoding="utf-8")
        i = src.index('_vb = out.get("vision_boxes")')
        seam = src[i:i + 600]
        assert "_added = [f for f in _vb if f not in _dst]" in seam

    def test_relocating_boxes_does_not_wipe_them(self):
        # `relocate-boxes` recomputes from the text layer. On a photographed
        # declaration that finds nothing, so a plain overwrite would delete the
        # only positions the job has.
        src = (BACKEND / "routes/jobs.py").read_text(encoding="utf-8")
        assert 'bb.get("source") == "vision"' in src
