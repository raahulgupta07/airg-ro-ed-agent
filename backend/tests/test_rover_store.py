"""Host-only unit tests for the fleet's pure-python modules.

Covers `rover.store` (persistence + report definitions + the 3 grains) and
`rover.item_text` (text-layer quantity parser + safe fill). No LLM, no PDF,
no docker, no network — everything runs against a throwaway temp store dir
and tiny in-memory fake DocContext objects.
"""
import os
import tempfile
import types

# Point the store at a throwaway dir BEFORE importing it so no test ever
# touches /app/data/rover_store. store reads ROVER_STORE_DIR at import time.
os.environ["ROVER_STORE_DIR"] = tempfile.mkdtemp(prefix="fleet_store_test_")

from rover import store  # noqa: E402
from rover import item_text  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean_store():
    """Wipe documents/ and reports/ so each test starts from empty."""
    for d in (store.DOCS_DIR, store.REPORTS_DIR):
        if os.path.isdir(d):
            for name in os.listdir(d):
                os.remove(os.path.join(d, name))


def _result(pdf="x.pdf", values=None, items=None, needs_review=False):
    return {
        "pdf": pdf,
        "values": values or {},
        "items": items or [],
        "needs_review": needs_review,
    }


class _Page:
    def __init__(self, number, text, lines):
        self.number = number
        self.text = text
        self.lines = lines


class _Ctx:
    """Minimal DocContext stand-in: .pages + .all_lines."""
    def __init__(self, pages):
        self.pages = pages

    @property
    def all_lines(self):
        out = []
        for p in self.pages:
            out.extend(p.lines)
        return out


# --------------------------------------------------------------------------- #
# store — documents
# --------------------------------------------------------------------------- #
def test_save_and_load():
    _clean_store()
    values = {"declaration_no": "999000111222", "currency": "THB"}
    doc_id = store.save_document(_result(values=values))
    loaded = store.load_document(doc_id)
    assert loaded is not None
    assert loaded["values"] == values


def test_doc_id_prefers_declaration_no():
    _clean_store()
    doc_id = store.save_document(
        _result(pdf="somefile.pdf", values={"declaration_no": "100313488550"})
    )
    assert "100313488550" in doc_id
    # and it is genuinely retrievable under that id
    assert store.load_document(doc_id) is not None


def test_overall_rows():
    _clean_store()
    store.save_document(_result(pdf="a.pdf", values={"declaration_no": "111", "currency": "THB"}))
    store.save_document(_result(pdf="b.pdf", values={"declaration_no": "222", "currency": "USD"}))
    rows = store.overall_rows()
    assert len(rows) == 2
    for row in rows:
        assert "declaration_no" in row
        assert "currency" in row
        assert "doc_id" in row


def test_product_rows_and_joined():
    _clean_store()
    store.save_document(_result(
        pdf="withitems.pdf",
        values={"declaration_no": "555", "currency": "THB"},
        items=[{"hs_code": "1806.20.90", "quantity": 10},
               {"hs_code": "1905.31.00", "quantity": 20}],
    ))
    store.save_document(_result(
        pdf="noitems.pdf",
        values={"declaration_no": "666", "currency": "USD"},
        items=[],
    ))

    products = store.product_rows()
    assert len(products) == 2
    for row in products:
        assert row["doc_id"] == "555"
        assert "hs_code" in row

    joined = store.joined_rows()
    # 2 item rows from doc 555 + 1 header-only row from doc 666 (0 items)
    assert len(joined) == 3
    from_555 = [r for r in joined if r["doc_id"] == "555"]
    from_666 = [r for r in joined if r["doc_id"] == "666"]
    assert len(from_555) == 2
    for row in from_555:
        assert row["currency"] == "THB"     # header value carried onto item row
        assert "hs_code" in row
    assert len(from_666) == 1               # header-only joined row
    assert from_666[0]["currency"] == "USD"
    assert "hs_code" not in from_666[0]


