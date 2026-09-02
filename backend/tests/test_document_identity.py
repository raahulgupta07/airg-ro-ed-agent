"""A page's DOCUMENT is a different question from how it is filled in.

Job `JOB_20260804_060507_662ec932_0259100560_` stored 19 product rows for a
declaration that has four. Seven of them were Belgian chocolate that is not in
the shipment at all, and the declared total belonged to the same wrong page, so
the item-sum gate saw a licence's goods list summing to a licence's total and
found nothing to complain about.

The bundle holds both forms. Pages 3-4 are the CUSDEC — handwritten, no text
layer. Pages 6-8 are the Import Licence — machine-printed. The classifier read
both correctly and said so in its `reason` string, which is truncated to 80
characters and used only for display. The only structured answer it gave was
TYPED / HANDWRITTEN / ATTACHMENT, which describes how a page is FILLED IN. So
"typed" stood in for "authoritative", the typed lane had read a licence, and
`v7_typed_priority` handed it the header and the item list.

An Import Licence lists every good the importer is PERMITTED to bring in —
routinely more goods than the shipment contains, at licence quantities rather
than shipped ones. Here it carried 11 lines against the declaration's 4, and
3,303 KG of one item where the CUSDEC declares 3,168. Both numbers are correct
on their own form. That is why this cannot be fixed by picking the "better"
read: the two documents genuinely disagree, and only one of them is the
declaration.

So `document` is now asked for and stored separately. This file pins the
deterministic half — the half that needs no model and no network.
"""
import pytest

from v11.agents.page_classifier import (
    DOCUMENTS,
    _document_from_text,
    _document_rescue,
)

# Lifted from the real pages of 662ec932_0259100560__MA0259100560.pdf.
LICENCE_TEXT = """
APPENDIX 4b
Republic of the Union of Myanmar
Ministry of Commerce
Department of Trade
IMPORT LICENCE
7. Licence No. YGNBIL12425001953 (12/03/2025)
12. Total CIF Value (Kyats) 95,707,461.0886
13.No 14.Hscode 15.Description of Goods 16.UnitCode 17.UnitPrice 18.Quantity
1 1806209000 DARK FINEST BELGIAN CHOCOLATE (811-E4-U71) KG 129.5210 2400.0000
"""

DECLARATION_TEXT = """
CUSTOMS DEPARTMENT   IMPORT DECLARATION   CUSDEC 1
11. Registration No 0259100560
35. Marks & Nos Container Nos  37. No & Type Of Packages  38. Description Of Goods
1. 368 CTN BONITO DASHI (FISH FLAVORED SEASONING) (150 G/CARTON BOX)
59. Import Duty (Ks.)  60. Commercial Tax (Ks)  61. Others Taxes / Fees (Ks)
Taxes and fees
"""

CONTINUATION_TEXT = """
Continuation Sheet   CUSTOMS DEPARTMENT   IMPORT DECLARATION   CUSDEC-1
11. Registration No 0259100560
2. 473 CTN BONITO DASHI (FISH FLAVORED SEASONING) (1 KG/CARTON BOX)
3. 352 CTN KOMBU DASHI (150 G/CARTON BOX)
"""


class TestTheTextLayerSettlesIt:

    def test_the_licence_is_identified_as_a_licence(self):
        assert _document_from_text(LICENCE_TEXT) == "LICENCE"

    def test_the_declaration_is_identified_as_a_declaration(self):
        assert _document_from_text(DECLARATION_TEXT) == "DECLARATION"

    def test_a_continuation_sheet_is_part_of_the_declaration(self):
        """It carries the item rows. Calling it an attachment loses them."""
        assert _document_from_text(CONTINUATION_TEXT) == "DECLARATION"

    def test_the_licences_own_goods_table_does_not_make_it_a_declaration(self):
        """The trap that made this bug possible.

        A licence prints an HS column, a goods table, quantities, unit prices and
        a CIF total — every generic customs word. `_DECL_MARKERS`, used by the
        older `_marker_rescue`, contains "cif value", so a marker set built from
        customs vocabulary identifies the wrong document with full confidence.
        Only title-block phrases separate the two forms.
        """
        assert "cif value" in LICENCE_TEXT.lower()
        assert _document_from_text(LICENCE_TEXT) != "DECLARATION"


class TestItRefusesToGuess:

    @pytest.mark.parametrize("text", ["", "   ", None, "IMPORT LICENCE"])
    def test_too_little_text_is_unknown(self, text):
        """A photographed page yields nothing; one phrase is not an identity."""
        assert _document_from_text(text) == "UNKNOWN"

    def test_one_marker_each_way_is_unknown_not_a_coin_flip(self):
        mixed = ("IMPORT LICENCE issued against the attached customs declaration "
                 "for the goods described below. " + "x" * 300)
        assert _document_from_text(mixed) == "UNKNOWN"

    def test_a_declaration_naming_its_licence_stays_a_declaration(self):
        """Real CUSDECs print '15. Import Licence / Permit No.' — a reference to
        the licence, not evidence of being one."""
        text = DECLARATION_TEXT + "\n15. Import Licence / Permit No. YGNBIL12425001953"
        assert _document_from_text(text) == "DECLARATION"


class TestTheRescueLeavesAScanAlone:

    def test_a_pdf_with_no_text_layer_keeps_the_models_answer(self, tmp_path):
        """The case this feature exists for.

        All 12 pages of the failing bundle have ZERO extractable characters, so
        nothing deterministic can run and the model's answer is the only answer
        there is. If the rescue overwrote it with UNKNOWN the feature would be
        useless on precisely the documents that need it.
        """
        fitz = pytest.importorskip("fitz")
        pdf = tmp_path / "scan.pdf"
        doc = fitz.open()
        doc.new_page()
        doc.save(str(pdf))
        doc.close()

        pages = [{"page": 1, "label": "HANDWRITTEN", "document": "DECLARATION",
                  "document_source": "model"}]
        _document_rescue(pages, str(pdf))
        assert pages[0]["document"] == "DECLARATION"
        assert pages[0]["document_source"] == "model"

    def test_a_missing_file_does_not_raise(self):
        pages = [{"page": 1, "label": "TYPED", "document": "DECLARATION"}]
        _document_rescue(pages, "/nonexistent/nope.pdf")
        assert pages[0]["document"] == "DECLARATION"


def test_unknown_is_in_the_vocabulary_and_is_not_other():
    """UNKNOWN means 'not identified' and is the only value allowed to fall back
    to legacy behaviour. OTHER means 'identified, and not one of these'."""
    assert "UNKNOWN" in DOCUMENTS
    assert "OTHER" in DOCUMENTS
    assert "DECLARATION" in DOCUMENTS
