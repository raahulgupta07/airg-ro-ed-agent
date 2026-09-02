"""A highlight box must land on the declaration, not on an attachment.

`compute_field_bboxes` feeds `jobs.field_bboxes_json`, which the reviewer UI
reads back as `fieldBboxes` to point at where a value came from. It finds a box
by searching the PDF for the value's own text and taking the FIRST hit anywhere
in the first 50 pages.

These are bundled release orders. One PDF carries the CUSDEC, the import
licence, the invoice, the packing list and the delivery order, and the same
string is printed on several of them. On `100313870641` the customs declaration
is on pages 10-11, and a first-hit search puts `importer_name` and
`invoice_number` on page 6 and `declaration_no` on page 9 — every one of them a
real occurrence of the right text on the wrong document. A reviewer sent to the
invoice to confirm a customs figure is being told something false, which is
worse than showing no box at all.

The fix is to search only the pages the declaration was read from. The engine
already knows them — the page classifier decides them, and `cusdec_rescue`
re-derives them from the text layer — so nothing new has to be computed.

Two levels here, deliberately:

  * `TestOnlyDeclarationPagesAreSearched` builds its own PDF and always runs.
    It is the actual contract.
  * `TestTheRealCorpus` replays the 20 UAT documents and skips when they are not
    on the machine. It is what caught this, and it stays because a synthetic
    two-page PDF cannot reproduce a 28-page bundle.
"""
from __future__ import annotations

import json
import os

import pytest

fitz = pytest.importorskip("fitz")

from v11.tools.field_bbox import compute_field_bboxes
from v11.triage import declaration_pages

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PAGE_MAP = os.path.join(FIXTURES, "declaration_pages.json")

# The corpus is not in the repo — it is customer paperwork. Point these at it to
# run the second class; without them the class skips rather than fails.
CORPUS_DIR = os.environ.get("UAT_CORPUS_DIR", os.path.expanduser("~/Desktop/pgro"))
RUNS_DIR = os.environ.get("UAT_RUNS_DIR", "")

# Values the reviewer is sent to confirm. Restricted to fields whose text is
# printed verbatim on the form, so a miss is a scoping fault and not a
# formatting one.
_CHECKED = ("declaration_no", "importer_name", "consignor_name", "invoice_number")


def _pdf_with(tmp_path, pages: int, text_on: dict):
    """A `pages`-page PDF with `{page_number: [strings]}` written on it."""
    doc = fitz.open()
    for pno in range(pages):
        page = doc.new_page()
        for i, s in enumerate(text_on.get(pno + 1, [])):
            page.insert_text((72, 100 + i * 30), s, fontsize=11)
    path = str(tmp_path / "bundle.pdf")
    doc.save(path)
    doc.close()
    return path


