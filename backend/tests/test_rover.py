"""Unit tests for the Column Fleet side project (backend/fleet/).

Covers the pure/deterministic layers — no LLM, no PDF needed:
  * deterministic.py  — declaration_no (First-approval rule), date, printed rate
  * supervisor.py     — OCR normalize, rate derivation (advisory), invariants
  * mapping.py        — fleet record -> accountant 'AI results' schema

The vision tiers (single_agent, vision_agents, pipeline) hit OpenRouter and are
exercised by the live batch runners, not here.
"""
from rover.schema import Cell
from rover import deterministic as det
from rover import supervisor as sup
from rover.mapping import to_accountant_row


# --------------------------------------------------------------------------- #
# deterministic.py
# --------------------------------------------------------------------------- #
def test_declaration_no_uses_top_of_form_not_first_approval():
    """The top-of-form 'Declaration No.' identifies THIS document.

    Both numbers here are real declarations from one Ex-bond release: the form's own
    100319576711 and the earlier entry it clears, 100313488550. They appear as
    separate rows in the team's ledger. Until 2026-08-01 the first-approval number
    won, which labelled the document with a different declaration's number — the
    team's decision is that only the top-of-form number is used.
    """
    lines = ["Declaration No.", "100319576711", "Section 00", "Importer PREMIUM",
             "B/L 13BDONEY", "100313488550", "First approval declaration No.",
             "Date 2025/10/09"]
    c = det.declaration_no(lines)
    assert c.value == "100319576711"       # the form's own number, NOT first-approval
    assert c.confidence >= 0.90
    # and the reader should say the other number was seen and deliberately not used
    assert "100313488550" in (c.note or "")


def test_declaration_no_when_no_first_approval_present():
    lines = ["Declaration No.", "100319576711", "Importer ..."]
    c = det.declaration_no(lines)
    assert c.value == "100319576711"
    assert not (c.note or "")              # nothing to warn about


def test_declaration_date_iso():
    lines = ["Declaration date", "2026/01/07", "10:19"]
    c = det.declaration_date(lines)
    assert c.value == "2026-01-07"


def test_printed_rate_in_band_extracted():
    lines = ["Exchange Rate (1) USD -", "2,100", "MACCS SERVICE FEE"]
    c = det.exchange_rate(lines)
    assert c.value == 2100.0
    assert c.status == "ok"


def test_printed_rate_rejects_out_of_band_token():
    # A THB doc: the only nearby number 5,953,500 is a tax, far outside 40-90.
    lines = ["Exchange Rate (1) THB", "5,953,500"]
    c = det.exchange_rate(lines)
    assert c.status == "empty"           # nothing band-valid -> no false rate


def test_printed_rate_thb():
    lines = ["Exchange Rate (1) THB -", "64.398"]
    c = det.exchange_rate(lines)
    assert c.value == 64.398


# --------------------------------------------------------------------------- #
# supervisor.py — OCR normalize
# --------------------------------------------------------------------------- #
def test_ocr_normalize_declaration_no():
    rec = {"declaration_no": Cell(column="declaration_no", value="1003063S1722",
                                  status="ok", confidence=0.6, model="grok")}
    sup.normalize_declaration_no(rec)
    assert rec["declaration_no"].value == "100306351722"   # S -> 5


def test_declaration_no_disagreement_flags():
    # multi-doc bundle: text reader grabbed a wrong id off another sub-document,
    # vision read the real one -> they disagree -> must flag, never ship silently.
    det = {"declaration_no": Cell(column="declaration_no", value="120001647100",
                                  status="ok", confidence=0.9, model="deterministic")}
    vis = {"declaration_no": Cell(column="declaration_no", value="100306922661",
                                  status="ok", confidence=0.9, model="grok"),
           "currency": Cell(column="currency", value="THB", status="ok", confidence=0.9),
           "exchange_rate": Cell(column="exchange_rate", value=64.398, status="ok",
                                 confidence=0.9, model="grok"),
           "import_export_customs_duty": Cell(column="import_export_customs_duty",
                                              value=6_868_660, status="ok", confidence=0.9)}
    rec, suspect, notes, needs_review = sup.compile(det, vis)
    assert "declaration_no" in suspect
    assert needs_review is True
    both = {sup._digits(rec["declaration_no"].value)} | {sup._digits(a) for a in rec["declaration_no"].alternates}
    assert {"120001647100", "100306922661"} <= both      # both candidates preserved


def test_declaration_no_uncorroborated_fallback_flags():
    # The real complex-bundle case: text reader used a plain 'Declaration No.'
    # fallback (conf 0.90) and vision returned nothing -> single unverified read -> flag.
    det = {"declaration_no": Cell(column="declaration_no", value="120001647100",
                                  status="ok", confidence=0.90, model="deterministic")}
    vis = {"currency": Cell(column="currency", value="THB", status="ok", confidence=0.9),
           "exchange_rate": Cell(column="exchange_rate", value=64.398, status="ok",
                                 confidence=0.9, model="grok"),
           "import_export_customs_duty": Cell(column="import_export_customs_duty",
                                              value=6_868_660, status="ok", confidence=0.9)}
    rec, suspect, notes, needs_review = sup.compile(det, vis)
    assert "declaration_no" in suspect
    assert needs_review is True


def test_declaration_no_agreement_not_flagged():
    det = {"declaration_no": Cell(column="declaration_no", value="100313488550",
                                  status="ok", confidence=0.98, model="deterministic")}
    vis = {"declaration_no": Cell(column="declaration_no", value="100313488550",
                                  status="ok", confidence=0.9, model="grok"),
           "currency": Cell(column="currency", value="USD", status="ok", confidence=0.9),
           "exchange_rate": Cell(column="exchange_rate", value=2100.0, status="ok",
                                 confidence=0.9, model="grok"),
           "import_export_customs_duty": Cell(column="import_export_customs_duty",
                                              value=5_953_500, status="ok", confidence=0.9)}
    rec, suspect, notes, needs_review = sup.compile(det, vis)
    assert "declaration_no" not in suspect


