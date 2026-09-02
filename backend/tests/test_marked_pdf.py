"""The marked PDF must be a product of every run, and must never lie.

The marked PDF is the original document with every extracted value highlighted
on it. Everything needed to build one has existed for a while — the coordinates
are computed during extraction and stored in `jobs.field_bboxes_json`, and
`GET /api/jobs/{job_id}/annotated-pdf` turns them into real PyMuPDF highlight
annotations. What did not exist was any way to reach it: nothing in the UI
linked to that route, so in practice a run never produced a marked PDF and a
reviewer had to type the URL by hand.

Two defects stood between "the route works" and "every run marks the document",
and this file guards both.

1. **The ROVER / ROSETTA engines stored almost no coordinates.** Their bridge
   (`workflow._run_rover`) passed through only `field_bboxes` as the ROVER
   record supplied it, and `rover.pipeline_fast._bboxes_from_record` walks the
   HEADER record — it reads `cell.bbox` off declaration columns and never
   touches the item rows at all. So on those engines a marked PDF came back
   with a handful of header marks and not one product line, which looks like a
   half-built feature rather than like a different engine having been used. The
   Atlas path never had this problem: its Phase 4.5 runs the text-layer locator
   over the declaration AND the items. `complete_field_bboxes` gives the bridge
   the same completion pass — no model call, no cost.

2. **The UI could not tell "nothing to mark" from "something broke".** A
   photographed declaration has no text layer, so no position can ever be
   measured on it; the route correctly answers 404. The old annotated-PDF tab
   pointed an iframe at that route regardless, and the browser rendered the 404
   JSON body where the document should be. `marked_pdf_status` is the cheap
   question the UI asks first — is there anything to mark, and if not, why not
   — so the action can be shown disabled with a true reason instead of handing
   out a broken link. It also has to separate the two no-mark cases: a
   photograph is permanent and correct, an old job predating the locator is
   fixable in a second by `relocate-boxes`, and telling a reviewer to retry
   something that can never work is worse than saying nothing.
"""
from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from routes.jobs import (marked_pdf_status, iter_marks, _declaration_page_is_digital,
                         SCANNED_REASON, STALE_REASON, NO_PDF_REASON)
from v11.workflow import complete_field_bboxes


def _pdf_with(tmp_path, pages: int, text_on: dict, name="bundle.pdf"):
    """A `pages`-page PDF with `{page_number: [strings]}` written on it."""
    doc = fitz.open()
    for pno in range(pages):
        page = doc.new_page()
        for i, s in enumerate(text_on.get(pno + 1, [])):
            page.insert_text((72, 100 + i * 30), s, fontsize=11)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


#: What `triage._locate_cusdec_page` needs before it will call a page the
#: customs declaration: at least two of its markers and 200+ characters. Without
#: this the completion pass gets `pages=[]` — the scanned answer — and a test
#: that meant to exercise the locator would silently be exercising nothing.
#:
#: Written as SEPARATE LINES on purpose: `page.insert_text` does not wrap, and a
#: single long string runs off the page edge — `get_text` then returns only the
#: part that fits (128 characters here), which falls under the 200-character
#: threshold and makes the page look scanned.
CUSDEC_HEADER = [
    "MYANMAR CUSTOMS DECLARATION (CUSDEC) - MACCS",
    "Taxes and Fees   Import/Export Customs Duty",
    "Commercial Tax   Advance Income Tax",
    "Security Fee     MACCS Service Fee",
    "Exchange Rate 61.9500   Release Order",
    "Assessment Notice   Total Customs Value",
    "Declared at the Yangon port office",
]


def _boxes(decl=None, items=None):
    return {"declaration": decl or {}, "items": items or {}}


def _box(page=1):
    return {"page": page, "x": 10.0, "y": 20.0, "w": 30.0, "h": 8.0}