class TestOnlyDeclarationPagesAreSearched:
    """The contract, on a PDF this file builds so it runs anywhere."""

    VALUE = "100313870641"

    def test_the_box_lands_on_the_declaration_not_the_first_match(self, tmp_path):
        # Page 1 is the invoice and page 3 is the CUSDEC. Both print the number.
        pdf = _pdf_with(tmp_path, 3, {1: [self.VALUE], 3: [self.VALUE]})
        out = compute_field_bboxes(pdf, {"declaration_no": self.VALUE}, [],
                                   pages=[3])
        assert out["declaration"]["declaration_no"]["page"] == 3

    def test_a_value_printed_only_off_the_declaration_gets_no_box(self, tmp_path):
        # No box is the honest answer. A box on the attachment would be a claim
        # that the declaration says something it does not.
        pdf = _pdf_with(tmp_path, 3, {1: [self.VALUE]})
        out = compute_field_bboxes(pdf, {"declaration_no": self.VALUE}, [],
                                   pages=[3])
        assert "declaration_no" not in out["declaration"]

    def test_item_boxes_are_scoped_the_same_way(self, tmp_path):
        # The item block is printed on the CUSDEC. An item name matching on the
        # packing list is the same error wearing different clothes.
        pdf = _pdf_with(tmp_path, 3, {1: ["WHITE SUGAR"], 3: ["WHITE SUGAR"]})
        out = compute_field_bboxes(pdf, {}, [{"item_name": "WHITE SUGAR"}],
                                   pages=[3])
        assert out["items"]["0"]["item_name"]["page"] == 3

    def test_several_declaration_pages_are_all_searchable(self, tmp_path):
        # A MACCS declaration runs 1/3-3/3; the tax block is on the last one.
        pdf = _pdf_with(tmp_path, 12, {11: ["PREMIUM DISTRIBUTION COMPANY LIMITED"]})
        out = compute_field_bboxes(
            pdf, {"importer_name": "PREMIUM DISTRIBUTION COMPANY LIMITED"}, [],
            pages=[10, 11])
        assert out["declaration"]["importer_name"]["page"] == 11

    def test_omitting_pages_keeps_the_old_whole_document_behaviour(self, tmp_path):
        # Callers that do not know the page set must not start losing boxes.
        # This is what every historical job and every non-Atlas path relies on.
        pdf = _pdf_with(tmp_path, 3, {2: [self.VALUE]})
        out = compute_field_bboxes(pdf, {"declaration_no": self.VALUE}, [])
        assert out["declaration"]["declaration_no"]["page"] == 2

    def test_an_empty_page_list_is_not_read_as_no_restriction(self, tmp_path):
        # `pages=[]` means the classifier found no declaration page. Falling
        # back to the whole document there would put a box on an attachment in
        # exactly the case we know least about.
        pdf = _pdf_with(tmp_path, 3, {2: [self.VALUE]})
        out = compute_field_bboxes(pdf, {"declaration_no": self.VALUE}, [],
                                   pages=[])
        assert out["declaration"] == {}


class TestMoneyRowsAreFindable:
    """The DB holds `98773433.29`; the form prints `98,773,433.29`.

    `_search_first` searched the stored string verbatim, so no numeric field ever
    matched and the money rows — the ones a reviewer most wants to see on the
    page — got no box at all. Measured on a real digital declaration: 6 of 23
    fields had a box, and every miss was a number.
    """

    def test_a_thousands_separated_amount_is_found(self, tmp_path):
        pdf = _pdf_with(tmp_path, 2, {2: ["98,773,433.29"]})
        out = compute_field_bboxes(pdf, {"total_customs_value": 98773433.29}, [],
                                   pages=[2])
        assert out["declaration"]["total_customs_value"]["page"] == 2

    def test_a_whole_amount_stored_as_a_float_is_found(self, tmp_path):
        # `14816014.0` in the column, `14,816,014` on the form. The trailing
        # `.0` is an artefact of the float column, not something the form prints.
        pdf = _pdf_with(tmp_path, 2, {2: ["14,816,014"]})
        out = compute_field_bboxes(pdf, {"import_export_customs_duty": 14816014.0},
                                   [], pages=[2])
        assert "import_export_customs_duty" in out["declaration"]

    def test_a_plain_unformatted_amount_still_matches(self, tmp_path):
        pdf = _pdf_with(tmp_path, 2, {2: ["20000"]})
        out = compute_field_bboxes(pdf, {"security_fee_sf": 20000.0}, [], pages=[2])
        assert "security_fee_sf" in out["declaration"]

    def test_a_rate_keeps_its_full_precision(self, tmp_path):
        # A ledger rate is 65.0025 and must not be rounded into a near-miss.
        pdf = _pdf_with(tmp_path, 2, {2: ["65.0025"]})
        out = compute_field_bboxes(pdf, {"exchange_rate": 65.0025}, [], pages=[2])
        assert "exchange_rate" in out["declaration"]

    def test_an_identifier_is_never_comma_formatted(self, tmp_path):
        # `100313870641` is a declaration number, not an amount. Searching for
        # `100,313,870,641` would find nothing and, worse, a partial match on a
        # grouped variant would box the wrong figure.
        pdf = _pdf_with(tmp_path, 2, {2: ["100313870641"]})
        out = compute_field_bboxes(pdf, {"declaration_no": "100313870641"}, [],
                                   pages=[2])
        assert out["declaration"]["declaration_no"]["page"] == 2

    def test_the_unformatted_form_is_tried_first(self, tmp_path):
        # When the form prints it plainly, that is the match — no reason to
        # prefer a grouped rendering that happens to appear elsewhere.
        pdf = _pdf_with(tmp_path, 2, {2: ["20000", "20,000"]})
        out = compute_field_bboxes(pdf, {"security_fee_sf": 20000.0}, [], pages=[2])
        assert out["declaration"]["security_fee_sf"]["page"] == 2

    def test_a_date_is_not_treated_as_a_number(self, tmp_path):
        pdf = _pdf_with(tmp_path, 2, {2: ["2025-10-14"]})
        out = compute_field_bboxes(pdf, {"declaration_date": "2025-10-14"}, [],
                                   pages=[2])
        assert "declaration_date" in out["declaration"]

    def test_a_date_stored_iso_is_found_printed_with_slashes(self, tmp_path):
        # `dates.py` normalises every engine's output to ISO so the four date
        # columns stop holding three formats at once. The form prints
        # `2025/10/14`, so searching the stored spelling found nothing and the
        # date rows were the ones left without a box.
        pdf = _pdf_with(tmp_path, 2, {2: ["Declaration date  2025/10/14  10:08"]})
        out = compute_field_bboxes(pdf, {"declaration_date": "2025-10-14"}, [],
                                   pages=[2])
        assert out["declaration"]["declaration_date"]["page"] == 2

    def test_a_date_printed_day_first_is_found(self, tmp_path):
        # The same bundle prints `27/09/2029` elsewhere — day-first is real on
        # these forms, which is why `dates.py` exists at all.
        pdf = _pdf_with(tmp_path, 2, {2: ["Arrival Date : 08/10/2025"]})
        out = compute_field_bboxes(pdf, {"arrival_date": "2025-10-08"}, [],
                                   pages=[2])
        assert out["declaration"]["arrival_date"]["page"] == 2

    def test_a_date_that_is_not_printed_still_gets_no_box(self, tmp_path):
        # The variants must widen the search, not invent a match.
        pdf = _pdf_with(tmp_path, 2, {2: ["nothing dateish here"]})
        out = compute_field_bboxes(pdf, {"declaration_date": "2025-10-14"}, [],
                                   pages=[2])
        assert "declaration_date" not in out["declaration"]


