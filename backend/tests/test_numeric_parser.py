"""The shared amount parser, and the regressions it must not introduce.

Ten copies of `_num()` all did `float(str(v).replace(",", ""))`. That drops any
amount printed with its currency — "THB 652,279.7184" — silently, in a different
way at each site. These tests pin both the fix and the thing the obvious fix
(strip every non-digit) would break: dates and slash-form declaration numbers.
"""

import pytest

import numeric


class TestAmountsAsPrintedOnTheForm:
    @pytest.mark.parametrize("text,expected", [
        ("1,394,615", 1394615.0),
        ("46,487,178.29", 46487178.29),
        ("111,488.4288", 111488.4288),
        ("67.2133333", 67.2133333),
        ("20000", 20000.0),
        ("0", 0.0),
    ])
    def test_plain_and_separated_amounts(self, text, expected):
        assert numeric.parse_amount(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("THB 652,279.7184", 652279.7184),   # the string that started this
        ("THB 1626905.9000", 1626905.9),     # verbatim from the text layer
        ("1626905.9000 THB", 1626905.9),     # the suffix form, also in the wild
        ("USD 22,136.75", 22136.75),
        ("1,394,615 MMK", 1394615.0),
        ("$1,200.00", 1200.0),
        ("K 30,000", 30000.0),
    ])
    def test_a_currency_token_at_either_end_is_dropped(self, text, expected):
        assert numeric.parse_amount(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("THB- 481,406.664", 481406.664),     # invoice price, every form
        ("THB - 210,229.5936", 210229.5936),  # adjustment value
    ])
    def test_the_label_joiner_dash_is_not_a_minus_sign(self, text, expected):
        # The value block renders as "Invoice price A - CIF - THB- 481,406.664".
        # Reading that dash as a sign would make the commonest amount on the
        # document negative.
        assert numeric.parse_amount(text) == expected

    def test_a_tight_minus_after_a_currency_is_still_negative(self):
        # A real sign binds to its digits. This is the only signal separating it
        # from the joiner above, so it is pinned deliberately.
        assert numeric.parse_amount("THB -1,200.50") == -1200.50

    def test_accounting_negatives(self):
        assert numeric.parse_amount("(1,200.50)") == -1200.50
        assert numeric.parse_amount("(THB 1,200.50)") == -1200.50

    def test_explicit_sign_survives(self):
        assert numeric.parse_amount("-210,229.5936") == -210229.5936
        assert numeric.parse_amount("+2") == 2.0

    def test_numbers_pass_straight_through(self):
        assert numeric.parse_amount(3262921) == 3262921.0
        assert numeric.parse_amount(64.408) == 64.408


class TestBlanks:
    @pytest.mark.parametrize("blank", ["", "  ", "-", "--", "—", "–", "/",
                                       "N/A", "na", "None", "null"])
    def test_the_markers_a_form_uses_for_empty_become_none(self, blank):
        # A dash on a customs form means "does not apply". Storing 0 would be a
        # different claim: that the fee was charged and came to zero.
        assert numeric.parse_amount(blank) is None

    def test_none_stays_none(self):
        assert numeric.parse_amount(None) is None