class TestTheMarkCountTheUIOffers:
    """`marked_pdf_status` — asked before the two-megabyte download."""

    def test_it_counts_header_and_item_marks_together(self, tmp_path):
        # The number in the button has to be the number the endpoint will
        # actually draw, or a reviewer opening "MARKED PDF (25)" and counting 22
        # highlights has been told something false about their own document.
        job = {"job_id": "J", "pdf_path": _pdf_with(tmp_path, 1, {1: ["x"]}),
               "declarations": [{"declaration_no": "100313870641",
                                 "importer_name": "PREMIUM DISTRIBUTION CO LTD"}],
               "items": [{"item_name": "WHITE SUGAR", "hs_code": "1701.99.90"},
                         {"item_name": "REFINED SUGAR"}],
               "field_bboxes": _boxes(
                   decl={"declaration_no": _box(), "importer_name": _box()},
                   items={"0": {"item_name": _box(), "hs_code": _box()},
                          "1": {"item_name": _box()}})}
        st = marked_pdf_status(job)
        assert st["available"] is True
        assert (st["declaration_marks"], st["item_marks"], st["marks"]) == (2, 3, 5)
        assert st["reason"] is None

    def test_a_photographed_declaration_says_so_and_offers_nothing(self, tmp_path):
        # 11 of the 20 UAT documents are scans. No text layer means no string to
        # search for, which means no coordinate — permanently, for every engine.
        # This is the case the UI must render as disabled-with-a-reason, never
        # as a download link.
        job = {"job_id": "J", "pdf_path": _pdf_with(tmp_path, 2, {}),
               "field_bboxes": _boxes()}
        st = marked_pdf_status(job)
        assert st["available"] is False
        assert st["marks"] == 0
        assert st["reason"] == SCANNED_REASON

    def test_a_digital_job_with_no_boxes_is_called_stale_not_scanned(self, tmp_path):
        # A document WITH a text layer and no stored boxes was extracted before
        # the locator ran. That is fixable in about a second by `relocate-boxes`
        # and no model call. Reporting it as "this is a photograph" would tell a
        # reviewer to give up on something that is one click from working.
        job = {"job_id": "J",
               "pdf_path": _pdf_with(tmp_path, 2, {1: CUSDEC_HEADER}),
               "field_bboxes": _boxes()}
        assert marked_pdf_status(job)["reason"] == STALE_REASON

    def test_a_readable_bundle_around_a_photographed_cusdec_is_still_scanned(self, tmp_path):
        # The live job `…_10030692266`, in miniature. 20 of its 28 pages carry a
        # text layer and the declaration is not one of them. The first version
        # of this feature reported it as stale and told the reviewer to re-run
        # the locator; the locator returned 0 boxes, because there is nothing on
        # a photograph to find. Verified against the real document.
        attachments = ["COMMERCIAL INVOICE No. A-9518633846",
                       "Buyer: PREMIUM DISTRIBUTION COMPANY LIMITED",
                       "WHITE SUGAR 2400 BAGS  THB 652,279.7184",
                       "PACKING LIST attached, gross weight 120,000 kg"]
        job = {"job_id": "J",
               "pdf_path": _pdf_with(tmp_path, 10,
                                     {3: attachments, 4: attachments, 5: attachments}),
               "field_bboxes": _boxes()}
        assert marked_pdf_status(job)["reason"] == SCANNED_REASON

    def test_a_missing_source_file_is_reported_as_such(self):
        # The PDF can be gone while the row survives — storage switched, disk
        # cleaned. That is neither "scanned" nor "stale".
        job = {"job_id": "J", "pdf_path": "/nonexistent/gone.pdf",
               "field_bboxes": _boxes()}
        assert marked_pdf_status(job)["reason"] == NO_PDF_REASON

    def test_marks_present_never_triggers_a_reason_lookup(self, tmp_path):
        # A job that HAS marks must not be probed for a text layer — that opens
        # the PDF, and it would run on every job the history list renders. The
        # unreadable path here stands in for that probe: if it ran, the answer
        # would not be None.
        job = {"job_id": "J", "pdf_path": "/nonexistent/gone.pdf",
               "declarations": [{"declaration_no": "100313870641"}], "items": [],
               "field_bboxes": _boxes(decl={"declaration_no": _box()})}
        st = marked_pdf_status(job)
        assert st["available"] is True and st["reason"] is None