# --------------------------------------------------------------------------- #
# store — reports
# --------------------------------------------------------------------------- #
def test_report_filter_and_columns():
    _clean_store()
    store.save_report({
        "name": "t",
        "grain": "overall",
        "columns": ["declaration_no", "currency"],
        "filters": [{"field": "currency", "op": "eq", "value": "THB"}],
        "sort": None,
    })
    store.save_document(_result(pdf="thb.pdf", values={"declaration_no": "111", "currency": "THB"}))
    store.save_document(_result(pdf="usd.pdf", values={"declaration_no": "222", "currency": "USD"}))

    rows = store.run_report("t")
    assert len(rows) == 1                       # only the THB doc survives the filter
    row = rows[0]
    assert set(row.keys()) == {"declaration_no", "currency"}   # projected to 2 cols
    assert row["currency"] == "THB"
    assert row["declaration_no"] == "111"


def test_report_ops():
    _clean_store()
    store.save_document(_result(
        pdf="q.pdf",
        values={"declaration_no": "700"},
        items=[{"hs_code": "1806.20.90", "quantity": 5},
               {"hs_code": "1905.31.00", "quantity": 50}],
    ))

    # gt op on product grain
    store.save_report({
        "name": "gtq",
        "grain": "product",
        "columns": ["hs_code", "quantity"],
        "filters": [{"field": "quantity", "op": "gt", "value": 10}],
        "sort": None,
    })
    gt_rows = store.run_report("gtq")
    assert len(gt_rows) == 1
    assert gt_rows[0]["quantity"] == 50

    # contains op on product grain
    store.save_report({
        "name": "hasq",
        "grain": "product",
        "columns": ["hs_code"],
        "filters": [{"field": "hs_code", "op": "contains", "value": "1806"}],
        "sort": None,
    })
    c_rows = store.run_report("hasq")
    assert len(c_rows) == 1
    assert c_rows[0]["hs_code"] == "1806.20.90"


# --------------------------------------------------------------------------- #
# item_text — fill_quantities
# --------------------------------------------------------------------------- #
def test_fill_quantities_no_overwrite():
    # Text layer would report quantity=999 for the same HS code, but the vision
    # item already has quantity=7 — fill_quantities must NOT overwrite it.
    ctx = _Ctx([_Page(
        1,
        "quantity\n1806.20.90\nCHOCOLATE PRODUCT DESCRIPTION HERE\n999 KG\n",
        ["quantity", "1806.20.90", "CHOCOLATE PRODUCT DESCRIPTION HERE", "999 KG"],
    )])
    vision = [{"hs_code": "1806.20.90", "quantity": 7, "unit": "CTN"}]
    out = item_text.fill_quantities(vision, ctx)
    assert out[0]["quantity"] == 7
    assert out[0]["unit"] == "CTN"


def test_fill_quantities_safe_on_empty():
    empty_ctx = _Ctx([])
    # empty vision list -> empty list
    assert item_text.fill_quantities([], empty_ctx) == []
    # ctx with no item text -> input returned unchanged
    vision = [{"hs_code": "1806.20.90", "quantity": None}]
    out = item_text.fill_quantities(vision, empty_ctx)
    assert out == vision


def test_parse_items_returns_list():
    ctx = _Ctx([_Page(
        1,
        "1806.20.90\nsome cocoa description text here long\n120 KG\n"
        "1905.31.00\nsweet biscuits description text here\n40 CTN\n",
        ["1806.20.90", "some cocoa description text here long", "120 KG",
         "1905.31.00", "sweet biscuits description text here", "40 CTN"],
    )])
    rows = item_text.parse_items(ctx)
    assert isinstance(rows, list)
    # each parsed row exposes the documented shape
    for row in rows:
        assert set(["hs_code", "description", "quantity", "unit"]).issubset(row.keys())