class TestLocatingTheDeclarationPages:
    """`triage.declaration_pages` — which pages the boxes are allowed on."""

    DN = "100313870641"

    def test_the_anchor_page_is_always_included(self, tmp_path):
        pdf = _pdf_with(tmp_path, 3, {2: ["anything"]})
        assert declaration_pages(pdf, 1, None) == [2]

    def test_continuation_sheets_are_found_by_the_declaration_number(self, tmp_path):
        # A MACCS declaration runs 1/3-3/3. Only the first sheet carries the tax
        # block the marker probe looks for; the rest hold the item rows, which
        # is what an item highlight has to point at.
        pdf = _pdf_with(tmp_path, 12, {10: [self.DN], 11: [self.DN], 12: [self.DN]})
        assert declaration_pages(pdf, 9, self.DN) == [10, 11, 12]

    def test_the_number_quoted_before_the_anchor_is_not_included(self, tmp_path):
        # The release-order notification precedes the declaration and quotes its
        # number. On two real documents that alone pulled in page 9 and page 13.
        pdf = _pdf_with(tmp_path, 12, {9: [self.DN], 10: [self.DN], 11: [self.DN]})
        assert declaration_pages(pdf, 9, self.DN) == [10, 11]

    def test_no_anchor_means_no_pages(self, tmp_path):
        # Every scanned declaration lands here. A scan has no text layer, so no
        # box could be found on it; searching the rest of the bundle would
        # return boxes drawn only from attachments.
        pdf = _pdf_with(tmp_path, 3, {1: [self.DN]})
        assert declaration_pages(pdf, None, self.DN) == []

    def test_a_short_number_is_not_used_to_extend(self, tmp_path):
        # "12" appears in a date, a page number and a quantity. Matching on it
        # would pull in most of the bundle.
        pdf = _pdf_with(tmp_path, 4, {2: ["12"], 3: ["12 boxes"], 4: ["page 12"]})
        assert declaration_pages(pdf, 1, "12") == [2]

    def test_an_out_of_range_anchor_is_refused(self, tmp_path):
        pdf = _pdf_with(tmp_path, 2, {1: [self.DN]})
        assert declaration_pages(pdf, 40, self.DN) == []

    def test_a_missing_file_returns_no_pages(self):
        assert declaration_pages("/nonexistent/x.pdf", 0, self.DN) == []