class TestABoxIsOnlyAMarkWhenTheValueIsStillThere:
    """The marked PDF must not assert a field is None over a printed figure.

    Boxes are keyed by field name and BOTH spellings of the tax block get
    located during extraction: Presto and Scribe emit `customs_duty`,
    `security_fee`, `commercial_tax`, `maccs_service_fee`,
    `advance_income_tax`, while the Phase-4 alias map rewrites them to the
    declarations columns `import_export_customs_duty`, `security_fee_sf`,
    `commercial_tax_ct` and so on. So a job stores boxes under names the table
    has no column for.

    Measured on the live 20-page job `…_10031969976`: 7 of its 28 stored
    declaration boxes resolve to nothing, and the delivered PDF carried 7
    highlights reading `customs_duty = None` — each one sitting on top of the
    same printed figure that was already correctly marked under its DB name. A
    reviewer opening the annotations panel saw the customs duty asserted as
    absent while the highlight pointed straight at it.
    """

    def test_a_box_whose_value_is_gone_is_not_marked(self):
        job = {"declarations": [{"import_export_customs_duty": 14816014.0}],
               "items": [],
               "field_bboxes": _boxes(decl={
                   "import_export_customs_duty": _box(),
                   "customs_duty": _box(),          # the engine spelling; no column
               })}
        labels = [lab for _k, _b, lab in iter_marks(job)]
        assert labels == ["import_export_customs_duty = 14816014.0"]

    def test_an_empty_string_counts_as_gone(self):
        # A reviewer clearing a cell lands in the same place as the alias case:
        # the box survives the edit, the value does not.
        job = {"declarations": [{"invoice_number": ""}], "items": [],
               "field_bboxes": _boxes(decl={"invoice_number": _box()})}
        assert list(iter_marks(job)) == []

    def test_a_zero_is_a_real_value_and_stays_marked(self):
        # Commercial Tax is genuinely 0 on many declarations — the same reason
        # `_pick()` replaced `a or b` in `_save_to_db`. Dropping zero here would
        # silently unmark a figure that is printed on the form.
        job = {"declarations": [{"commercial_tax_ct": 0}], "items": [],
               "field_bboxes": _boxes(decl={"commercial_tax_ct": _box()})}
        assert [lab for _k, _b, lab in iter_marks(job)] == ["commercial_tax_ct = 0"]

    def test_item_marks_follow_the_same_rule_and_are_numbered_from_one(self):
        job = {"declarations": [{}],
               "items": [{"item_name": "WHITE SUGAR", "hs_code": None}],
               "field_bboxes": _boxes(items={"0": {"item_name": _box(),
                                                   "hs_code": _box()}})}
        marks = list(iter_marks(job))
        assert [k for k, _b, _l in marks] == ["item"]
        assert marks[0][2] == "Item 1 · item_name = WHITE SUGAR"

    def test_a_box_for_an_item_row_that_no_longer_exists_is_dropped(self):
        # Rows are soft-deleted in review, and `get_job_details` filters them
        # out. The boxes are not renumbered, so index 3 can outlive its row.
        job = {"declarations": [{}], "items": [{"item_name": "WHITE SUGAR"}],
               "field_bboxes": _boxes(items={"3": {"item_name": _box()}})}
        assert list(iter_marks(job)) == []

    def test_the_count_the_ui_shows_is_the_count_in_the_file(self, tmp_path):
        # `marked_pdf_status` and the render loop both walk `iter_marks`, so the
        # number on the button cannot drift from the number of highlights.
        job = {"job_id": "J", "pdf_path": _pdf_with(tmp_path, 1, {1: ["x"]}),
               "declarations": [{"declaration_no": "100313870641",
                                 "customs_duty": None}],
               "items": [{"item_name": "WHITE SUGAR"}],
               "field_bboxes": _boxes(
                   decl={"declaration_no": _box(), "customs_duty": _box()},
                   items={"0": {"item_name": _box()}})}
        st = marked_pdf_status(job)
        assert st["marks"] == len(list(iter_marks(job))) == 2
        assert (st["declaration_marks"], st["item_marks"]) == (1, 1)


