"""`invoice_price` holds the INVOICE CURRENCY. Pin it — this regressed silently.

When ROVER PRO was bridged in, the mapping took `invoice_price_mmk` for the
`invoice_price` column. Nothing crashed: the column is a float and got a
perfectly valid float. Scored against the manual ledger the field went from
12/13 correct in July to 1/13.

Three separate safety nets missed it, which is why this file exists:
  * no test asserted the UNIT of the column;
  * the CIF gate reads `invoice_price_fc` FIRST, so the arithmetic still closed
    and the document shipped with suspect=[];
  * the Excel writers and the team's ledger read `invoice_price` and quietly
    showed kyats under a THB heading.

The Beta v3 requirement form, §3: "Values are read in the invoice currency
(not MMK)."
"""

import pytest

import numeric
from v11.workflow import invoice_price_fields


def _decl_from(vals):
    """Exercise the REAL bridge mapping, with the same coercion it uses."""
    return invoice_price_fields(vals, numeric.keep_if_unparseable)


# Real values off declaration 100306920561: the form prints
#   Invoice price  A- DAP- THB  652,279.7184
#                        (MMK)   42,005,509.3
REAL = {"invoice_price_fc": "652,279.7184", "invoice_price_mmk": "42,005,509.3"}


class TestTheColumnMeansInvoiceCurrency:
    def test_invoice_price_is_the_foreign_currency_amount(self):
        d = _decl_from(REAL)
        assert d["invoice_price"] == 652279.7184

    def test_invoice_price_is_not_the_kyat_amount(self):
        # The exact regression. Kept as its own named test so a future change
        # that reinstates it fails with an unmistakable name.
        d = _decl_from(REAL)
        assert d["invoice_price"] != 42005509.3

    def test_the_kyat_figure_is_still_kept_separately(self):
        d = _decl_from(REAL)
        assert d["invoice_price_mmk"] == 42005509.3
        assert d["invoice_price_fc"] == 652279.7184

    def test_the_two_columns_reconcile_through_the_rate(self):
        d = _decl_from(REAL)
        assert abs(d["invoice_price_fc"] * 64.398 - d["invoice_price_mmk"]) < 2


class TestWhenOnlyOneAmountIsPrinted:
    def test_kyats_only_still_populates_invoice_price(self):
        # Some forms print no foreign-currency line. Better a kyat figure than a
        # blank column — but it must not happen while an FC amount exists.
        d = _decl_from({"invoice_price_fc": None,
                        "invoice_price_mmk": "1,394,615"})
        assert d["invoice_price"] == 1394615.0

    def test_a_zero_fc_is_not_treated_as_absent(self):
        d = _decl_from({"invoice_price_fc": 0, "invoice_price_mmk": "42,005,509.3"})
        assert d["invoice_price"] == 0.0


class TestTheExportsShowOneInvoicePrice:
    """The team's workbook carries a single "Invoice Price" column.

    That column now holds the INVOICE CURRENCY. The FC/MMK split is kept in the
    database and the review screen — it is what the CIF gate checks the exchange
    rate against — but the team asked for their own 23-column layout, so it is
    deliberately not exported. See tests/test_export_columns.py, which pins the
    sheet itself.
    """

    def test_the_sheet_has_exactly_one_invoice_price_column(self):
        from tests.test_export_columns import DECLARATION_COLUMNS
        priced = [c for c in DECLARATION_COLUMNS if "Invoice Price" in c]
        assert priced == ["Invoice Price"]

    def test_the_split_is_still_stored_even_though_it_is_not_exported(self):
        import database
        assert "invoice_price_fc" in database.DECLARATION_FIELD_MAP
        assert "invoice_price_mmk" in database.DECLARATION_FIELD_MAP
