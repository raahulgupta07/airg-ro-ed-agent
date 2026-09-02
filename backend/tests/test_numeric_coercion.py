"""Money columns are `double precision`; the values reaching them are form text.

ROVER reads amounts straight off the customs form and keeps the printed
formatting — "1,394,615", "THB 652,279.7184". The review UI shows the reviewer
that same string, so both an inline edit and a CHECKS resolve hand Postgres a
value it refuses:

    invalid input syntax for type double precision: "3,362,921"

That is not a hypothetical: it made every numeric CHECKS item unresolvable
(HTTP 500) until the coercion below landed. These tests pin the formats that
actually appear on the documents.
"""

import pytest

import database


class TestFormFormattedAmounts:
    def test_thousands_separators_are_stripped(self):
        assert database.coerce_numeric("1,394,615") == 1394615.0

    def test_separators_survive_a_decimal_part(self):
        assert database.coerce_numeric("46,487,178.29") == 46487178.29

    def test_a_currency_prefix_is_dropped(self):
        # The exact string that broke _num() on the release-order docs.
        assert database.coerce_numeric("THB 652,279.7184") == 652279.7184

    def test_a_currency_suffix_is_dropped(self):
        assert database.coerce_numeric("1,394,615 MMK") == 1394615.0

    def test_surrounding_whitespace_is_ignored(self):
        assert database.coerce_numeric("  20,000  ") == 20000.0

    def test_a_plain_number_passes_through(self):
        assert database.coerce_numeric("67.2133333") == 67.2133333

    def test_numbers_are_returned_untouched(self):
        assert database.coerce_numeric(3262921) == 3262921
        assert database.coerce_numeric(3262921.5) == 3262921.5

    def test_parenthesised_amounts_are_negative(self):
        assert database.coerce_numeric("(1,200.50)") == -1200.50


class TestBlanksAndRefusals:
    @pytest.mark.parametrize("blank", ["", "  ", "-", "--", "/", "N/A", "none"])
    def test_the_markers_a_form_uses_for_empty_become_null(self, blank):
        # A customs form prints "-" for an amount that does not apply. Storing 0
        # would assert the fee is zero, which is a different claim.
        assert database.coerce_numeric(blank) is None

    def test_none_stays_none(self):
        assert database.coerce_numeric(None) is None

    @pytest.mark.parametrize("junk", ["abc", "THB", "n.a", "?"])
    def test_text_with_no_number_raises_rather_than_becoming_zero(self, junk):
        # Silently writing 0 into a money column would be a wrong number that
        # looks authoritative. Loud failure, caught by the route as a 400.
        with pytest.raises(ValueError):
            database.coerce_numeric(junk)


class TestOnlyNumericColumnsAreTouched:
    def test_a_numeric_column_is_coerced(self):
        assert database.coerce_for_column(
            "total_customs_value", "46,487,178.29",
            database.NUMERIC_DECLARATION_COLUMNS) == 46487178.29

    def test_a_text_column_keeps_its_commas(self):
        # importer_name is text; "PREMIUM DISTRIBUTION COMPANY LIMITED, YANGON"
        # must not lose its comma or be parsed as a number.
        name = "PREMIUM DISTRIBUTION COMPANY LIMITED, YANGON"
        assert database.coerce_for_column(
            "importer_name", name,
            database.NUMERIC_DECLARATION_COLUMNS) == name

    def test_a_date_column_is_never_treated_as_a_number(self):
        # The point this test was written for still holds: stripping separators
        # would turn "2026/01/08" into 20260108. What changed is that a date
        # column is no longer merely passed through — it is normalised to ISO,
        # because the same TEXT column was found holding "2025-06-25",
        # "2024/04/01" and "12/10/2025" at once. See tests/test_dates.py.
        got = database.coerce_for_column(
            "release_order_date", "2026/01/08",
            database.NUMERIC_DECLARATION_COLUMNS)
        assert got == "2026-01-08"
        assert not isinstance(got, (int, float))

    def test_declaration_no_is_not_numeric(self):
        # 12-digit ids and the MA-series "MA0259/100405" are identifiers, not
        # amounts. Coercing them would corrupt the slash form.
        assert "declaration_no" not in database.NUMERIC_DECLARATION_COLUMNS
        assert database.coerce_for_column(
            "declaration_no", "MA0259/100405",
            database.NUMERIC_DECLARATION_COLUMNS) == "MA0259/100405"

    def test_item_amounts_are_covered_too(self):
        assert database.coerce_for_column(
            "customs_value_mmk", "111,488.4288",
            database.NUMERIC_ITEM_COLUMNS) == 111488.4288

    def test_item_text_columns_are_not(self):
        assert database.coerce_for_column(
            "hs_code", "0405.10", database.NUMERIC_ITEM_COLUMNS) == "0405.10"


class TestEveryMoneyColumnIsListed:
    def test_the_widened_columns_all_coerce(self):
        # These are exactly the columns migration 0006 widened to double
        # precision. A column that is numeric in the DB but missing from this
        # set fails at write time with the psycopg error above.
        widened = {
            "invoice_price", "invoice_price_fc", "exchange_rate",
            "freight_value", "insurance_value", "adjustment_value",
            "total_customs_value", "import_export_customs_duty",
            "commercial_tax_ct", "advance_income_tax_at",
            "security_fee_sf", "maccs_service_fee_mf", "exemption_reduction",
        }
        assert widened <= database.NUMERIC_DECLARATION_COLUMNS

    def test_every_numeric_column_is_a_real_mapped_column(self):
        mapped = set(database.DECLARATION_FIELD_MAP.values())
        assert database.NUMERIC_DECLARATION_COLUMNS <= mapped
        assert database.NUMERIC_ITEM_COLUMNS <= set(database.ITEM_FIELD_MAP.values())