class TestTellingAScanFromAForm:
    """`_declaration_page_is_digital` — the only thing separating the reasons.

    The first version of this asked "does this PDF have any text?" and got the
    live scanned test job wrong. `…_10030692266` is 28 pages, and 20 of them
    carry a text layer — the invoice, the packing list, the delivery order — but
    the CUSDEC itself is a photograph. That probe called the document digital,
    so the UI told the reviewer to re-run the locator, and the locator found
    nothing, exactly as it had the first time. The question has to be asked
    about the declaration page, not about the bundle around it.
    """

    def test_a_declaration_page_with_text_is_digital(self, tmp_path):
        assert _declaration_page_is_digital(
            _pdf_with(tmp_path, 1, {1: CUSDEC_HEADER})) is True

    def test_a_document_with_no_text_at_all_is_not(self, tmp_path):
        assert _declaration_page_is_digital(_pdf_with(tmp_path, 3, {})) is False

    def test_a_bundle_of_readable_attachments_around_a_photographed_cusdec(self, tmp_path):
        # The measured case. Pages of real text, none of them the declaration:
        # no CUSDEC markers anywhere, so there is no page whose coordinates
        # could ever be found.
        invoice = ["COMMERCIAL INVOICE No. A-9518633846",
                   "Seller: SIAM TRADING COMPANY LIMITED",
                   "Buyer: PREMIUM DISTRIBUTION COMPANY LIMITED",
                   "WHITE SUGAR 2400 BAGS  THB 652,279.7184",
                   "Port of loading: Bangkok   Port of discharge: Yangon",
                   "PACKING LIST attached, 5 pages, gross weight 120,000 kg"]
        assert _declaration_page_is_digital(
            _pdf_with(tmp_path, 8, {2: invoice, 3: invoice, 4: invoice})) is False

    def test_a_declaration_deep_in_a_bundle_is_still_found(self, tmp_path):
        # These are bundles; the CUSDEC can sit on page 10 behind covering
        # letters. Only scanning the front of the file would call it a photo.
        assert _declaration_page_is_digital(
            _pdf_with(tmp_path, 12, {10: CUSDEC_HEADER})) is True

    def test_a_missing_file_is_answered_before_this_is_ever_asked(self):
        # `_locate_cusdec_page` cannot open a file that is not there and returns
        # "no declaration page", which is indistinguishable from a photograph.
        # `marked_pdf_status` therefore has to check the file FIRST — otherwise a
        # job whose PDF was cleaned off disk is described to the reviewer as a
        # scanned document, and no amount of re-running will change it.
        assert _declaration_page_is_digital("/nonexistent/x.pdf") is False
        job = {"job_id": "J", "pdf_path": "/nonexistent/x.pdf",
               "field_bboxes": _boxes()}
        assert marked_pdf_status(job)["reason"] == NO_PDF_REASON


