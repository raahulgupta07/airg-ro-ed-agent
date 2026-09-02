"""The DB whitelist must tell "absent" apart from "zero".

Both cases below are real, and both were live before this fix:

  * Commercial Tax prints as 0 on plenty of declarations (100306920561 among
    them). Under `decl.get(a) or decl.get(b)` the zero read as falsy, fell
    through to an absent alias, and stored NULL — which the tax-completeness
    gate then reports as a dropped tax block.

  * On the release order "Adjustment" is a code integer (2) printed beside
    "Adjustment value" (326,139.8592). A null adjustment_value fell through and
    stored that code as an amount.
"""

import pytest

from v11.workflow import _pick


class TestZeroIsAReading:
    def test_a_declared_zero_is_kept(self):
        assert _pick({"commercial_tax": 0.0}, "commercial_tax", "Commercial Tax (CT)") == 0.0

    def test_a_declared_zero_does_not_fall_through_to_the_alias(self):
        # The alias holds a different number; the zero must still win.
        decl = {"commercial_tax": 0, "Commercial Tax (CT)": 5000}
        assert _pick(decl, "commercial_tax", "Commercial Tax (CT)") == 0

    def test_zero_beats_an_absent_alias_instead_of_becoming_none(self):
        assert _pick({"freight_value": 0.0}, "freight_value", "Freight") == 0.0


class TestTheAdjustmentCodeHazard:
    def test_a_null_adjustment_does_not_pick_up_the_code_integer(self):
        # "Adjustment" here is the form's code field, not money. Storing 2 in a
        # money column is a wrong figure AND tightens the CIF tolerance as
        # though a build-up had been supplied. ROVER always sets the key, as
        # None when the row is blank — so this is the live path, not a corner.
        decl = {"adjustment_value": None, "Adjustment": 2}
        assert _pick(decl, "adjustment_value", "Adjustment") is None

    def test_a_real_adjustment_wins_over_the_code(self):
        decl = {"adjustment_value": 326139.8592, "Adjustment": 2}
        assert _pick(decl, "adjustment_value", "Adjustment") == 326139.8592

    def test_the_v7_alias_still_works_when_the_field_is_absent(self):
        # V7 names the build-up amount "Adjustment"; that path must keep working.
        assert _pick({"Adjustment": 44612.82}, "adjustment_value", "Adjustment") == 44612.82


class TestBlanksAndMissing:
    """The fallback turns on key presence, not on the value."""

    def test_a_blank_primary_is_an_answer_and_stops_the_search(self):
        # The engine that owns the primary name read the row and found it
        # empty. An alias must not overrule that — it only speaks for engines
        # that never produced the field at all.
        assert _pick({"a": "", "b": 7}, "a", "b") is None
        assert _pick({"a": None, "b": 7}, "a", "b") is None

    def test_an_absent_primary_hands_over_to_the_alias(self):
        assert _pick({"b": 7}, "a", "b") == 7

    def test_a_blank_alias_is_skipped_when_the_primary_is_absent(self):
        assert _pick({"b": None, "c": 7}, "a", "b", "c") == 7

    def test_all_absent_gives_none(self):
        assert _pick({}, "a", "b") is None
