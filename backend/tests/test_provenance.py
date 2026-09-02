"""Evidence must say where a figure came from, and must not overstate it.

`_field_engine` has been written by four stages and read by nobody — it went to
the worker log and stopped there. A reviewer confirming a customs total could not
tell whether it was read off the page's own text, agreed on by the item rows, or
worked out from the CIF identity, and those three deserve different amounts of
attention.

The tests that matter here are the ones about what evidence must NOT do: invent a
confidence number, put an entry on a blank field, or lose a value to the two key
spaces the way the tax block was lost at the save whitelist.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from v11.tools.provenance import (CORROBORATED, DERIVED, READ, _ALIASES,
                                  build_evidence)

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(BACKEND, "v11", "workflow.py")


class TestWhatTheReviewerIsTold:
    def test_a_read_value_is_marked_read(self):
        ev = build_evidence({"importer_name": "PREMIUM DISTRIBUTION COMPANY LIMITED"},
                            {"importer_name": "presto"})
        assert ev["importer_name"]["trust"] ==READ

    def test_a_corroborated_total_is_distinguished_from_a_single_reading(self):
        # The whole point. Two independent readings agreeing is a different fact
        # from one model reading it once, and the screen has to show which.
        ev = build_evidence({"total_customs_value": 64691431.29},
                            {"total_customs_value": "item_sum_corroborated"})
        assert ev["total_customs_value"]["trust"] ==CORROBORATED

    def test_a_derived_value_says_it_was_not_read(self):
        ev = build_evidence({"adjustment_value": 1051.894},
                            {"adjustment_value": "derived_cif"})
        assert ev["adjustment_value"]["trust"] ==DERIVED
        assert "not read" in ev["adjustment_value"]["note"].lower()

    def test_a_scanned_page_reading_says_there_is_nothing_to_check_against(self):
        ev = build_evidence({"exchange_rate": 296.354},
                            {"exchange_rate": "vision_cusdec"})
        note = ev["exchange_rate"]["note"].lower()
        assert "photograph" in note or "no text" in note

    def test_no_confidence_number_is_invented(self):
        # There is no honest number for "a vision model read this once". A
        # percentage looks like a measurement; a status word does not.
        ev = build_evidence({"total_customs_value": 1.0},
                            {"total_customs_value": "v7"})
        assert "confidence" not in ev["total_customs_value"]


class TestWhatEvidenceMustNotClaim:
    def test_a_field_with_no_provenance_gets_no_entry(self):
        # Absence is documented as "no evidence". A default entry would read as
        # an account of a reading that never happened.
        ev = build_evidence({"importer_name": "X", "consignor_name": "Y"},
                            {"importer_name": "presto"})
        assert set(ev) == {"importer_name"}

    def test_a_blank_field_gets_no_entry(self):
        ev = build_evidence({"security_fee_sf": None},
                            {"security_fee_sf": "cusdec_text"})
        assert ev == {}

    def test_a_declared_zero_is_a_reading_and_keeps_its_entry(self):
        # Commercial Tax is genuinely 0 on many declarations. Dropping it here
        # would repeat the `a or b` bug that stored those zeros as NULL.
        ev = build_evidence({"commercial_tax_ct": 0},
                            {"commercial_tax_ct": "cusdec_text"})
        assert ev["commercial_tax_ct"]["value"] == 0

    def test_internal_keys_are_not_published(self):
        ev = build_evidence({"_cross_val_passed": 1, "importer_name": "X"},
                            {"_cross_val_passed": "presto", "importer_name": "presto"})
        assert set(ev) == {"importer_name"}

    def test_an_unknown_writer_is_reported_not_swallowed(self):
        # A new stage that tags with a name this module has not learned yet must
        # still show up, under its own name, rather than vanishing.
        ev = build_evidence({"exchange_rate": 1.0}, {"exchange_rate": "brand_new"})
        assert ev["exchange_rate"]["writer"] == "brand_new"

    def test_it_never_raises_on_junk(self):
        assert build_evidence(None, None, None) == {}
        assert build_evidence({"a": 1}, "not a dict") == {}


class TestTheTwoKeySpaces:
    """The failure that dropped a verified tax read at the save whitelist.

    `_field_engine` is keyed the way the WRITER spelled the field. The typed lane
    writes `customs_duty`; the deterministic rescue writes
    `import_export_customs_duty`. Evidence is keyed by DB column, so it has to
    resolve both.
    """

    def test_an_engine_spelling_lands_under_the_db_column(self):
        ev = build_evidence({"import_export_customs_duty": 14816014.0},
                            {"customs_duty": "presto"})
        assert "import_export_customs_duty" in ev

    def test_the_db_spelling_wins_when_both_are_recorded(self):
        # `_pick` takes the DB name first so a rescue value beats the engine's.
        # Evidence has to agree, or the screen credits the wrong reader.
        ev = build_evidence({"security_fee_sf": 20000.0},
                            {"security_fee_sf": "cusdec_text", "security_fee": "v7"})
        assert ev["security_fee_sf"]["writer"] == "cusdec_text"

    def test_the_invoice_price_rename_resolves(self):
        # `invoice_price_fc` and `invoice_price` are the same quantity under two
        # names — the regression that cost 10 fields.
        ev = build_evidence({"invoice_price_fc": 1172853.4954},
                            {"invoice_price": "presto"})
        assert "invoice_price_fc" in ev

    def test_the_alias_table_matches_the_real_save_whitelist(self):
        """Read `_save_to_db` and check no `_pick` group has drifted.

        Read rather than imported: `v11.workflow` pulls in the whole pipeline.
        A hand-copied second map going stale is exactly how the tax block was
        lost, so this compares against the code that actually runs.
        """
        with open(WORKFLOW, encoding="utf-8") as fh:
            src = fh.read()

        missing = []
        for call in re.finditer(r'_pick\(\s*decl\s*,\s*((?:"[^"]*"\s*,?\s*)+)\)', src):
            names = re.findall(r'"([^"]*)"', call.group(1))
            if len(names) < 2:
                continue                     # no alias to keep in step with
            db_name, aliases = names[0], names[1:]
            known = _ALIASES.get(db_name)
            if known is None:
                missing.append(f"{db_name}: absent from _ALIASES")
                continue
            for a in aliases:
                if a not in known:
                    missing.append(f"{db_name}: alias {a!r} not in _ALIASES")
        assert missing == [], "\n".join(missing)


class TestItFeedsTheChecksQueue:
    """The Checks surface already exists — it was starved, not missing.

    `routes/evidence.py` queues, crops and resolves per-field evidence, and has
    since ROVER. It reads `evidence_json`, which only ROVER ever wrote, so once
    ROVER was retired the queue went permanently empty. These pin the shape that
    module actually consumes.
    """

    @staticmethod
    def _flagged(ev):
        from routes.evidence import _flagged
        return set(_flagged(ev))

    def test_a_vision_read_of_a_money_field_reaches_the_queue(self):
        ev = build_evidence({"total_customs_value": 64691431.29},
                            {"total_customs_value": "vision_cusdec"})
        assert self._flagged(ev) == {"total_customs_value"}

    def test_a_text_layer_read_does_not(self):
        ev = build_evidence({"total_customs_value": 1.0},
                            {"total_customs_value": "cusdec_text"})
        assert self._flagged(ev) == set()

    def test_a_corroborated_total_does_not(self):
        # Two independent readings agreeing is the strongest evidence the
        # pipeline can produce without a human. Queuing it wastes the reviewer.
        ev = build_evidence({"total_customs_value": 1.0},
                            {"total_customs_value": "item_sum_corroborated"})
        assert self._flagged(ev) == set()

    def test_a_derived_value_reaches_the_queue(self):
        ev = build_evidence({"adjustment_value": 1051.894},
                            {"adjustment_value": "derived_cif"})
        assert self._flagged(ev) == {"adjustment_value"}

    def test_a_low_stakes_field_is_not_queued_even_when_read_by_vision(self):
        # On a scanned declaration nearly every field is a single vision read.
        # Flagging all twenty makes the queue unreadable, and an unread queue
        # protects nobody.
        ev = build_evidence({"consignor_name": "ASIATIC MART HOLDING PTE LTD"},
                            {"consignor_name": "vision_cusdec"})
        assert self._flagged(ev) == set()
        assert ev["consignor_name"]["note"]      # the account is still there

    def test_a_gate_disagreement_outranks_a_clean_read(self):
        # An arithmetic check that ran and disagreed beats how the value was
        # obtained. A text-layer exchange rate the CIF identity refuses is still
        # wrong.
        ev = build_evidence({"exchange_rate": 64.398},
                            {"exchange_rate": "cusdec_text"},
                            ["exchange_rate_suspect"])
        assert ev["exchange_rate"]["status"] == "suspect"
        assert self._flagged(ev) == {"exchange_rate"}

    def test_a_measured_box_is_reported_as_exact(self):
        from routes.evidence import _located
        boxes = {"declaration": {"security_fee_sf":
                                 {"page": 11, "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}}}
        ev = build_evidence({"security_fee_sf": 20000.0},
                            {"security_fee_sf": "cusdec_text"}, [], boxes)
        assert _located(ev["security_fee_sf"]) == "exact"
        assert ev["security_fee_sf"]["page"] == 11

    def test_a_box_is_never_claimed_for_a_writer_that_did_not_measure(self):
        # `_located` reports `exact` on `model == "geometry"`. A vision read has
        # coordinates only by coincidence of the text search, so it must not
        # claim to have been measured.
        from routes.evidence import _located
        boxes = {"declaration": {"exchange_rate":
                                 {"page": 2, "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}}}
        ev = build_evidence({"exchange_rate": 64.398},
                            {"exchange_rate": "vision_cusdec"}, [], boxes)
        assert _located(ev["exchange_rate"]) == "estimated"

    def test_no_box_means_no_page_rather_than_page_one(self):
        # The crop endpoint defaults to page 1 when a cell has no page. That is
        # honest as a fallback and dishonest as a stored value.
        ev = build_evidence({"exchange_rate": 64.398},
                            {"exchange_rate": "vision_cusdec"})
        assert "page" not in ev["exchange_rate"]

    def test_the_queue_row_renders_without_a_confidence_number(self):
        from routes.evidence import _row
        ev = build_evidence({"total_customs_value": 64691431.29},
                            {"total_customs_value": "vision_cusdec"})
        row = _row("total_customs_value", ev["total_customs_value"])
        assert "confidence" not in row
        assert row["reason"]                      # the note became the reason
        assert row["label"] == "Total customs value"


class TestGateFlags:
    def test_a_field_specific_flag_annotates_only_that_field(self):
        ev = build_evidence({"exchange_rate": 296.354, "importer_name": "X"},
                            {"exchange_rate": "vision_cusdec", "importer_name": "presto"},
                            ["exchange_rate_suspect"])
        assert "does not close" in ev["exchange_rate"]["note"]
        assert "does not close" not in ev["importer_name"]["note"]

    def test_a_document_wide_flag_reaches_every_field(self):
        # A reviewer reads one row at a time. A statement about the whole read is
        # useless if it only appears somewhere else on the screen.
        ev = build_evidence({"exchange_rate": 1.0, "importer_name": "X"},
                            {"exchange_rate": "v7", "importer_name": "v7"},
                            ["vision_rescue_empty"])
        assert all("could not be read" in e["note"] for e in ev.values())

    def test_the_flag_is_recorded_alongside_the_note(self):
        ev = build_evidence({"adjustment_value": 1.0},
                            {"adjustment_value": "derived_cif"},
                            ["adjustment_derived"])
        assert ev["adjustment_value"]["flags"] == ["adjustment_derived"]

    def test_an_unknown_flag_is_ignored_rather_than_shown_raw(self):
        ev = build_evidence({"exchange_rate": 1.0}, {"exchange_rate": "v7"},
                            ["some_new_internal_flag"])
        assert "some_new_internal_flag" not in ev["exchange_rate"]["note"]


class TestTheWritersTagThemselves:
    """Every stage that overwrites a value must say so.

    `cusdec_rescue` runs LAST and wins over everything upstream. Untagged, the
    review screen credits whichever earlier lane happens to share the name.
    """

    def test_the_deterministic_rescue_tags_its_fields(self):
        path = os.path.join(BACKEND, "v11", "tools", "cusdec_rescue.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        assert '"cusdec_text"' in src
        assert "_field_engine" in src

    def test_the_vision_rescue_tags_its_fields(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            src = fh.read()
        assert '"vision_cusdec"' in src

    def test_every_writer_name_in_the_pipeline_is_described(self):
        """A tag the pipeline writes but this module cannot describe is a hole.

        It would surface to the reviewer as a bare internal name.
        """
        from v11.tools import provenance
        described = set(provenance._SOURCES)
        written = set()
        for rel in ("v11/workflow.py", "v11/tools/cusdec_rescue.py"):
            with open(os.path.join(BACKEND, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                # `_fe[k] = "presto"` / `_fe365["x"] = "item_sum_corroborated"`
                if (isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                        and any(isinstance(t, ast.Subscript)
                                and isinstance(t.value, ast.Name)
                                and t.value.id.startswith("_fe")
                                for t in node.targets)):
                    written.add(node.value.value)
        assert written, "no provenance tags found — did the writers change shape?"
        assert written <= described, f"undescribed writer tags: {sorted(written - described)}"
