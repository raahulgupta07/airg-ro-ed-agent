"""The shared date reader, and the ambiguity the corpus already settled.

A survey of the 23 stored declarations found three spellings in one TEXT
column — "2025-06-25", "2024/04/01" and "12/10/2025". The mixed forms are why a
probe reported three dates missing that were in the database all along.
"""

import pytest

import dates


class TestTheThreeFormsThatAppearOnTheseDocuments:
    def test_iso_passes_through(self):
        assert dates.to_iso("2025-06-25") == "2025-06-25"

    @pytest.mark.parametrize("text,expected", [
        ("2025/10/27", "2025-10-27"),   # MACCS release-order page
        ("2025/10/29", "2025-10-29"),
        ("2024/04/01", "2024-04-01"),   # verbatim from a stored row
    ])
    def test_a_leading_four_digit_group_is_the_year(self, text, expected):
        assert dates.to_iso(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("27/09/2029", "2029-09-27"),   # licence page; 27 cannot be a month
        ("19/10/2025", "2025-10-19"),
        ("22/06/2025", "2025-06-22"),
        ("12/10/2025", "2025-10-12"),   # the ambiguous-looking stored row
    ])
    def test_a_trailing_four_digit_group_is_day_first(self, text, expected):
        # Settled by the documents, not by locale guessing: the same bundle
        # prints 27/09/2029 and 19/10/2025, whose leading groups are far too
        # large to be months. So this form is day-first, and 12/10/2025 —
        # which alone would be ambiguous — is 12 October.
        assert dates.to_iso(text) == expected


class TestTimesAndDecoration:
    def test_a_trailing_time_is_dropped(self):
        # The form prints "Declaration date 2024/04/01 13:12"; the column holds
        # a date.
        assert dates.to_iso("2024/04/01 13:12") == "2024-04-01"

    def test_brackets_come_off(self):
        # Licence numbers print their date as "MWDBIL12526000886 (22/06/2025)"
        # and the reader can hand the bracketed group straight over.
        assert dates.to_iso("(22/06/2025)") == "2025-06-22"


class TestBlanks:
    @pytest.mark.parametrize("blank", ["", "  ", "-", "—", "/", "/ /", "/  /",
                                       "N/A", "none", None])
    def test_the_empty_date_box_becomes_none(self, blank):
        # "/  /" is the unfilled date box, and it reaches us verbatim from the
        # text layer.
        assert dates.to_iso(blank) is None


class TestWhatMustBeRefused:
    @pytest.mark.parametrize("text", [
        "MA0259/100405",   # a declaration number, not a date
        "100306920561",
        "PREMIUM DISTRIBUTION COMPANY LIMITED",
        "THB 652,279.7184",
    ])
    def test_non_dates_raise(self, text):
        with pytest.raises(ValueError):
            dates.to_iso(text)

    @pytest.mark.parametrize("text", ["2025-13-01", "2025/02/32", "45/10/2025"])
    def test_impossible_dates_raise(self, text):
        with pytest.raises(ValueError):
            dates.to_iso(text)


class TestTheForgivingWrapper:
    def test_normalise_keeps_what_it_cannot_read(self):
        # The bridge maps a whole record in one pass; a non-date must survive.
        assert dates.normalise("MA0259/100405") == "MA0259/100405"

    def test_normalise_still_converts_a_real_date(self):
        assert dates.normalise("2024/04/01") == "2024-04-01"

    def test_normalise_maps_an_empty_box_to_none(self):
        assert dates.normalise("/  /") is None


class TestDatabaseDelegatesToTheSameReader:
    def test_the_db_coercion_shares_this_behaviour(self):
        import database
        assert database.coerce_date("2024/04/01") == "2024-04-01"
        assert database.coerce_date("12/10/2025") == "2025-10-12"
        assert database.coerce_date("/  /") is None
        with pytest.raises(ValueError):
            database.coerce_date("MA0259/100405")

    def test_date_columns_are_routed_to_the_date_coercion(self):
        import database
        assert "declaration_date" in database.DATE_DECLARATION_COLUMNS
        got = database.coerce_for_column(
            "declaration_date", "2024/04/01", database.NUMERIC_DECLARATION_COLUMNS)
        assert got == "2024-04-01"

    def test_a_text_column_is_still_untouched(self):
        import database
        got = database.coerce_for_column(
            "importer_name", "PREMIUM DISTRIBUTION",
            database.NUMERIC_DECLARATION_COLUMNS)
        assert got == "PREMIUM DISTRIBUTION"