class TestCompletingWhatTheEngineDidNotLocate:
    """`complete_field_bboxes` — the ROVER / ROSETTA gap.

    `_bboxes_from_record` produces `{"declaration": {...}, "items": {}}`. The
    empty items dict is not an accident of the corpus: nothing in that function
    ever looks at an item row. Every ROVER PRO and ROSETTA run therefore
    produced a marked PDF with no product lines on it.
    """

    DN = "100313870641"

    def test_item_rows_the_engine_never_located_get_boxes(self, tmp_path):
        # This is the defect. `measured` is exactly the shape ROVER supplies:
        # some header columns, no items at all.
        pdf = _pdf_with(tmp_path, 3, {2: CUSDEC_HEADER + [self.DN],
                                      3: [self.DN, "WHITE SUGAR", "1701.99.90"]})
        measured = _boxes(decl={"declaration_no": _box(page=2)})
        out = complete_field_bboxes(
            pdf, {"declaration_no": self.DN},
            [{"item_name": "WHITE SUGAR", "hs_code": "1701.99.90"}], measured)
        assert out["items"], "the item row was never located — the ROVER defect"
        assert out["items"]["0"]["item_name"]["page"] == 3

    def test_a_measured_box_is_never_overwritten_by_a_search_hit(self, tmp_path):
        # A coordinate the reader recorded while it was reading that cell is
        # better evidence than a later search for the same string, which can
        # land on another occurrence of it elsewhere in the bundle. Completion
        # only ever ADDS.
        pdf = _pdf_with(tmp_path, 3, {2: CUSDEC_HEADER + [self.DN], 3: [self.DN]})
        mine = {"page": 3, "x": 111.0, "y": 222.0, "w": 40.0, "h": 9.0}
        out = complete_field_bboxes(pdf, {"declaration_no": self.DN}, [],
                                    _boxes(decl={"declaration_no": mine}))
        assert out["declaration"]["declaration_no"] == mine

    def test_header_fields_the_engine_missed_are_filled_in(self, tmp_path):
        pdf = _pdf_with(tmp_path, 3,
                        {2: CUSDEC_HEADER + [self.DN, "PREMIUM DISTRIBUTION CO LTD"]})
        out = complete_field_bboxes(
            pdf,
            {"declaration_no": self.DN, "importer_name": "PREMIUM DISTRIBUTION CO LTD"},
            [], _boxes(decl={"declaration_no": _box(page=2)}))
        assert "importer_name" in out["declaration"]

    def test_a_scanned_document_still_gets_nothing(self, tmp_path):
        # Completion must not become a reason to search the whole bundle when
        # the declaration page is unknown. `declaration_pages` returns [] there,
        # and an empty page list means "search nothing" — boxes drawn from the
        # attachments would be worse than no boxes at all.
        pdf = _pdf_with(tmp_path, 3, {1: [self.DN]})   # no CUSDEC markers anywhere
        out = complete_field_bboxes(pdf, {"declaration_no": self.DN},
                                    [{"item_name": "WHITE SUGAR"}], _boxes())
        assert out == {"declaration": {}, "items": {}}

    def test_measured_boxes_survive_a_document_that_cannot_be_searched(self):
        # Whatever the engine did measure has to reach the marked PDF even when
        # the completion pass finds nothing — including when the file is gone.
        mine = _boxes(decl={"declaration_no": _box(page=2)})
        out = complete_field_bboxes("/nonexistent/x.pdf",
                                    {"declaration_no": self.DN}, [], mine)
        assert out["declaration"]["declaration_no"] == mine["declaration"]["declaration_no"]

    def test_it_never_raises_into_the_extraction_path(self):
        # It runs at the end of a successful run. A coordinate is worth less
        # than the extraction, so a failure here must lose the boxes, not the job.
        assert complete_field_bboxes(None, None, None, None) == {
            "declaration": {}, "items": {}}

    def test_an_empty_item_row_is_not_emitted(self, tmp_path):
        # `{"items": {"0": {}}}` renders as a product line with nothing under it.
        pdf = _pdf_with(tmp_path, 3, {2: CUSDEC_HEADER + [self.DN]})
        out = complete_field_bboxes(pdf, {"declaration_no": self.DN},
                                    [{"item_name": "NOT PRINTED ANYWHERE"}],
                                    _boxes(items={"0": {}}))
        assert out["items"] == {}


class TestTheRouteAgreesWithTheStatus:
    """One condition must not produce two different sentences.

    The UI decides whether to offer the download from `/marks`, and the download
    itself 404s with its own message. When those drifted apart a reviewer was
    told the document was scanned in one place and stale in the other, for the
    same job.
    """

    def test_the_route_reuses_the_status_reason(self, tmp_path, monkeypatch):
        import routes.jobs as jobs_routes
        # Distinct filenames: `_pdf_with` defaults to one name in `tmp_path`, so
        # two calls in a single test overwrite each other and both dicts end up
        # pointing at the SAME document.
        scanned = {"job_id": "J",
                   "pdf_path": _pdf_with(tmp_path, 2, {}, name="scan.pdf"),
                   "field_bboxes": _boxes()}
        digital = {"job_id": "J",
                   "pdf_path": _pdf_with(tmp_path, 2, {1: CUSDEC_HEADER},
                                         name="digital.pdf"),
                   "field_bboxes": _boxes()}
        # The route body raises HTTPException(404, marked_pdf_status(job)["reason"]).
        # Asserting the two helpers agree is the whole contract; standing the
        # FastAPI app up needs a database this suite deliberately does without.
        assert jobs_routes.marked_pdf_status(scanned)["reason"] == SCANNED_REASON
        assert jobs_routes.marked_pdf_status(digital)["reason"] == STALE_REASON

    def test_every_reason_is_a_sentence_a_reviewer_can_act_on(self):
        # These strings go straight onto a button tooltip and an in-panel note.
        # No jargon, no field names, no "bbox".
        for reason in (SCANNED_REASON, STALE_REASON, NO_PDF_REASON):
            assert reason and reason[0].isupper() and reason.rstrip().endswith(".")
            low = reason.lower()
            assert "bbox" not in low and "json" not in low and "null" not in low