def test_ocr_normalize_flags_garbage():
    rec = {"declaration_no": Cell(column="declaration_no", value="12XYZ",
                                  status="ok", confidence=0.6)}
    sup.normalize_declaration_no(rec)
    assert rec["declaration_no"].status == "suspect"        # not 11-12 digits


# --------------------------------------------------------------------------- #
# supervisor.py — rate derivation is ADVISORY (never auto-clears review)
# --------------------------------------------------------------------------- #
def _value_basis(mmk, usd, cur="USD", duty=5_000_000, rate=None):
    vis = {
        "total_customs_value": Cell(column="total_customs_value", value=mmk,
                                    status="ok", confidence=0.9),
        "customs_value_usd": Cell(column="customs_value_usd", value=usd,
                                  status="ok", confidence=0.9),
        "currency": Cell(column="currency", value=cur, status="ok", confidence=0.9),
        "import_export_customs_duty": Cell(column="import_export_customs_duty",
                                           value=duty, status="ok", confidence=0.9),
    }
    if rate is not None:
        vis["exchange_rate"] = Cell(column="exchange_rate", value=rate,
                                    status="ok", confidence=0.9, model="grok")
    return vis


def test_derived_rate_is_exact_but_flagged():
    rec, suspect, notes, needs_review = sup.compile({}, _value_basis(198_450_000, 94_500))
    assert rec["exchange_rate"].value == 2100.0        # exact math
    assert rec["exchange_rate"].model == "derived"
    assert "exchange_rate" in suspect                  # advisory -> flagged
    assert needs_review is True                        # never auto-clears


def test_printed_rate_not_overwritten_by_derivation():
    # A directly-read in-band rate must survive; derivation must not touch it.
    rec, suspect, notes, needs_review = sup.compile(
        {}, _value_basis(198_450_000, 94_500, rate=2100.0))
    assert rec["exchange_rate"].model == "grok"        # kept the direct read
    assert "exchange_rate" not in suspect
    assert needs_review is False


# --------------------------------------------------------------------------- #
# supervisor.py — invariants
# --------------------------------------------------------------------------- #
def test_out_of_band_rate_flagged():
    vis = _value_basis(210_000_000, 100_000, cur="USD", rate=500.0)
    del vis["customs_value_usd"]                        # avoid derivation path
    rec, suspect, notes, needs_review = sup.compile({}, vis)
    assert "exchange_rate" in suspect                  # 500 outside USD band


def test_ct_equals_exemption_flagged():
    vis = {
        "currency": Cell(column="currency", value="THB", status="ok", confidence=0.9),
        "exchange_rate": Cell(column="exchange_rate", value=64.4, status="ok",
                              confidence=0.9, model="grok"),
        "commercial_tax_ct": Cell(column="commercial_tax_ct", value=3_622_975,
                                  status="ok", confidence=0.9),
        "exemption_reduction": Cell(column="exemption_reduction", value=3_622_975,
                                    status="ok", confidence=0.9),
    }
    rec, suspect, notes, needs_review = sup.compile({}, vis)
    assert "commercial_tax_ct" in suspect              # gemini mislabel guard


def test_value_block_empty_detection():
    empty = {"currency": Cell(column="currency", value="THB", status="ok")}
    assert sup.value_block_empty(empty) is True
    filled = {"total_customs_value": Cell(column="total_customs_value",
                                          value=1_000, status="ok")}
    assert sup.value_block_empty(filled) is False


# --------------------------------------------------------------------------- #
# mapping.py — fleet record -> accountant schema
# --------------------------------------------------------------------------- #
def _result(**vals):
    base = {c: None for c in [
        "declaration_no", "declaration_date", "importer_name", "invoice_number",
        "currency", "exchange_rate", "invoice_price_mmk", "total_customs_value",
        "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
        "security_fee_sf", "maccs_service_fee_mf", "exemption_reduction"]}
    base.update(vals)
    return {"values": base, "needs_review": False, "suspect": []}


def test_mapping_invoice_split_and_total_import():
    row = to_accountant_row(_result(
        invoice_number="A-960773210", import_export_customs_duty=5_953_500,
        commercial_tax_ct=10_220_175, advance_income_tax_at=3_969_000,
        security_fee_sf=0, maccs_service_fee_mf=30_000,
        invoice_price_mmk=104_763_078, total_customs_value=104_763_078))
    assert row["INVOICE NUMBER"] == "960773210"
    assert row["INVOICE NUMBER (CUSTOM)"] == "A - 960773210"
    assert row["TOTAL IMPORT"] == 5_953_500 + 10_220_175 + 3_969_000 + 30_000
    assert row["SECURITY"] == 0 and row["MACCS"] == 30_000   # kept separate
    assert row["REVIEW"] == "clean"


def test_mapping_flags_value_disagreement():
    row = to_accountant_row(_result(
        invoice_price_mmk=104_763_078, total_customs_value=198_450_000,
        import_export_customs_duty=5_953_500))
    assert "confirm which the ledger books" in row["NOTE"]


def test_mapping_review_flag_and_note():
    res = _result(invoice_number="INV-123", exchange_rate=64.4)
    res["needs_review"] = True
    res["suspect"] = ["exchange_rate"]
    row = to_accountant_row(res)
    assert row["REVIEW"] == "REVIEW"
    assert "suspect" in row["NOTE"]
    assert row["INVOICE NUMBER"] == "123"          # INV- prefix stripped