@pytest.mark.skipif(not os.path.isdir(CORPUS_DIR),
                    reason=f"UAT corpus not present at {CORPUS_DIR}")
class TestTheRealCorpus:
    """The 20 bundled release orders, against the hand-checked page map.

    `fixtures/declaration_pages.json` records which pages of each document hold
    the customs declaration. It was built by reading the documents, not by
    asking the pipeline, so it is independent of the code under test.
    """

    @staticmethod
    def _page_map():
        with open(PAGE_MAP, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _stored_declarations():
        """`{pdf_name: declaration_row}` from a scored UAT run, if one is around."""
        if not RUNS_DIR or not os.path.isdir(RUNS_DIR):
            return {}
        out = {}
        for fn in sorted(os.listdir(RUNS_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(RUNS_DIR, fn), encoding="utf-8") as fh:
                    run = json.load(fh)
                rows = run["review"]["declarations"]
            except Exception:
                continue
            if rows:
                out[os.path.basename(run["pdf"])] = rows[0]
        return out

    def test_the_page_map_covers_every_document_in_the_corpus(self):
        mapped = set(self._page_map())
        on_disk = {f for f in os.listdir(CORPUS_DIR) if f.lower().endswith(".pdf")}
        assert on_disk - mapped == set(), "corpus grew; extend declaration_pages.json"

    def test_no_box_lands_outside_the_declaration(self):
        stored = self._stored_declarations()
        if not stored:
            pytest.skip("set UAT_RUNS_DIR to a directory of scored run JSON")

        strays = []
        for pdf_name, decl in sorted(stored.items()):
            entry = self._page_map().get(pdf_name)
            if not entry:
                continue
            pages = entry["cusdec"]
            path = os.path.join(CORPUS_DIR, pdf_name)
            if not os.path.exists(path):
                continue
            values = {f: decl.get(f) for f in _CHECKED if decl.get(f)}
            out = compute_field_bboxes(path, values, [], pages=pages)
            for field, box in out["declaration"].items():
                if box["page"] not in pages:
                    strays.append(f"{pdf_name}: {field} -> page {box['page']}, "
                                  f"declaration is on {pages}")
        assert strays == [], "\n".join(strays)

    def test_the_locator_never_picks_a_page_outside_the_declaration(self):
        """The pipeline's own page finder, against the hand-read map.

        The test above is handed the right pages. This one lets
        `declaration_pages` work them out the way the worker does, so a
        regression in the locator cannot hide behind a correct fixture.

        Under-selecting is allowed and expected — 11 of the 20 declarations are
        scanned, and those correctly yield no pages at all. Selecting a page
        that is NOT the declaration is the failure.
        """
        stored = self._stored_declarations()
        if not stored:
            pytest.skip("set UAT_RUNS_DIR to a directory of scored run JSON")

        strays, located = [], 0
        for pdf_name, decl in sorted(stored.items()):
            entry = self._page_map().get(pdf_name)
            path = os.path.join(CORPUS_DIR, pdf_name)
            if not entry or not os.path.exists(path):
                continue
            from v11.triage import _locate_cusdec_page
            anchor, _digital = _locate_cusdec_page(path)
            got = declaration_pages(path, anchor, decl.get("declaration_no"))
            if got:
                located += 1
            outside = [p for p in got if p not in entry["cusdec"]]
            if outside:
                strays.append(f"{pdf_name}: picked {outside}, "
                              f"declaration is on {entry['cusdec']}")
        assert strays == [], "\n".join(strays)
        assert located >= 9, (f"the locator found pages for only {located} "
                              f"documents; 9 digital declarations are known-locatable")
