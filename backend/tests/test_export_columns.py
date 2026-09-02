"""The Excel layout the team actually uses. Pinned so it cannot drift.

Taken verbatim from the workbook the team supplied as the required format:
two sheets, 23 declaration columns and 13 item columns, in this order.

Both writers must agree — `routes/jobs.py` (per-job) and `routes/data.py`
(bulk). And `jobs.py` passes `columns=` to `pd.DataFrame`, which DROPS any key
missing from the list rather than raising, so the list and the row dict have to
be checked together. That exact trap silently omitted "Invoice Price (MMK)"
from the bulk export the same day it was added.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DECLARATION_COLUMNS = [
    "Job", "Declaration No", "Declaration Date",
    "Importer (Name)", "Consignor (Name)",
    "Invoice Number", "Invoice Number (Customs Declaration)",
    "Invoice Number (Commercial Invoice)",
    "Invoice Price", "Freight", "Insurance", "Adjustment",
    "Currency", "Exchange Rate", "Currency 2",
    "Total Customs Value", "Import/Export Customs Duty",
    "Commercial Tax (CT)", "Advance Income Tax (AT)",
    "Security Fee (SF)", "MACCS Service Fee (MF)",
    "Exemption/Reduction", "Processed",
]

ITEM_COLUMNS = [
    "Job", "Item Name", "Customs Duty Rate", "Quantity (1)",
    "Invoice Unit Price", "CIF Unit Price", "Currency", "Commercial Tax %",
    "Exchange Rate (1)", "HS Code", "Origin Country", "Customs Value (MMK)",
    "Processed",
]


def _source(name):
    with open(os.path.join(ROOT, name)) as fh:
        return fh.read()


def _column_list(src, var):
    """The literal list assigned to `var`, in source order."""
    m = re.search(re.escape(var) + r"\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "could not find %s" % var
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))


class TestPerJobWorkbook:
    def test_declaration_sheet_matches_the_team_layout(self):
        cols = _column_list(_source("routes/jobs.py"), "all_decl_cols")
        assert cols == DECLARATION_COLUMNS

    def test_item_sheet_matches_the_team_layout(self):
        cols = _column_list(_source("routes/jobs.py"), "all_item_cols")
        assert cols == ITEM_COLUMNS

    def test_exactly_23_and_13_columns(self):
        src = _source("routes/jobs.py")
        assert len(_column_list(src, "all_decl_cols")) == 23
        assert len(_column_list(src, "all_item_cols")) == 13


class TestBulkExport:
    def test_bulk_declarations_match_the_same_layout(self):
        cols = _column_list(_source("routes/data.py"), "all_decl_cols")
        assert cols == DECLARATION_COLUMNS


class TestTheColumnsListAndTheRowDictAgree:
    """`columns=` drops silently, so a key absent from the list vanishes."""

    @pytest.mark.parametrize("path,var", [
        ("routes/jobs.py", "all_decl_cols"),
        ("routes/data.py", "all_decl_cols"),
    ])
    def test_every_listed_column_is_actually_populated(self, path, var):
        src = _source(path)
        for col in _column_list(src, var):
            # The row dict writes each column as a quoted key followed by ':'
            assert re.search(r"['\"]" + re.escape(col) + r"['\"]\s*:", src), (
                "%s lists %r but never assigns it — the column would export blank"
                % (path, col))


class TestWhatIsDeliberatelyNotExported:
    """Extracted and stored, intentionally absent from the sheet.

    These are not oversights: the team's workbook does not carry them. They stay
    in the database, in the review screen and in the arithmetic gates.
    """

    @pytest.mark.parametrize("col", [
        "Release Order Date", "Arrival Date", "Completion Date",
        "Invoice Price (FC)", "Invoice Price (MMK)",
    ])
    def test_not_in_the_declaration_sheet(self, col):
        assert col not in DECLARATION_COLUMNS

    def test_but_the_underlying_fields_still_exist(self):
        import database
        for f in ("release_order_date", "arrival_date", "completion_date",
                  "invoice_price_fc", "invoice_price_mmk"):
            assert f in database.DECLARATION_FIELD_MAP, f