class TestWhatMustNotBeCoerced:
    """The regressions a strip-every-non-digit parser would introduce."""

    @pytest.mark.parametrize("date", ["2026/01/08", "2026-01-05", "08/01/2026"])
    def test_dates_are_not_numbers(self, date):
        # "2026/01/08" -> 20260108 would sail into a date column as a number.
        with pytest.raises(ValueError):
            numeric.parse_amount(date)

    def test_the_ma_series_declaration_no_is_not_a_number(self):
        # MA0259/100405 is an identifier. Mashing it to 2590100405 is corruption.
        with pytest.raises(ValueError):
            numeric.parse_amount("MA0259/100405")

    def test_an_importer_name_is_not_a_number(self):
        with pytest.raises(ValueError):
            numeric.parse_amount("PREMIUM DISTRIBUTION COMPANY LIMITED")

    def test_an_invoice_reference_is_not_a_number(self):
        with pytest.raises(ValueError):
            numeric.parse_amount("A-FN251202")

    def test_a_bare_currency_code_is_not_a_number(self):
        with pytest.raises(ValueError):
            numeric.parse_amount("THB")

    def test_a_whole_label_line_is_refused_not_guessed_at(self):
        # "AD - THB - 210,229.5936" is a label plus a value. Clipping the label
        # off is the row-band's job (geometry does it); a parser that chewed
        # through arbitrary prefixes is exactly how "2026/01/08" becomes a
        # number. Refusing loses the value visibly instead of inventing one.
        with pytest.raises(ValueError):
            numeric.parse_amount("AD - THB - 210,229.5936")
        with pytest.raises(ValueError):
            numeric.parse_amount("Total customs value 46,487,178.29")

    def test_a_parenthesised_currency_header_is_not_an_amount(self):
        # "19.Value(THB)" is a column header in the item table's text layer.
        with pytest.raises(ValueError):
            numeric.parse_amount("19.Value(THB)")

    @pytest.mark.parametrize("text", [
        "A- 9518633846",      # invoice reference, straight out of the corpus
        "A- 960773210",
        "Rate 64.408",        # a ROVER source string: label plus value
        "AD 210,229.5936",
        "No 100319699762",
    ])
    def test_a_short_word_is_not_a_currency(self, text):
        # Replaying the stored corpus caught this: a `[A-Za-z]{1,4}` prefix rule
        # turned the invoice reference "A- 9518633846" into 9518633846.0. Only
        # real currency tokens may be stripped.
        with pytest.raises(ValueError):
            numeric.parse_amount(text)

    def test_the_bridge_leaves_those_references_untouched(self):
        # This is the path that matters: the ROVER->DB mapper must hand the
        # invoice reference back as-is, not as a float.
        assert numeric.keep_if_unparseable("A- 9518633846") == "A- 9518633846"

    def test_booleans_are_rejected(self):
        # bool subclasses int; True would quietly become 1.0.
        with pytest.raises(ValueError):
            numeric.parse_amount(True)

    def test_an_ambiguous_european_amount_is_refused(self):
        # "1.394.615" could be 1394615 or garbage. Guessing risks a wrong figure
        # in a money column, so it raises rather than picking one.
        with pytest.raises(ValueError):
            numeric.parse_amount("1.394.615")


class TestTheQuietWrappers:
    def test_to_float_returns_none_instead_of_raising(self):
        assert numeric.to_float("PREMIUM DISTRIBUTION") is None
        assert numeric.to_float("THB 652,279.7184") == 652279.7184

    def test_to_float_honours_a_default(self):
        assert numeric.to_float("nonsense", default=0.0) == 0.0

    def test_to_float_maps_blanks_to_the_default_too(self):
        # The old _num() returned None for "-"; callers branch on None, not 0.
        assert numeric.to_float("-") is None

    def test_keep_if_unparseable_returns_the_original(self):
        # The ROVER bridge maps a whole record in one pass — a date has to come
        # out the other side unchanged, not as None.
        assert numeric.keep_if_unparseable("2026/01/08") == "2026/01/08"
        assert numeric.keep_if_unparseable("MA0259/100405") == "MA0259/100405"

    def test_keep_if_unparseable_still_coerces_real_amounts(self):
        assert numeric.keep_if_unparseable("1,394,615") == 1394615.0
        assert numeric.keep_if_unparseable("THB 652,279.7184") == 652279.7184

    def test_keep_if_unparseable_maps_a_blank_to_none(self):
        # A dash must reach the DB as NULL, not as the literal string "-".
        assert numeric.keep_if_unparseable("-") is None


class TestDatabaseDelegatesToTheSameParser:
    def test_the_db_coercion_shares_this_behaviour(self):
        import database
        assert database.coerce_numeric("THB 652,279.7184") == 652279.7184
        assert database.coerce_numeric("-") is None
        with pytest.raises(ValueError):
            database.coerce_numeric("2026/01/08")
