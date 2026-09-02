"""V11 — Master Router workflow.
Per-page classifier → split PDF → V7 (typed) + V10 (HW) parallel → merge."""
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numeric

from v11.agents.page_classifier import classify_pages
from v11.tools.pdf_split import split_pdf_by_labels
from v11.agents.merger import merge_results
from v11.tools import reconcile as _reconcile
try:
    from v11.tools.field_bbox import compute_field_bboxes as _compute_field_bboxes
except Exception:
    # Signature must match the real one, `pages` included — the caller passes it
    # by keyword, and a stub that omits it turns a missing optional dependency
    # into a TypeError inside the extraction path.
    def _compute_field_bboxes(_pdf, _decl, _items, pages=None):
        return {}

try:
    from v11.triage import declaration_pages as _declaration_pages
except Exception:
    def _declaration_pages(_pdf, _cusdec_page, _decl_no=None):
        return []

try:
    import event_logger
except Exception:
    event_logger = None

# Event bus (built in parallel as backend/v11/event_bus.py).
# Wrap import in try/except so workflow still runs if bus missing.
try:
    from v11.event_bus import emit as _bus_emit, close as _bus_close
except Exception:
    def _bus_emit(*_args, **_kwargs):
        return None

    def _bus_close(*_args, **_kwargs):
        return None


def _emit(job_id: Optional[str], action: str, data: Dict) -> None:
    """Emit to event bus; never raise."""
    try:
        _bus_emit(job_id, action, data)
    except Exception:
        pass


def _close(job_id: Optional[str]) -> None:
    try:
        _bus_close(job_id)
    except Exception:
        pass


def _conf_to_float(c) -> float:
    """Map classifier confidence ('high|med|low') or numeric → float 0.0–1.0."""
    if isinstance(c, (int, float)):
        try:
            f = float(c)
            return max(0.0, min(1.0, f))
        except Exception:
            return 0.5
    s = str(c or "").lower().strip()
    return {"high": 0.95, "med": 0.7, "medium": 0.7, "low": 0.4}.get(s, 0.5)


def _label_to_verdict(label: str) -> str:
    # Frontend display verdicts (V11 router observability rename):
    #   typed        → PRINTED
    #   handwritten  → INKED
    #   attachment   → EXTRA
    # Backend internal labels (TYPED/HANDWRITTEN/ATTACHMENT) are unchanged.
    return {
        "TYPED": "PRINTED",
        "HANDWRITTEN": "INKED",
        "ATTACHMENT": "EXTRA",
    }.get((label or "").upper(), "PRINTED")


# Pipeline → fancy display label (used for `label` field on STAGE_* events).
PIPELINE_LABEL = {
    "V7": "Veritas",
    "V10_PRO": "Scrivener",
    "V11": "Maestro",
}


def _log_event(action: str, data: Dict, job_id=None) -> None:
    """Mirror to event_logger if available."""
    if not event_logger:
        return
    try:
        event_logger.log_event(
            action=action,
            user=None,
            job_id=job_id,
            status="OK",
            details=json.dumps(data, default=str)[:2000],
        )
    except Exception:
        pass


def _call_v7(pdf_path: str) -> Dict:
    from pipeline.pipeline import run_pipeline
    return run_pipeline(pdf_path)


def _call_typed(typed_pdf: str, use_presto: bool = False,
                full_pdf: str = None, presto_pages=None) -> Dict:
    """Typed-page extraction with the V12 Presto fast-path + math-gated fallback.

    When `use_presto` (flag on AND every typed page is digital), try Presto first:
    read the text layer of the typed + attachment pages (`presto_pages` of the
    ORIGINAL `full_pdf`, so misrouted item pages the classifier dropped into
    ATTACHMENT are still seen) → one schema call. Keep it ONLY if the reconcile
    equation balances (Σ item customs values == declared total); otherwise fall
    back to V7 Veritas on the typed slice so accuracy never regresses.
    `use_presto=False` → behaves exactly like V7.
    """
    if use_presto:
        try:
            from v11.presto import run as presto_run
            from v11.tools import reconcile as _rc
            from v11.tools import self_correct as _sc
            res = presto_run(full_pdf or typed_pdf, pages=presto_pages)
            v = _rc.reconcile(res.get("declaration") or {}, res.get("items") or [])
            if v.get("checked") and v.get("balanced"):
                res["_engine"] = "presto"
                res["_field_engine"] = {k: "presto" for k in (res.get("declaration") or {})}
                return res
            # Gate failed → try targeted self-correction (fix only the broken
            # header field) BEFORE any slow fallback.
            cor = _sc.correct(full_pdf or typed_pdf, res.get("declaration") or {},
                              res.get("items") or [], v, header_page=1)
            if cor.get("corrected") and cor["verdict"].get("balanced"):
                res["declaration"] = cor["declaration"]
                res["_engine"] = "presto+selfcorrect"
                res.setdefault("trace", []).append({"phase": "self_correct", "log": cor["log"]})
                return res
            print(f"[Presto] gap {v.get('gap_pct')}% — falling back to V7 Veritas")
            _presto_fallback = res          # keep it; V7 may be worse, field by field
        except Exception as e:
            print(f"[Presto] fast-path error, falling back to V7: {e}")
    res = _call_v7(typed_pdf)
    if isinstance(res, dict):
        res.setdefault("_engine", "v7")
    # A failed gate is a reason to REVIEW the document, not to throw away a header
    # that read correctly.
    #
    # PRESTO WINS ON HEADER FIELDS IT READ. Presto parses the PDF's own text layer —
    # exact characters, no OCR step, so it cannot mis-see a digit. V7 re-reads
    # rendered images and guesses. Backfilling only V7's BLANKS was not enough,
    # because V7's failure mode is confidently writing the WRONG value, and a wrong
    # value beat a right one. Measured on one Ex-bond release, all from the same run:
    #
    #     field                    Presto (text layer)   V7 (image re-read)
    #     declaration_no           100319576711 correct  100313488550 wrong number
    #     total_customs_value      198,450,000 correct   204,403,500 (tax base)
    #     adjustment_value         null correct          2.0 (the CODE, not money)
    #     invoice_price_fc         481,406.664 correct   not produced at all
    #
    # Items are NOT taken from Presto here — the gate failed on the item/CIF maths,
    # which is exactly what V7's slower per-page read exists to improve.
    _p = locals().get("_presto_fallback")
    if isinstance(res, dict) and isinstance(_p, dict):
        p_decl = _p.get("declaration") or {}
        r_decl = res.get("declaration") or {}
        taken = [k for k, val in p_decl.items()
                 if val not in (None, "", "None") and r_decl.get(k) != val]
        for k in taken:
            r_decl[k] = p_decl[k]
        # Provenance, recorded rather than inferred. Four rebuild-and-rerun cycles
        # were spent guessing which layer replaced Presto's header; a per-field tag
        # answers it in one run and keeps answering it for every document after.
        fe = {k: ("presto" if k in taken else "v7") for k in r_decl}
        res["_field_engine"] = fe
        if taken:
            res["declaration"] = r_decl
            res["_engine"] = f"{res.get('_engine', 'v7')}+presto_header"
            res.setdefault("trace", []).append(
                {"phase": "presto_header_preferred", "fields": taken})
            print(f"[Presto] header preferred over V7 for {len(taken)} field(s): "
                  f"{', '.join(sorted(taken)[:8])}")
    return res


def _call_scribe(pdf_path: str) -> Dict:
    """V14 Atlas handwriting engine — V13 Scribe (vision vote + math gates)."""
    from v13.scribe import run as run_scribe
    res = run_scribe(pdf_path)
    if isinstance(res, dict):
        res.setdefault("_engine", "scribe")
    return res


def _call_v10(pdf_path: str) -> Dict:
    """Use V10 PRO (shape-validated, memory-aware, cost-tracked)."""
    from v10_pro.workflow import run as run_v10_pro
    return run_v10_pro(pdf_path)


def invoice_price_fields(vals: Dict, coerce=lambda x: x) -> Dict:
    """The three invoice-amount columns, from an engine's raw values.

    `invoice_price` means the INVOICE-CURRENCY amount and always has — the
    team's ledger column, both Excel writers and the Beta v3 requirement form
    ("Values are read in the invoice currency (not MMK)") all read it that way.

    The ROVER bridge used to map it from `invoice_price_mmk`, which silently
    redefined an existing column. Nothing crashed — a float column got a valid
    float — but scored against the manual ledger the field fell from 12/13
    correct to 1/13, and the CIF gate could not see it because that gate reads
    `invoice_price_fc` first. The kyat figure keeps its own column instead of
    displacing this one.

    Lives here, out of the inline dict, so a test can pin the unit against the
    real mapping rather than a copy of it.
    """
    fc = vals.get("invoice_price_fc")
    mmk = vals.get("invoice_price_mmk")
    return {
        # Explicit None test, not `or`: a printed 0 is a reading.
        "invoice_price": coerce(fc if fc is not None else mmk),
        "invoice_price_fc": coerce(fc),
        "invoice_price_mmk": coerce(mmk),
    }


def _pick(decl: Dict, *keys):
    """First key that is actually set, keeping an explicit 0.

    The whitelist below used `decl.get(a) or decl.get(b)` on every row, which
    reads a real zero as "absent" and falls through to the alias. Two ways that
    bites on a live form:

      * Commercial Tax is genuinely 0 on plenty of declarations. The `or` sends
        it to the alias, the alias is None, and the column stores NULL — which
        the tax-completeness gate then reports as a dropped tax block.
      * On the release order "Adjustment" is the small CODE integer (2) sitting
        next to "Adjustment value" (326,139.8592). A null adjustment therefore
        fell through and stored the code as an amount — a wrong figure in a
        money column, and one that also tightens the CIF tolerance as though a
        build-up had been supplied.

    Missing and zero are different claims; only the first may fall through.

    The fallback is decided on key PRESENCE, not on the value. An alias exists
    because two engines name the same field differently — so it answers "this
    engine didn't produce this field", never "this engine produced it and said
    blank". If the primary key is in the dict at all, its reading stands, null
    included; that is what stops a blank adjustment from picking up the code.
    """
    for i, k in enumerate(keys):
        if k not in decl:
            continue
        v = decl.get(k)
        if v is not None and v != "":
            return v
        # Present but blank. For the primary name that is an authoritative
        # reading — the engine looked and the row was empty — so stop here
        # rather than letting an alias speak for it.
        if i == 0:
            return None
    return None


def _save_to_db(out: Dict, pdf_path: str) -> str:
    """Save merged V11 result to DB. Returns job_id."""
    import database
    from pathlib import Path
    p = Path(pdf_path)
    pdf_size = p.stat().st_size if p.exists() else 0
    cls = out.get("page_classification", {}) or {}
    n_pages = cls.get("n_pages") or 0
    summary = cls.get("summary", {}) or {}
    typed_pages = summary.get("TYPED", 0)
    hw_pages = summary.get("HANDWRITTEN", 0)

    job_id = database.create_job(
        pdf_name=p.name,
        pdf_path=str(p),
        pdf_size=pdf_size,
        total_pages=n_pages,
        text_pages=typed_pages,
        image_pages=hw_pages,
    )

    decl = out.get("declaration", {}) or {}
    if decl:
        # Map snake_case → DB column names matching V7 schema
        _fe = (decl or {}).get("_field_engine") or {}
        if _fe:
            _watch = ("declaration_no", "declaration_date", "total_customs_value",
                      "adjustment_value", "invoice_price_fc", "exchange_rate",
                      "arrival_date", "release_order_date", "completion_date")
            print("[provenance] " + "  ".join(
                f"{k}={_fe.get(k, '?')}" for k in _watch if k in _fe))
        # `invoice_price` and `invoice_price_fc` are the SAME quantity — the amount in
        # the invoice currency. The second name was added so the kyat figure could
        # have its own column; the first is what the typed lane still emits.
        #
        # It cannot be expressed as a `_pick` alias. `_pick` stops at the primary key
        # when that key is present but blank, treating it as "the engine looked and
        # the row was empty" — the rule that stops a blank `adjustment_value` picking
        # up the adjustment CODE printed beside it. The engines emit
        # `invoice_price_fc: None` explicitly, so the fallback never got a turn and
        # two documents stored null beside a perfectly good `invoice_price`.
        #
        # A rename is not an alias, so it is resolved here instead of weakening a rule
        # that is doing useful work elsewhere.
        _ip_fc = _pick(decl, "invoice_price_fc", "Invoice Price (FC)")
        if _ip_fc is None:
            _ip_fc = _pick(decl, "invoice_price", "Invoice Price")

        db_decl = {
            "declaration_no": decl.get("declaration_no") or decl.get("Declaration No"),
            "declaration_date": decl.get("declaration_date") or decl.get("Declaration Date"),
            "arrival_date": decl.get("arrival_date") or decl.get("Arrival Date"),
            "release_order_date": decl.get("release_order_date") or decl.get("Release Order Date"),
            "completion_date": decl.get("completion_date") or decl.get("Completion Date"),
            "importer_name": decl.get("importer_name") or decl.get("Importer (Name)"),
            "consignor_name": decl.get("consignor_name") or decl.get("Consignor (Name)"),
            "invoice_number": decl.get("invoice_number") or decl.get("Invoice Number"),
            "invoice_number_customs_declaration": decl.get("invoice_number_customs"),
            "invoice_number_commercial_invoice": decl.get("invoice_number_commercial"),
            "currency": decl.get("currency") or decl.get("Currency"),
            "currency_2": decl.get("currency_2") or decl.get("Currency 2"),
            # Money rows go through _pick, not `or`: a declared 0 is a reading,
            # not a blank, and must not fall through to the alias.
            "exchange_rate": _pick(decl, "exchange_rate", "Exchange Rate"),
            "invoice_price": _pick(decl, "invoice_price", "Invoice Price"),
            "invoice_price_fc": _ip_fc,
            "invoice_price_mmk": _pick(decl, "invoice_price_mmk", "Invoice Price (MMK)"),
            "total_customs_value": _pick(decl, "total_customs_value", "Total Customs Value"),
            # CIF build-up — was silently dropped at this whitelist, leaving the DB
            # columns permanently NULL even when an engine supplied them.
            "freight_value": _pick(decl, "freight_value", "Freight"),
            "insurance_value": _pick(decl, "insurance_value", "Insurance"),
            # "Adjustment" is V7's name for the build-up AMOUNT. On the release
            # order the same word labels the small code integer beside
            # "Adjustment value", so it is only consulted when the real field is
            # absent — never when it is present-and-null.
            "adjustment_value": _pick(decl, "adjustment_value", "Adjustment"),
            # Currency each build-up line is printed in. Not decoration: the CIF
            # gate has to convert them, and they are NOT always the invoice currency
            # (Insurance is frequently already MMK on a form whose Adjustment is not).
            "freight_currency": _pick(decl, "freight_currency"),
            "insurance_currency": _pick(decl, "insurance_currency"),
            "adjustment_currency": _pick(decl, "adjustment_currency"),
            # DB name FIRST, engine name second — and the order is the whole point.
            #
            # These six rows listed only the RAW engine spellings. Every deterministic
            # rescue writes the DB spellings: `cusdec_rescue` sets
            # `security_fee_sf`, `vision_rescue` sets `exemption_reduction`, and both
            # are documented as authoritative because they read the legal source page.
            # None of it ever reached the database — the whitelist looked for keys
            # those stages do not produce, found the engine's raw key instead, and
            # stored that. A coordinate read of the tax table was computed, verified
            # against the corpus at 48 correct / 0 wrong, and then dropped right here.
            #
            # `_pick` takes the first key PRESENT, so putting the DB name first means
            # a rescue value wins and the engine's reading remains the fallback.
            "import_export_customs_duty": _pick(decl, "import_export_customs_duty",
                                                "customs_duty", "Import/Export Customs Duty"),
            "commercial_tax_ct": _pick(decl, "commercial_tax_ct",
                                       "commercial_tax", "Commercial Tax (CT)"),
            "advance_income_tax_at": _pick(decl, "advance_income_tax_at",
                                           "advance_income_tax", "Advance Income Tax (AT)"),
            "security_fee_sf": _pick(decl, "security_fee_sf",
                                     "security_fee", "Security Fee (SF)"),
            "maccs_service_fee_mf": _pick(decl, "maccs_service_fee_mf",
                                          "maccs_service_fee", "MACCS Service Fee (MF)"),
            "exemption_reduction": _pick(decl, "exemption_reduction",
                                         "exemption", "Exemption/Reduction"),
            "document_format": out.get("document_format"),
            # Reconciliation verdict (items_sum vs declared total) — the gate.
            # save_declarations reads the underscore-prefixed metadata key.
            "_cross_val_passed": 1 if out.get("cross_val_passed") else 0,
        }
        # Persist sanity_flags (item_count_mismatch, currency_rate, etc) for review UI
        _sflags = out.get("sanity_flags") or []
        if _sflags:
            import json as _json
            db_decl["_sanity_flags_json"] = _json.dumps(_sflags)
            db_decl["_document_format"] = out.get("document_format")
        # Per-field evidence (ROVER Cell record). Computed on every rover run and,
        # until now, dropped right here at the whitelist — `out["rover_record"]` was
        # set by _run_rover and never mapped, so the DB never saw it.
        _ev = out.get("rover_record") or out.get("evidence")
        if not _ev and _fe:
            # ATLAS records per-field provenance too, and it used to end at the
            # `print` above. `evidence_json` was populated only from a ROVER
            # record, so once ROVER was retired the review screen had no way to
            # distinguish a value read off the declaration's text layer from one
            # a vision model read off a photograph, or from one the CIF identity
            # worked out. Those need very different amounts of trust.
            try:
                from v11.tools.provenance import build_evidence
                # Boxes are already computed — Phase 4.5 runs before this save —
                # so a cell can carry the page it was read from and the Checks
                # screen can crop that patch of paper instead of the whole page.
                _ev = build_evidence(db_decl, _fe, out.get("sanity_flags") or [],
                                     out.get("field_bboxes") or {})
            except Exception as e:
                print(f"[V11 DB] provenance skipped: {e}")
        if _ev:
            import json as _json2
            try:
                db_decl["_evidence_json"] = _json2.dumps(_ev, default=str)
            except Exception as e:
                print(f"[V11 DB] evidence serialise skipped: {e}")
        try:
            database.save_declarations(job_id, [db_decl])
        except Exception as e:
            print(f"[V11 DB] save_declarations error: {e}")

    items = out.get("items") or []
    if items:
        db_items = []
        for it in items:
            db_items.append({
                "item_name": it.get("item_name") or it.get("Item name"),
                "customs_duty_rate": it.get("customs_duty_rate") or it.get("Customs duty rate"),
                "quantity": it.get("quantity") or it.get("Quantity (1)"),
                "invoice_unit_price": it.get("invoice_unit_price") or it.get("Invoice unit price"),
                "cif_unit_price": it.get("cif_unit_price") or it.get("CIF unit price"),
                "currency": it.get("currency") or it.get("Currency"),
                "commercial_tax_percent": it.get("commercial_tax_pct") or it.get("Commercial tax %"),
                "exchange_rate": it.get("exchange_rate") or it.get("Exchange Rate (1)"),
                "hs_code": it.get("hs_code") or it.get("HS Code"),
                "origin_country": it.get("origin") or it.get("Origin Country"),
                "customs_value_mmk": it.get("customs_value_mmk") or it.get("Customs Value (MMK)"),
            })
        try:
            database.save_items(job_id, db_items)
        except Exception as e:
            print(f"[V11 DB] save_items error: {e}")

    # Compute accuracy from extraction signals:
    # - decl populated rate: how many of expected fields filled
    # - items populated rate: how many cells filled per item
    # - cross_val flag (V7 inside V11)
    try:
        _decl = out.get("declaration") or {}
        _items = out.get("items") or []
        _expected_decl = ['declaration_no','declaration_date','importer_name','consignor_name',
                          'invoice_number','currency','exchange_rate','invoice_price',
                          'total_customs_value','customs_duty','commercial_tax',
                          'advance_income_tax','maccs_service_fee']
        _decl_filled = sum(1 for f in _expected_decl if _decl.get(f) not in (None, '', 0))
        _decl_score = _decl_filled / len(_expected_decl)
        _expected_item = ['item_name','hs_code','quantity','invoice_unit_price','customs_value_mmk','origin']
        if _items:
            _item_filled = 0
            _item_total = 0
            for _it in _items:
                for _f in _expected_item:
                    _item_total += 1
                    if _it.get(_f) not in (None, '', 0):
                        _item_filled += 1
            _item_score = _item_filled / max(1, _item_total)
        else:
            _item_score = 0
        # Weighted: decl 60%, items 40%
        _accuracy_pct = round((_decl_score * 0.6 + _item_score * 0.4) * 100, 1)
    except Exception:
        _accuracy_pct = 0

    try:
        database.update_job_metrics(
            job_id,
            processing_time=out.get("duration_seconds", 0) or 0,
            cost=out.get("cost", 0) or 0,
            accuracy=_accuracy_pct,
        )
        database.update_job_status(job_id, "COMPLETED")
    except Exception as e:
        print(f"[V11 DB] update_job error: {e}")

    # Mark for human review (V11 → side-by-side approve UI)
    try:
        database.update_review_status(job_id, "pending_review")
    except Exception as e:
        print(f"[V11 DB] update_review_status error: {e}")

    # Persist field bboxes (PDF↔form highlight)
    try:
        bb = out.get("field_bboxes") or {}
        if bb:
            database.update_job_field_bboxes(job_id, bb)
    except Exception as e:
        print(f"[V11 DB] update_job_field_bboxes error: {e}")

    # Persist the engine's verbatim output (jobs.raw_extraction), so a mapping
    # bug can be re-projected from stored JSON instead of re-extracting every
    # document. Deliberately outside the db_decl whitelist — this is the job's
    # own artifact, not a declaration field. Never fatal: a database without the
    # column, or a payload that would not serialise, leaves it NULL.
    try:
        _raw = out.get("raw_extraction")
        if _raw:
            database.update_job_raw_extraction(job_id, _raw)
    except Exception as e:
        print(f"[V11 DB] update_job_raw_extraction error: {e}")

    # Compute document_type from page_classification summary
    cls = out.get("page_classification", {}) or {}
    summary = cls.get("summary", {}) or {}
    typed = summary.get("TYPED", 0)
    hw = summary.get("HANDWRITTEN", 0)
    if typed > 0 and hw > 0:
        doc_type = "MIXED"
    elif hw > 0:
        doc_type = "HANDWRITTEN"
    elif typed > 0:
        doc_type = "TYPED"
    else:
        doc_type = "UNKNOWN"

    try:
        database.update_job_usage(
            job_id=job_id,
            tokens_in=out.get("tokens_in", 0),
            tokens_out=out.get("tokens_out", 0),
            document_type=doc_type,
            pipeline_mode="v11",
            doc_class=(out.get("triage") or {}).get("doc_class") or out.get("doc_class"),
        )
    except Exception as e:
        print(f"[V11 DB] update_job_usage error: {e}")

    # Persist model_used + processed_at for UI display
    try:
        import database as _db
        _conn = _db._connect()
        _cur = _conn.cursor()
        _proc = out.get("processed_at") or None
        if _proc == "":
            _proc = None
        _cur.execute(
            "UPDATE jobs SET model_used = ?, processed_at = ? WHERE job_id = ?",
            (out.get("model_used"), _proc, job_id),
        )
        _conn.commit()
        _conn.close()
    except Exception as e:
        print(f"[V11 DB] update model/processed_at error: {e}")

    return job_id


# ROSETTA's reader, named here rather than read from the shared env var so the
# engine cannot drift when someone changes ROVER_PRIMARY_MODEL. Overridable for
# a bake-off, but the default is the engine's definition.
ROSETTA_MODEL = os.environ.get("ROSETTA_MODEL", "google/gemini-3.6-flash")


def _looks_incomplete(result: Dict) -> str:
    """Is this run obviously short of what the document holds? Returns a reason.

    Measured on the hardest bundle in the corpus (100306922661 — 28 pages, 8
    scanned, two declarations). Three runs of the SAME file through the SAME
    model gave: header wrong + 0 items; header right + 7 items; header right +
    0 items. Same input, three different answers. Nothing in the pipeline
    noticed, because a missing item list is not an arithmetic error — there is
    no sum to fail when there are no rows.

    This is deliberately narrow. It fires only on outcomes that cannot be right
    for a customs declaration, never on a merely surprising value:
      * a declared total with no product rows at all;
      * no total when the reader did find products.
    A document that genuinely has no items also has no total, and is left alone.
    """
    vals = result.get("values", {}) or {}

    def _v(col):
        c = vals.get(col)
        return getattr(c, "value", c)

    total = _v("total_customs_value")
    items = result.get("items") or []
    if total not in (None, "", 0) and not items:
        return "a declared total but no product rows"
    if total in (None, "") and items:
        return "product rows but no declared total"
    return ""


def _run_with_retry(pipeline_fast, pdf_path: str, on_log, retry: bool = False) -> Dict:
    """Run the reader; on an obviously-incomplete result, run it once more.

    One retry, not a loop: the failure is a coin-flip, not a systematic gap, so
    a second attempt is worth ~$0.2 and a minute. If the retry is no better the
    first result stands and the job still goes to review — a retry must never be
    able to make the outcome worse.
    """
    r = pipeline_fast.run(pdf_path, on_log=on_log)
    if not retry:
        return r
    why = _looks_incomplete(r)
    if not why:
        return r
    on_log("Incomplete read (%s) — reading once more" % why, "warn")
    try:
        r2 = pipeline_fast.run(pdf_path, on_log=on_log)
    except Exception as e:            # a failed retry must not lose the first read
        on_log("Retry failed (%s) — keeping the first read" % e, "warn")
        return r
    if _looks_incomplete(r2):
        on_log("Second read no better — keeping the first, flagged for review", "warn")
        r.setdefault("notes", []).append("retried once; still %s" % why)
        r["needs_review"] = True
        return r
    on_log("Second read is complete — using it", "ok")
    r2.setdefault("notes", []).append("first read was incomplete (%s); re-read" % why)
    return r2


# Ceiling on the serialised engine output stored in `jobs.raw_extraction`.
# A 28-page bundle serialises to a few hundred KB, so this leaves headroom on
# the worst document in the corpus; past it the extra is bulk rather than
# evidence, and the payload is rebuilt from the parts a re-projection actually
# reads (the declaration values and the item rows).
RAW_EXTRACTION_MAX_BYTES = 1_000_000

# Keys that can carry a page render or a base64 blob. Dropped at every depth,
# always — image data is never stored: it is enormous, it cannot help re-derive
# a typed column, and the source PDF is already kept on the job. `content` is
# named because that is the LLM message-block key the base64 PDF rides in.
_RAW_BLOB_KEYS = {
    "image", "images", "img", "imgs", "image_b64", "page_image", "page_images",
    "b64", "base64", "file_data", "data_uri", "thumbnail", "thumbnails",
    "png", "jpeg", "jpg", "content",
    "page_text", "pages_text", "raw_text", "full_text", "text_layer",
}

# A single string longer than this is a blob under a key we did not predict.
# No customs field — including a Cell's `source` proof text — runs this long.
_RAW_MAX_STR = 20_000


def _strip_blobs(v):
    """Recursive copy of `v` with image/base64/page-text blobs removed."""
    if isinstance(v, dict):
        return {k: _strip_blobs(x) for k, x in v.items()
                if str(k).lower() not in _RAW_BLOB_KEYS}
    if isinstance(v, (list, tuple)):
        return [_strip_blobs(x) for x in v]
    if isinstance(v, str) and (len(v) > _RAW_MAX_STR or v.startswith("data:")):
        return "<%d chars elided>" % len(v)
    return v


def _raw_snapshot(result: Dict) -> Optional[str]:
    """Serialise the engine's verbatim output for `jobs.raw_extraction`.

    Why it is kept: the engine→DB bridge below is hand-written field by field.
    When it was wrong about item values — ROVER's invoice-currency 'Item value'
    was being written into `customs_value_mmk`, ~58x off on a THB document — the
    only remedy was to re-run every document through the model and pay for
    extraction a second time, because nothing had kept what the model actually
    said. Everything stored had already been through the lossy mapping.

    With the raw read stored, that class of bug is a re-projection: fix the
    mapping, rebuild the typed columns from this JSON, no model calls, and the
    documents processed before the fix get corrected too.

    Kept: the engine's own `values`, the per-field evidence `record` (proof
    text, confidence, model, page geometry) and the raw `items` rows as emitted,
    plus the run's verdicts (suspect columns, notes, cost, token counts). Never
    kept: page renders or any base64 blob.

    Never raises. A job must not fail because a debug artifact could not be
    written — on any error the column simply stays NULL.
    """
    if not isinstance(result, dict):
        return None
    try:
        lean = _strip_blobs(result)
        payload = json.dumps(lean, default=str, ensure_ascii=False)
        if len(payload.encode("utf-8")) <= RAW_EXTRACTION_MAX_BYTES:
            return payload
        # Over the cap. Keep what a re-projection reads and say so inside the
        # payload, so a later reader cannot mistake a trimmed record for the
        # engine's whole answer.
        _keep = ("values", "items", "record", "suspect", "notes", "cost",
                 "n_items", "declared_count", "items_incomplete", "needs_review",
                 "rescued_by", "pages_total", "pdf")
        trimmed = {k: lean[k] for k in _keep if k in lean}
        trimmed["_truncated"] = ("over %d bytes — bulk keys dropped"
                                 % RAW_EXTRACTION_MAX_BYTES)
        payload = json.dumps(trimmed, default=str, ensure_ascii=False)
        if len(payload.encode("utf-8")) > RAW_EXTRACTION_MAX_BYTES:
            # Still over. The evidence record is the bulky half (one entry per
            # column, each carrying its proof text); the declaration values and
            # the item rows are the entire point of the column, so they stay
            # even if that leaves the row above the cap. An oversize row still
            # re-projects; an empty one does not.
            trimmed.pop("record", None)
            trimmed["_truncated"] = (
                "over %d bytes — evidence record and bulk keys dropped"
                % RAW_EXTRACTION_MAX_BYTES)
            payload = json.dumps(trimmed, default=str, ensure_ascii=False)
        return payload
    except Exception as e:
        print(f"[V11] raw_extraction capture skipped: {e}")
        return None


def complete_field_bboxes(pdf_path: str, decl: Dict, items: List[Dict],
                          measured: Optional[Dict] = None) -> Dict:
    """Fill in every value the engine did not already locate on the page.

    The marked PDF is drawn from `jobs.field_bboxes_json`, and only the Atlas
    path ever filled that in completely: Phase 4.5 runs the text-layer locator
    over the whole declaration + every item row. The ROVER / ROSETTA bridge
    supplied only what its own Cells happened to carry — header columns read by
    the deterministic reader, and **no item rows at all**, because
    `_bboxes_from_record` walks the header record and nothing else. So on those
    engines a run finished with a handful of marks and no product lines, and the
    marked PDF looked like the feature was half-built rather than like a
    different engine having been used.

    Measured boxes WIN. A coordinate the reader recorded while it was reading
    the cell is better evidence than a later search for the same string, which
    can land on another occurrence of it elsewhere in the bundle. This only ever
    ADDS entries.

    Never raises: a missing coordinate must not fail an extraction that
    otherwise succeeded.
    """
    out = {"declaration": dict((measured or {}).get("declaration") or {}),
           "items": {k: dict(v or {}) for k, v in
                     ((measured or {}).get("items") or {}).items()}}
    try:
        # `declaration_pages` needs the 0-based CUSDEC anchor; passing None makes
        # it return [], and an EMPTY page list means "search nothing" (None means
        # "search everything"). Getting that backwards produces zero boxes and
        # looks exactly like a scanned document.
        from v11.triage import _locate_cusdec_page
        anchor, _digital = _locate_cusdec_page(pdf_path)
        pages = _declaration_pages(pdf_path, anchor, (decl or {}).get("declaration_no"))
        found = _compute_field_bboxes(pdf_path, decl or {}, items or [], pages=pages)
    except Exception as e:
        print(f"[V11 bbox] complete_field_bboxes skipped: {e}")
        return out

    for field, bb in (found.get("declaration") or {}).items():
        out["declaration"].setdefault(field, bb)
    for idx, row in (found.get("items") or {}).items():
        tgt = out["items"].setdefault(str(idx), {})
        for field, bb in (row or {}).items():
            tgt.setdefault(field, bb)
    # An item entry that ended up empty is noise in the payload and renders as a
    # row with nothing under it.
    out["items"] = {k: v for k, v in out["items"].items() if v}
    return out


def _run_rover(pdf_path: str, job_id: str, retry_on_empty: bool = False,
               label: str = "ROVER PRO", model: str = None) -> Dict:
    """ATLAS V15 — native-PDF ROVER engine bridged into the V11 job contract.

    Runs the ROVER fast pipeline (native-PDF gemini primary + math-supervisor
    JUDGE + fail-closed review), maps its Cell record to the V11 declaration/items
    schema, persists via the same `_save_to_db` path, and emits the same live
    events the Atlas terminal consumes. The rich ROVER surface (/rover) stays the
    deep-review workbench — this is the quick single-upload lane on the Agent page.
    """
    import sys as _sys
    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")
    t0 = time.time()
    filename = Path(pdf_path).name
    model_used = "%s · Native-PDF" % label.title() if label != "ROVER PRO" \
        else "Rover Pro · Native-PDF"

    # ROSETTA pins its reader in the engine definition rather than inheriting the
    # shared env var. A model swap moved results silently this morning; an engine
    # that names its own model cannot drift underneath you.
    #
    # `llm.PRIMARY` is module state shared by every job this worker runs, so it
    # is restored below — otherwise one ROSETTA job would silently re-point every
    # later ROVER job on the same worker.
    from rover import llm as _llm
    _prev_model = _llm.PRIMARY
    if model:
        _llm.PRIMARY = model

    n_pages = None
    try:
        import fitz
        _d = fitz.open(str(pdf_path)); n_pages = len(_d); _d.close()
    except Exception:
        n_pages = None

    _emit(job_id, "JOB_START", {
        "file": filename, "pages": n_pages, "pipeline": "ROVER_PRO",
        "label": label,
    })

    def _on_log(ev, level=None):
        """Accept BOTH call shapes.

        `pipeline_fast.run` wraps its own `log(msg, level)` and calls this with a
        single dict. `_run_with_retry` calls it directly as `on_log(msg, level)` —
        two positionals, message first. The dict-only signature meant that the
        moment ROSETTA decided to retry, the callback raised
        `TypeError: _on_log() takes 1 positional argument but 2 were given`
        and the whole job FAILED instead of re-reading. The retry guard is the
        reason ROSETTA exists as a separate engine, and it had never once run.
        """
        try:
            if isinstance(ev, dict):
                msg, lvl = str(ev.get("msg", "")), ev.get("level", "info")
            else:
                msg, lvl = str(ev), (level or "info")
            _emit(job_id, "STAGE_DETAIL", {
                "label": label, "step": "rover", "msg": msg, "level": lvl,
            })
        except Exception:
            pass

    from rover import pipeline_fast
    from rover.mapping import _core_invoice  # strip 'A-'/'INV-' → bare number (team: store bare)
    from rover.deterministic import ma_decl_no_from_name
    try:
        r = _run_with_retry(pipeline_fast, pdf_path, _on_log, retry=retry_on_empty)
    finally:
        _llm.PRIMARY = _prev_model      # never leak the pin into the next job

    # Freeze the engine's verbatim answer HERE, before the hand-written mapping
    # below flattens it into the V11 schema. Serialising now rather than holding
    # a reference also means what lands in the DB is exactly what the engine
    # returned, whatever the mapping does to `r` afterwards.
    raw_extraction = _raw_snapshot(r)

    vals = r.get("values", {}) or {}

    # ROVER returns numeric strings as the form prints them ("1,394,615",
    # "111,488.4288", "THB 652,279.7184"). Postgres numeric columns reject all of
    # those, and one bad item value aborts the whole save_items batch — items
    # vanish silently. Non-numerics (dates, names, the MA-series slash id) come
    # back untouched, which is what the whole-record mapping below needs.
    _num = numeric.keep_if_unparseable

    # ROVER column → V11 declaration snake_case (the names _save_to_db + UI read).
    decl = {
        "declaration_no": vals.get("declaration_no"),
        "declaration_no_official": vals.get("declaration_no_official"),
        "declaration_date": vals.get("declaration_date"),
        "arrival_date": vals.get("arrival_date"),
        "release_order_date": vals.get("release_order_date"),
        "completion_date": vals.get("completion_date"),
        "importer_name": vals.get("importer_name"),
        "consignor_name": vals.get("consignor_name"),
        "invoice_number": _core_invoice(vals.get("invoice_number")) or None,
        "currency": vals.get("currency"),
        "exchange_rate": _num(vals.get("exchange_rate")),
        **invoice_price_fields(vals, _num),
        "freight_value": _num(vals.get("freight_value")),
        "insurance_value": _num(vals.get("insurance_value")),
        "adjustment_value": _num(vals.get("adjustment_value")),
        "total_customs_value": _num(vals.get("total_customs_value")),
        "customs_duty": _num(vals.get("import_export_customs_duty")),
        "commercial_tax": _num(vals.get("commercial_tax_ct")),
        "advance_income_tax": _num(vals.get("advance_income_tax_at")),
        "security_fee": _num(vals.get("security_fee_sf")),
        "maccs_service_fee": _num(vals.get("maccs_service_fee_mf")),
        "exemption": _num(vals.get("exemption_reduction")),
    }
    # Old-format 'MA' declarations: file under the Registration No 'MA0259/100405'
    # (team-confirmed). Filename is authoritative here — the printed stamp is OCR-hostile.
    _ma = ma_decl_no_from_name(filename)
    if _ma:
        decl["declaration_no"] = _ma

    # UI-facing alias keys (mirror the merge-phase aliasing so both name styles resolve).
    decl["import_export_customs_duty"] = decl["customs_duty"]
    decl["commercial_tax_ct"] = decl["commercial_tax"]
    decl["advance_income_tax_at"] = decl["advance_income_tax"]
    decl["security_fee_sf"] = decl["security_fee"]
    decl["maccs_service_fee_mf"] = decl["maccs_service_fee"]
    decl["exemption_reduction"] = decl["exemption"]

    items = []
    for it in (r.get("items") or []):
        qty = _num(it.get("quantity"))
        val = _num(it.get("value"))            # 'Item value' — INVOICE currency
        unit = _num(it.get("unit_price"))      # 'Invoice unit price' — INVOICE currency
        cust = _num(it.get("customs_value"))   # 'Customs value' — assessed MMK
        # ROVER items carry row value + quantity but no unit price — derive it.
        if unit in (None, "") and isinstance(qty, (int, float)) and qty \
                and isinstance(val, (int, float)):
            unit = round(val / qty, 4)
        # `customs_value_mmk` is MMK. `value` is the invoice-currency line total, which on a
        # THB document is ~60x smaller. Storing one as the other silently corrupts the column,
        # every export that reads it, and the item-sum gate — it was doing exactly that.
        # Leave it NULL when the row prints no assessed value: deriving it as value*rate is
        # wrong wherever the assessed value carries an uplift.
        items.append({
            "item_name": it.get("description") or it.get("item_name"),
            "hs_code": it.get("hs_code"),
            "quantity": qty,
            "invoice_unit_price": unit,
            "customs_value_mmk": cust,
            "currency": vals.get("currency"),
            "customs_duty_rate": _num(it.get("customs_duty_rate")),
            "origin": it.get("origin"),
            # Items share the declaration's rate — fill so the item row is complete.
            "exchange_rate": decl.get("exchange_rate"),
        })

    needs_review = bool(r.get("needs_review"))
    out = {
        "pdf": pdf_path,
        "job_id_live": job_id,
        "declaration": decl,
        "items": items,
        "document_format": "CUSDEC",
        "cost": float(r.get("cost") or 0),
        "duration_seconds": round(time.time() - t0, 2),
        "needs_review": needs_review,
        "suspect": r.get("suspect") or [],
        "sanity_flags": r.get("suspect") or [],
        "cross_val_passed": not needs_review,
        "trace": [{"phase": "rover", "engine": "native-pdf",
                   "items": len(items), "suspect": r.get("suspect"),
                   "rescued_by": r.get("rescued_by"), "notes": r.get("notes")}],
        "pipeline_version": "rover_pro",
        "model_used": model_used,
        "rover_record": r.get("record"),
        # The engine's verbatim output, already serialised. `_save_to_db`
        # persists it to jobs.raw_extraction; it is NOT part of the declaration
        # whitelist and is never read back into the mapping.
        "raw_extraction": raw_extraction,
        # Measured page coordinates for geometry-bound fields. _save_to_db already
        # persists this key (jobs.field_bboxes_json) for the Atlas path and the
        # review UI already reads it — ROVER simply never supplied it, so the
        # highlight worked on one engine and silently did nothing on the other.
        # The record's own coordinates cover part of the header and none of the
        # items, so the text-layer locator fills the rest in — same call Atlas
        # makes in Phase 4.5, no model, no cost.
        "field_bboxes": complete_field_bboxes(
            pdf_path, decl, items, r.get("field_bboxes") or {}),
        "tokens_in": int(r.get("tokens_in") or 0),
        "tokens_out": int(r.get("tokens_out") or 0),
        "total_pages": n_pages or r.get("pages_total") or 0,
    }
    # Page classification summary — _save_to_db reads this for jobs.total_pages /
    # text_pages / image_pages + doc_type; without it the review UI shows "0 pg".
    _np = int(out["total_pages"] or 0)
    _scanned = min(int(r.get("pages_scanned") or 0), _np)
    out["page_classification"] = {
        "n_pages": _np,
        "summary": {"TYPED": _np - _scanned, "HANDWRITTEN": _scanned},
    }

    db_job_id = None
    try:
        db_job_id = _save_to_db(out, pdf_path)
        out["job_id"] = db_job_id
        _emit(job_id, "DB_SAVE", {"job_id": db_job_id, "decls": 1 if decl else 0,
                                  "items": len(items)})
    except Exception as e:
        out["trace"].append({"phase": "db_save", "error": str(e)})
        _emit(job_id, "DB_SAVE", {"job_id": None, "decls": 0,
                                  "items": len(items), "error": str(e)})

    out["processed_at"] = datetime.utcnow().isoformat() + "Z"
    _emit(job_id, "DONE", {
        "total_s": out["duration_seconds"], "total_cost": out["cost"],
        "total_tokens_in": out["tokens_in"], "total_tokens_out": out["tokens_out"],
        "tokens_in": out["tokens_in"], "tokens_out": out["tokens_out"],
        "label": label, "pipeline": "ROVER_PRO",
    })
    _close(job_id)
    return out


#: Header fields whose value is printed on more than one form in a bundle, with
#: DIFFERENT figures on each. The importer's name is the same on the licence and
#: the declaration, so blanking it buys nothing; the total, the invoice and the
#: prices are the ones that ship a licence's numbers as a declaration's.
_OFF_DECLARATION_HEADER = (
    "invoice_number", "invoice_number_customs", "invoice_number_commercial",
    "invoice_price", "invoice_price_fc", "invoice_price_mmk",
    "total_customs_value", "arrival_date",
)

#: Documents that are POSITIVE evidence a lane was reading something else.
#: UNKNOWN and OTHER are deliberately absent: "could not tell" is not proof, and
#: dropping items on an absence of evidence deletes real rows from every bundle
#: the classifier finds hard.
_FOREIGN_DOCUMENTS = frozenset({"LICENCE", "INVOICE", "PACKING_LIST"})


def _scope_items(cls_pages, v7_res, v10_res, v7_pages, v10p_pages, out) -> None:
    """Drop items from a lane that demonstrably read a different document.

    Phase 3.9 keeps the typed lane's items on purpose — misrouted item pages are
    why attachment pages are read at all — and that is right when the stray pages
    are continuation sheets. It is wrong when they are an IMPORT LICENCE, which
    carries its OWN goods table: same HS codes, same product names, licence
    quantities rather than shipped ones, and its own CIF total.

    On `0259100560` the typed lane read pages 6-8 (licence) while the vision lane
    read 3-4 (CUSDEC); both were merged and a four-item declaration stored
    nineteen rows. The duplication was the visible part, not the dangerous one:
    the licence's eleven lines sum EXACTLY to the licence's own total, which the
    header had taken from the same page, so removing the duplicates alone would
    have left a self-consistent wrong answer with no gate able to fail it.

    `label` cannot separate them — a licence is machine-printed, so TYPED is an
    honest answer. Only `document` can.

    Mutates `v7_res` / `v10_res` / `out` in place. Never raises: a scoping
    failure must not cost a job its extraction.
    """
    try:
        docmap = {}
        for p in (cls_pages or []):
            if not isinstance(p, dict):
                continue
            pg = p.get("page")
            if pg:
                docmap[int(pg)] = (p.get("document") or "UNKNOWN").upper()

        # Scoping needs an anchor. With no page identified as the declaration
        # there is nothing to scope TO, so keep every lane and let the gates
        # decide — which is where this codebase stood before today.
        decl_pages = {p for p, d in docmap.items() if d == "DECLARATION"}
        lanes = [("typed", v7_res, set(v7_pages or [])),
                 ("vision", v10_res, set(v10p_pages or []))]

        # Stamp every row with the document its lane read, BEFORE deciding what to
        # drop and BEFORE the no-anchor bail-out below. `reconcile._document_check`
        # needs this on the rows that SURVIVE, and the three cases this function
        # deliberately does not drop — no page identified as the declaration, the
        # foreign lane being the only source of items, a lane with no page list —
        # are precisely the ones a downstream gate still has to see. Stamping only
        # on the drop path would leave the gate blind exactly where it is needed.
        for _n, res, pgs in lanes:
            if not res:
                continue
            if not pgs or (pgs & decl_pages):
                src = "DECLARATION" if (pgs & decl_pages) else "UNKNOWN"
            else:
                named = sorted({docmap.get(p, "UNKNOWN") for p in pgs} & _FOREIGN_DOCUMENTS)
                src = named[0] if named else "UNKNOWN"
            for it in (res.get("items") or []):
                if isinstance(it, dict):
                    it.setdefault("_src_doc", src)

        if not decl_pages:
            out["trace"].append({"phase": "item_scope",
                                 "skipped": "no page identified as DECLARATION"})
            return

        # A lane with no page list is the whole-document fallback: it read
        # everything, so it read the declaration.
        survivors = sum(1 for _, r, pg in lanes
                        if r and (r.get("items") or []) and (not pg or pg & decl_pages))

        for name, res, pgs in lanes:
            if not res:
                continue
            items = res.get("items") or []
            if not items or not pgs or (pgs & decl_pages):
                continue
            foreign = sorted({docmap.get(p, "UNKNOWN") for p in pgs} & _FOREIGN_DOCUMENTS)
            if not foreign:
                continue

            # Never leave the job with no items at all. An empty list is a
            # known-bad outcome — the ROSETTA retry guard exists because a
            # declaration with no rows has no sum to fail — and a reviewer can
            # delete a wrong row but cannot recover a dropped one.
            if survivors == 0:
                out.setdefault("sanity_flags", []).append(
                    "items_only_from_" + foreign[0].lower())
                out["needs_review"] = True
                out["trace"].append({"phase": "item_scope", "lane": name,
                                     "kept_because": "only source of items",
                                     "documents": foreign, "pages": sorted(pgs),
                                     "n_items": len(items)})
                print(f"[scope] {name} lane read {'/'.join(foreign)}, not the declaration"
                      f" — KEPT {len(items)} items (only source), review forced")
                continue

            res["items"] = []
            out.setdefault("sanity_flags", []).append("items_off_declaration")
            out["trace"].append({"phase": "item_scope", "lane": name,
                                 "dropped_items": len(items), "documents": foreign,
                                 "pages": sorted(pgs),
                                 "declaration_pages": sorted(decl_pages)})
            print(f"[scope] {name} lane read {'/'.join(foreign)} pages {sorted(pgs)}, "
                  f"not the declaration {sorted(decl_pages)} — dropped {len(items)} items")

            # A lane that owns no items owns no header either. Phase 3.9 fires on
            # `cusdec_page_digital is False`, which needs triage to have LOCATED
            # the CUSDEC by text — impossible on a bundle whose every page is a
            # photograph, which is this exact document.
            if name == "typed" and res.get("declaration"):
                hdr = res["declaration"]
                cleared = [k for k in _OFF_DECLARATION_HEADER
                           if hdr.get(k) not in (None, "")]
                for k in cleared:
                    hdr[k] = None
                if cleared:
                    out.setdefault("sanity_flags", []).append("typed_header_off_declaration")
                    out["trace"].append({"phase": "typed_header_scoped",
                                         "reason": f"typed lane read {'/'.join(foreign)}",
                                         "cleared": cleared})
                    print(f"[scope] and dropped its header fields: {', '.join(cleared)}")
    except Exception as e:
        out.setdefault("trace", []).append({"phase": "item_scope", "error": str(e)})


def run(pdf_path: str, job_id: Optional[str] = None, engine: str = "auto") -> Dict:
    """End-to-end V11 dispatch. Returns merged result + trace.

    Args:
        pdf_path: source PDF.
        job_id: optional live-event job identifier (threaded through every stage).
                If not supplied, a uuid is auto-generated so events can stream.
    """
    if not job_id:
        job_id = f"v11-{uuid.uuid4().hex[:12]}"

    # ATLAS V15 — native-PDF ROVER engine. Fully separate reader; bridged to the
    # V11 job/DB/event contract so the Agent page (upload → terminal → results →
    # review/approve) works unchanged. Never falls through to the V7/V10 body.
    # ROSETTA — the same native-PDF reader, with a determinism guard and its
    # model pinned. Named for the Release Order it reads and for deciphering a
    # scanned page. ROVER PRO stays unchanged alongside it so the two can be
    # compared on the same document.
    # ROSETTA and ROVER PRO were retired as extraction engines on 2 Aug 2026.
    #
    # Both used to return right here, above every ATLAS stage — so neither saw the
    # declaration-page scoping, the scanned-CUSDEC vision rescue, the item-sum
    # corroboration or the derived CIF adjustment. Their native-PDF reader takes the
    # text layer, and in a bundled release order whose declaration is a photograph
    # that text layer belongs to the Import Licence and the waybill: a real
    # consignment, wrong document, no stage downstream able to notice.
    #
    # A stored job, a saved queue entry or an old client can still ask for them by
    # name. Those requests run ATLAS instead of failing — the user wants their
    # document read — but the substitution is emitted and recorded rather than done
    # quietly, so a run is never mistaken for the engine that was requested.
    # ATLAS V14 is now the only engine. `presto` and `classic` are retired with
    # them: both were sub-lanes of ATLAS dressed up as choices, and selecting one
    # ran part of the pipeline with the rest disabled. ATLAS already routes each
    # page to the right lane, so there is nothing a hand-picked lane can do better.
    _req_engine = (engine or "").lower()
    if _req_engine and _req_engine != "atlas":
        print(f"[engine] '{_req_engine}' is retired — running ATLAS V14 instead")
        try:
            _emit(job_id, "STAGE_DETAIL", {
                "label": "ATLAS V14", "step": "engine",
                "msg": f"{_req_engine.upper()} has been retired; running ATLAS V14",
                "level": "warn"})
        except Exception:
            pass
    engine = "atlas"

    t0 = time.time()
    filename = Path(pdf_path).name
    current_stage = "init"

    # ─── Phase 0: JOB_START ───
    try:
        # n_pages not yet known until classifier; use fitz quickly if possible
        n_pages_est = None
        try:
            import fitz
            _d = fitz.open(str(pdf_path))
            n_pages_est = len(_d)
            _d.close()
        except Exception:
            n_pages_est = None
        # File size (best-effort).
        size_mb = None
        try:
            _bytes = Path(pdf_path).stat().st_size
            size_mb = round(_bytes / (1024 * 1024), 2)
        except Exception:
            size_mb = None
        _emit(job_id, "JOB_START", {
            "file": filename,
            "pages": n_pages_est,
            "size_mb": size_mb,
            "pipeline": "V11",
            "label": PIPELINE_LABEL["V11"],
        })
        if event_logger:
            try:
                event_logger.log_job(
                    action="JOB_START", user=None, job_id=job_id,
                    status="OK",
                    details=f"V11 routing started for {filename}",
                    payload={"pipeline": "v11", "pdf": pdf_path,
                             "pages": n_pages_est, "job_id": job_id},
                )
            except Exception:
                pass
    except Exception:
        pass

    try:
        out = {
            "pdf": pdf_path,
            "job_id_live": job_id,
            "declaration": {},
            "items": [],
            "document_format": None,
            "cost": 0,
            "duration_seconds": 0,
            "trace": [],
            "total_pages": n_pages_est or 0,
            "text_pages": 0,
            "image_pages": 0,
            "attachment_pages": 0,
            "pipeline_version": "v11",
            "model_used": "V11 Maestro",
            "processed_at": "",
        }

        # ─── Phase 1: Page classifier ───
        current_stage = "classifier"
        try:
            cls = classify_pages(pdf_path)
            out["page_classification"] = cls
            try:
                _summary = cls.get("summary", {}) or {}
                out["total_pages"] = cls.get("n_pages") or n_pages_est or 0
                out["text_pages"] = int(_summary.get("TYPED", 0) or 0)
                out["image_pages"] = int(_summary.get("HANDWRITTEN", 0) or 0)
                out["attachment_pages"] = int(_summary.get("ATTACHMENT", 0) or 0)
            except Exception:
                pass
            out["trace"].append({"phase": "classifier",
                                  "n_pages": cls.get("n_pages"),
                                  "summary": cls.get("summary"),
                                  "pages": [{"page": p["page"], "label": p["label"]}
                                            for p in cls.get("pages", [])]})

            # Per-page CLASSIFY events.
            # NOTE: classifier exposes label + qualitative confidence + reason,
            # but does NOT compute pen_ratio / typed_ratio / box_count features.
            # Those keys are emitted as None placeholders.
            for p in cls.get("pages", []) or []:
                verdict = _label_to_verdict(p.get("label"))
                conf = _conf_to_float(p.get("confidence"))
                payload = {
                    "page": p.get("page"),
                    "verdict": verdict,
                    "conf": conf,
                    "features": {
                        "pen_ratio": None,
                        "typed_ratio": None,
                        "box_count": None,
                    },
                    "reason": p.get("reason", ""),
                    # Evidence = classifier's free-text justification.
                    # Pass-through of `reason` under a stable, frontend-friendly key.
                    "evidence": p.get("reason", "") or "",
                }
                _emit(job_id, "CLASSIFY", payload)
                _log_event("PAGE_CLASSIFY", payload, job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "classifier", "error": str(e)})
            cls = {"pages": [{"page": 1, "label": "TYPED"}], "n_pages": 1,
                   "summary": {"TYPED": 1, "HANDWRITTEN": 0, "ATTACHMENT": 0}}

        # ─── Phase 1.5: Document-type triage (single authority) ───
        # Compute the doc class ONCE from the classified pages + a CUSDEC text
        # probe. Everything downstream READS this instead of re-sniffing text
        # layers: routing (fast-path vs vision), the CUSDEC rescue path (text vs
        # vision), and the review expectation for scanned docs. Recorded on the
        # job + emitted so "why slow / why flagged?" is answered up front.
        try:
            from v11.triage import compute_triage
            triage = compute_triage(pdf_path, cls, engine=engine)
            out["triage"] = triage
            out["doc_class"] = triage.get("doc_class")
            out["trace"].append({"phase": "triage", **triage})
            _emit(job_id, "TRIAGE", triage)
            _log_event("TRIAGE", triage, job_id=job_id)
        except Exception as _te:
            triage = {"doc_class": "MIXED", "fast_path_available": False,
                      "needs_vision_rescue": True, "cusdec_page": None}
            out["trace"].append({"phase": "triage", "error": str(_te)})

        # ─── Phase 2: Split PDF ───
        current_stage = "split"
        try:
            splits = split_pdf_by_labels(pdf_path, cls.get("pages", []))
            out["trace"].append({"phase": "split",
                                  "buckets": splits.get("page_buckets")})
        except Exception as e:
            out["trace"].append({"phase": "split", "error": str(e)})
            splits = {"TYPED": pdf_path, "HANDWRITTEN": None, "ATTACHMENT": None,
                      "page_buckets": {"TYPED": [], "HANDWRITTEN": [], "ATTACHMENT": []}}

        typed_pdf = splits.get("TYPED")
        hw_pdf = splits.get("HANDWRITTEN")

        # If neither typed nor HW (rare), default to V7 on full PDF
        fallback_to_full = False
        if not typed_pdf and not hw_pdf:
            typed_pdf = pdf_path
            fallback_to_full = True
            out["trace"].append({"phase": "fallback", "reason": "no typed or HW pages — full doc → V7"})

        # ─── ROUTE event ───
        buckets = (splits.get("page_buckets") or
                   {"TYPED": [], "HANDWRITTEN": [], "ATTACHMENT": []})
        v7_pages = list(buckets.get("TYPED") or [])
        v10p_pages = list(buckets.get("HANDWRITTEN") or [])
        dropped = list(buckets.get("ATTACHMENT") or [])
        if fallback_to_full and not v7_pages:
            # Whole-doc fallback → all known pages routed to V7
            try:
                v7_pages = [p.get("page") for p in cls.get("pages", []) if p.get("page")]
            except Exception:
                v7_pages = []
        route_payload = {
            "v7_pages": v7_pages,
            "v10_pro_pages": v10p_pages,
            "dropped": dropped,
        }
        _emit(job_id, "ROUTE", route_payload)
        _log_event("ROUTE", route_payload, job_id=job_id)

        # ─── Phase 3: Parallel V7 + V10 dispatch ───
        current_stage = "dispatch"
        v7_res = None
        v10_res = None
        v7_t0 = v10_t0 = None
        v7_dt = v10_dt = 0.0

        # STAGE_START events fired just before submit.
        # NOTE: ROUTE has already been emitted above with populated buckets,
        # so the frontend always sees ROUTE → STAGE_START in that order.
        if typed_pdf:
            _emit(job_id, "STAGE_START", {
                "pipeline": "V7",
                "label": PIPELINE_LABEL["V7"],
                "pages": v7_pages,
            })
        if hw_pdf:
            _emit(job_id, "STAGE_START", {
                "pipeline": "V10_PRO",
                "label": PIPELINE_LABEL["V10_PRO"],
                "pages": v10p_pages,
            })

        # TODO(STAGE_DETAIL): emit fine-grained mid-pipeline progress events
        # (vision / declaration_assembler / items_assembler / verifier / fee_verifier
        #  for V7; hw_detect / dpi_vote / reader / shape_valid / post_fix for V10_PRO).
        # Skipped here because V7 (pipeline.pipeline.run_pipeline) and V10_PRO
        # (v10_pro.workflow.run) are invoked as opaque blocking calls inside a
        # ThreadPoolExecutor — neither exposes a callback/hook interface, and the
        # spec forbids modifying their internals. To wire this up later, either:
        #   (a) thread `job_id` + an emit callback into both workflows, or
        #   (b) tail their structured logs and re-emit as STAGE_DETAIL.
        # V12 Presto eligibility: flag ON and every TYPED page is digital (has a
        # text layer). Flag off → identical to today's V7 path.
        from v11.config import PRESTO_ENABLED
        # Single source of truth: fast-path eligibility comes from triage (Phase
        # 1.5), not a re-derivation here — so routing, rescue, and the DB record
        # can never disagree about whether the doc is digital.
        _typed_digital = bool(triage.get("fast_path_available"))
        # Per-job engine choice overrides the global flag:
        #   "presto" → force fast-path (only takes effect if typed pages digital)
        #   "classic" → force V7 Veritas
        #   "auto"   → follow PRESTO_ENABLED
        _eng = (engine or "auto").lower()
        if _eng in ("presto", "atlas"):
            _use_presto = _typed_digital
        elif _eng == "classic":
            _use_presto = False
        else:
            _use_presto = bool(PRESTO_ENABLED and _typed_digital)
        # V14 Atlas: also route handwritten pages to Scribe (V13) instead of V10 PRO.
        _use_scribe = (_eng == "atlas")
        # Presto reads typed + attachment pages of the ORIGINAL pdf so misrouted
        # item pages (classifier put them in ATTACHMENT) are still captured.
        _presto_pages = sorted(set((buckets.get("TYPED") or []) + (buckets.get("ATTACHMENT") or [])))
        # Hand Presto ONLY the pages that actually have a text layer. It parses
        # embedded characters, so a scanned page contributes nothing but tokens and
        # invites the model to guess at an image it cannot see. The fast path is now
        # available whenever SOME typed page is digital (see triage.py), so this
        # filter is what keeps the promise that Presto reads text and nothing else.
        try:
            import fitz as _fitz
            _doc = _fitz.open(pdf_path)
            _digital = {i + 1 for i in range(_doc.page_count)
                        if len((_doc[i].get_text() or "").strip()) >= 30}
            _doc.close()
            _kept = [p for p in _presto_pages if p in _digital]
            if _kept and _kept != _presto_pages:
                out["trace"].append({"phase": "presto_pages_filtered",
                                     "kept": _kept,
                                     "dropped_no_text_layer":
                                         [p for p in _presto_pages if p not in _digital]})
                _presto_pages = _kept
            elif not _kept:
                _use_presto = False      # nothing readable — don't call it at all
        except Exception as _pe:
            out["trace"].append({"phase": "presto_pages_filtered", "error": str(_pe)})
        if _use_presto:
            out["trace"].append({"phase": "route_typed", "engine": "presto",
                                  "pages": _presto_pages})

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {}
            if typed_pdf:
                v7_t0 = time.time()
                futs[ex.submit(_call_typed, typed_pdf, _use_presto, pdf_path, _presto_pages)] = "v7"
            if hw_pdf:
                v10_t0 = time.time()
                futs[ex.submit(_call_scribe if _use_scribe else _call_v10, hw_pdf)] = "v10"
            for f in futs:
                label = futs[f]
                try:
                    res = f.result(timeout=900)
                    if label == "v7":
                        v7_res = res
                        v7_dt = round(time.time() - (v7_t0 or time.time()), 2)
                        out["trace"].append({"phase": "v7", "ok": True,
                                              "items": len(res.get("items", []))})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7",
                            "label": PIPELINE_LABEL["V7"],
                            "duration_s": v7_dt,
                            "cost_usd": float(res.get("cost") or res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(res.get("tokens_in") or 0),
                            "tokens_out": int(res.get("tokens_out") or 0),
                        })
                    else:
                        v10_res = res
                        v10_dt = round(time.time() - (v10_t0 or time.time()), 2)
                        out["trace"].append({"phase": "v10", "ok": True,
                                              "items": len(res.get("items", []))})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V10_PRO",
                            "label": PIPELINE_LABEL["V10_PRO"],
                            "duration_s": v10_dt,
                            "cost_usd": float(res.get("cost") or res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(res.get("tokens_in") or 0),
                            "tokens_out": int(res.get("tokens_out") or 0),
                        })
                except Exception as e:
                    out["trace"].append({"phase": label, "error": str(e)})
                    _pl = "V7" if label == "v7" else "V10_PRO"
                    _emit(job_id, "STAGE_DONE", {
                        "pipeline": _pl,
                        "label": PIPELINE_LABEL[_pl],
                        "duration_s": None,
                        "cost_usd": None,
                        "tokens_in": None,
                        "tokens_out": None,
                        "error": str(e),
                    })

        # ─── Phase 3.9: drop a header read off the wrong document ───
        # The typed lane is pointed at whichever pages carry characters. In a
        # bundled release order where the declaration itself is a SCAN, that set is
        # the Import Licence (Appendix 4b), the waybill and the permit — real papers
        # for the same shipment, carrying a different invoice number, a different
        # invoice price, and a licence quantity larger than what actually moved.
        # Nothing downstream can tell those values from the declaration's own, and
        # on 100306920231 they shipped as the answer: invoice PDG25009 (a bill of
        # lading on page 11) and a price 2.46x the declared one (page 9).
        #
        # So when triage says the CUSDEC has no text layer, the typed lane keeps its
        # ITEMS — misrouted item pages are why attachments are in the set at all —
        # and loses its HEADER. The scanned-CUSDEC vision rescue below reads those
        # fields off the declaration page. Where it cannot, the field stays blank:
        # a reviewer can fill a blank, but has no way to know a plausible number
        # came from another document.
        try:
            # `is False` on purpose: when triage itself failed, its fallback dict
            # carries no verdict, and an unknown must not be treated as a scan —
            # that would strip a header nothing is going to replace.
            if v7_res and triage.get("cusdec_page_digital") is False:
                _hdr = v7_res.get("declaration") or {}
                _off_page = ("invoice_number", "invoice_number_customs",
                             "invoice_number_commercial", "invoice_price",
                             "invoice_price_fc", "invoice_price_mmk",
                             "total_customs_value", "arrival_date")
                _cleared = [k for k in _off_page if _hdr.get(k) not in (None, "")]
                for _k in _cleared:
                    _hdr[_k] = None
                v7_res["declaration"] = _hdr
                if _cleared:
                    out.setdefault("sanity_flags", []).append("typed_header_off_cusdec")
                    out["trace"].append({"phase": "typed_header_scoped",
                                         "reason": "cusdec page has no text layer",
                                         "cleared": _cleared,
                                         "pages_read": _presto_pages})
                    print(f"[scope] cusdec is a scan — dropped typed-lane header "
                          f"fields read from pages {_presto_pages}: {', '.join(_cleared)}")
        except Exception as _se:
            out["trace"].append({"phase": "typed_header_scoped", "error": str(_se)})

        # ─── Phase 3.95: a lane that never read the declaration owns no items ───
        # Phase 3.9 above keeps the typed lane's ITEMS on purpose — misrouted item
        # pages are the reason attachment pages are read at all. That is right when
        # the stray pages are continuation sheets. It is wrong when they are an
        # IMPORT LICENCE, because a licence carries its own goods table: same HS
        # codes, same product names, licence quantities, and its own CIF total.
        #
        # On 0259100560 the typed lane read pages 6-8 (the licence) and the vision
        # lane read pages 3-4 (the CUSDEC). The merge kept both, so a 4-item
        # declaration stored 19 rows — 7 of them Belgian chocolate that is not in
        # the shipment. Worse than the duplication: the licence's 11 lines sum
        # EXACTLY to the licence's own total, which the header had also taken, so
        # stripping the duplicates alone would have produced a self-consistent
        # wrong answer with nothing left to fail. `label` could not catch this —
        # the licence is machine-printed, so it is legitimately TYPED. Only
        # `document` separates them.
        _scope_items(cls.get("pages"), v7_res, v10_res, v7_pages, v10p_pages, out)

        # ─── Phase 4: Merge ───
        current_stage = "merge"
        try:
            merged = merge_results(v7_res, v10_res)

            # ── Text layer wins on header fields it can read ──────────────────
            # Where a page carries embedded characters, those characters ARE the
            # answer; asking a model which label a number belongs to adds a guess
            # to something unambiguous. Two fields resisted every prompt wording:
            # `declaration_no` kept returning the "First approval declaration No."
            # (a different, earlier declaration lower on the page) and
            # `adjustment_value` kept returning `2`, the classification code printed
            # 33pt above the row holding the money. Both read correctly by position.
            # Scanned pages yield nothing here, so the model's reading stands.
            try:
                from v11.textlayer_header import read as _tl_read
                # Same scoping rule as Phase 3.9: when the declaration is a scan,
                # `_presto_pages` are attachments, and a label-anchored read of an
                # attachment is still a read of the wrong document.
                # Scope: the DECLARATION page, not every page with characters on it.
                # `_presto_pages` is typed ∪ attachment, which on these bundles means
                # the Import Licence and the waybill — and the licence has its own
                # value rows that would answer a label search just as readily. When
                # triage located the CUSDEC in the text layer, read only that page.
                if triage.get("cusdec_page_digital") is False:
                    _tl_pages = []
                elif triage.get("cusdec_page") is not None:
                    # The form is printed 1/2-2/2 or 1/3-3/3 and the tax block sits on
                    # either sheet, so take the located page and the one after it.
                    # `read()` stops at the first page where a field is found, so an
                    # extra page costs nothing and a missing one costs the tax table.
                    _p0 = int(triage["cusdec_page"]) + 1
                    _tl_pages = [_p0, _p0 + 1]
                else:
                    _tl_pages = _presto_pages
                _tl = _tl_read(pdf_path, _tl_pages or None) if _tl_pages else {}
                if _tl:
                    _md = merged.get("declaration") or {}
                    _fe = _md.get("_field_engine") or {}
                    _over = []
                    for _k, _v in _tl.items():
                        if str(_md.get(_k) or "") != str(_v):
                            _md[_k] = _v
                            _fe[_k] = "textlayer"
                            _over.append(_k)
                    _md["_field_engine"] = _fe
                    merged["declaration"] = _md
                    if _over:
                        out["trace"].append({"phase": "textlayer_header",
                                             "fields": _over, "values": _tl})
                        print(f"[textlayer] header from text layer for {len(_over)} "
                              f"field(s): {', '.join(sorted(_over))}")
            except Exception as _tle:
                out["trace"].append({"phase": "textlayer_header", "error": str(_tle)})
            out["declaration"] = merged.get("declaration", {})
            # Alias keys for UI compatibility (UI expects V7 schema names)
            _d = out["declaration"]
            _decl_aliases = {
                "invoice_number_customs": "invoice_number_customs_declaration",
                "invoice_number_commercial": "invoice_number_commercial_invoice",
                "customs_duty": "import_export_customs_duty",
                "commercial_tax": "commercial_tax_ct",
                "advance_income_tax": "advance_income_tax_at",
                "security_fee": "security_fee_sf",
                "maccs_service_fee": "maccs_service_fee_mf",
                "exemption": "exemption_reduction",
                "country_origin": "origin_country",
            }
            for src, dst in _decl_aliases.items():
                if src in _d and _d.get(src) not in (None, "") and not _d.get(dst):
                    _d[dst] = _d[src]
            out["items"] = merged.get("items", [])
            # Item aliases too
            _item_aliases = {
                "commercial_tax_pct": "commercial_tax_percent",
                "origin": "origin_country",
            }
            _decl_currency = _d.get("currency") or _d.get("currency_2")
            _item_review_flags = []
            for idx, it in enumerate(out["items"]):
                for src, dst in _item_aliases.items():
                    if src in it and it.get(src) not in (None, "") and not it.get(dst):
                        it[dst] = it[src]
                # Inherit currency from declaration when item-level missing
                if not it.get("currency") and _decl_currency:
                    it["currency"] = _decl_currency
                # Flag missing duty rate (None) only — 0 is often genuine (Form D / FTA / ASEAN preferential)
                _rate = it.get("customs_duty_rate")
                if _rate is None or _rate == "":
                    _item_review_flags.append({
                        "item_index": idx,
                        "field": "customs_duty_rate",
                        "value": _rate,
                        "note": "Missing duty rate — verify against tariff",
                    })
            if _item_review_flags:
                out["item_review_flags"] = _item_review_flags
            out["document_format"] = merged.get("document_format")
            out["cost"] = merged.get("cost", 0)
            out["needs_review"] = merged.get("needs_review", False)
            out["sanity_flags"] = merged.get("sanity_flags") or []
            out["v7_used"] = merged.get("v7_result", {}).get("used", False)
            out["v10_used"] = merged.get("v10_result", {}).get("used", False)

            # Best-effort merge diagnostics. Current merger.py doesn't expose
            # a per-field conflict log, so we reconstruct it locally by
            # comparing V7-normalized vs V10 declaration fields.
            try:
                from v11.agents.merger import _normalize_v7_decl  # type: ignore
                v7_decl_norm = _normalize_v7_decl((v7_res or {}).get("declaration") or {})
            except Exception:
                v7_decl_norm = (v7_res or {}).get("declaration") or {}
            v10_decl = (v10_res or {}).get("declaration") or {}

            conflict_fields = []
            resolved_by = set()
            keys = set(v7_decl_norm.keys()) | set(v10_decl.keys())
            for k in keys:
                a = v7_decl_norm.get(k)
                b = v10_decl.get(k)
                a_present = a not in (None, "", "None")
                b_present = b not in (None, "", "None")
                if a_present and b_present and str(a).strip() != str(b).strip():
                    # V7 wins per merger strategy
                    conflict_fields.append({
                        "name": k,
                        "v7": a,
                        "v10p": b,
                        "winner": "v7",
                        "reason": "v7_typed_priority",
                    })
                    resolved_by.add("v7_typed_priority")
                elif b_present and not a_present:
                    resolved_by.add("v10_pro_fill")
                elif a_present and not b_present:
                    resolved_by.add("v7_only")

            merge_payload = {
                "conflicts": len(conflict_fields),
                "resolved_by": sorted(resolved_by),
                "fields": conflict_fields,
            }
            _emit(job_id, "MERGE", merge_payload)
            _log_event("MERGE", merge_payload, job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "merge", "error": str(e)})

        # ─── Phase 4.25: Item-count mismatch fallback ───
        # If sanity flagged item_count_mismatch (extracted < PDF-declared total items),
        # re-run V7 on the FULL original PDF (forcing all pages through Veritas) and
        # replace items if recovery yields more. Catches PageClassifier misroute
        # (tail pages wrongly verdicted INKED/EXTRA → Veritas never saw them).
        try:
            _sflags = (merged or {}).get("sanity_flags") or []
            _mismatch = next((f for f in _sflags if f.startswith("item_count_mismatch")), None)
            _already_full = bool(fallback_to_full) or (typed_pdf == pdf_path and not hw_pdf)
            if _mismatch and not _already_full:
                out["trace"].append({"phase": "recover", "reason": _mismatch,
                                     "action": "rerun V7 on full PDF"})
                _emit(job_id, "STAGE_START", {
                    "pipeline": "V7_RECOVER",
                    "label": "Veritas recovery — full PDF",
                    "pages": list(range(1, (cls.get("n_pages") or 0) + 1)),
                })
                _r_t0 = time.time()
                try:
                    # Bound recovery to 600s to prevent worker hang on large PDFs
                    with ThreadPoolExecutor(max_workers=1) as _rex:
                        _rfut = _rex.submit(_call_v7, pdf_path)
                        full_res = _rfut.result(timeout=600)
                    _r_dt = round(time.time() - _r_t0, 2)
                    full_items_n = len(full_res.get("items") or [])
                    current_items_n = len(out.get("items") or [])
                    if full_items_n > current_items_n:
                        out["items"] = full_res.get("items", [])
                        # Track recovery in tokens + cost
                        out["trace"].append({"phase": "recover", "ok": True,
                                              "items_before": current_items_n,
                                              "items_after": full_items_n})
                        # Re-flag (count may now match)
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RECOVER",
                            "label": "Veritas recovery",
                            "duration_s": _r_dt,
                            "cost_usd": float(full_res.get("cost") or full_res.get("cost_usd") or 0) or 0.0,
                            "tokens_in": int(full_res.get("tokens_in") or 0),
                            "tokens_out": int(full_res.get("tokens_out") or 0),
                            "items_recovered": full_items_n - current_items_n,
                        })
                        # Accumulate recovery cost/tokens (aggregated below)
                        v7_res = v7_res or {}
                        v7_res["tokens_in"] = int(v7_res.get("tokens_in", 0)) + int(full_res.get("tokens_in", 0))
                        v7_res["tokens_out"] = int(v7_res.get("tokens_out", 0)) + int(full_res.get("tokens_out", 0))
                        v7_res["cost"] = float(v7_res.get("cost", 0) or 0) + float(full_res.get("cost", 0) or 0)
                    else:
                        out["trace"].append({"phase": "recover", "ok": False,
                                              "items_before": current_items_n,
                                              "items_after": full_items_n,
                                              "reason": "no new items recovered"})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RECOVER",
                            "label": "Veritas recovery — no gain",
                            "duration_s": _r_dt,
                        })
                except Exception as _re:
                    out["trace"].append({"phase": "recover", "error": str(_re)})
                    _emit(job_id, "STAGE_DONE", {
                        "pipeline": "V7_RECOVER",
                        "label": "Veritas recovery",
                        "error": str(_re),
                    })
        except Exception as _e:
            out["trace"].append({"phase": "recover_guard", "error": str(_e)})

        # ─── Phase 4.3: Empty-declaration safety net ───
        # If the merged DECLARATION came back empty (no total + no importer), the
        # classifier misrouted the real declaration page (e.g. a bundled release
        # order where the customs declaration was tagged ATTACHMENT/"blank"). The
        # item-recovery paths above only restore ITEMS, never the header — so an
        # empty header would ship empty. Here we re-run Veritas (V7) on the FULL
        # PDF and adopt its declaration + items wholesale. Engine-agnostic; only
        # fires when there is genuinely nothing to lose.
        try:
            _decl_now = out.get("declaration") or {}
            _has_total = _reconcile._to_float(_decl_now.get("total_customs_value")
                                              or _decl_now.get("Total Customs Value"))
            _imp = _decl_now.get("importer_name") or _decl_now.get("Importer (Name)") or ""
            _has_importer = bool(str(_imp).strip())
            _decl_empty = not _has_total and not _has_importer
            _already_full2 = bool(fallback_to_full) or (typed_pdf == pdf_path and not hw_pdf)
            if _decl_empty and not _already_full2:
                out["trace"].append({"phase": "decl_rescue",
                                     "reason": "empty declaration after merge",
                                     "action": "rerun V7 on full PDF (adopt header+items)"})
                _emit(job_id, "STAGE_START", {
                    "pipeline": "V7_RESCUE",
                    "label": "Veritas rescue — empty declaration",
                    "pages": list(range(1, (cls.get("n_pages") or 0) + 1)),
                })
                _rs_t0 = time.time()
                try:
                    with ThreadPoolExecutor(max_workers=1) as _rsx:
                        _rsfut = _rsx.submit(_call_v7, pdf_path)
                        rescue_raw = _rsfut.result(timeout=600)
                    rescue_norm = merge_results(rescue_raw, None)
                    rdecl = rescue_norm.get("declaration") or {}
                    ritems = rescue_norm.get("items") or []
                    rtotal = _reconcile._to_float(rdecl.get("total_customs_value"))
                    if rtotal and rtotal > 0:
                        out["declaration"] = rdecl
                        if len(ritems) > len(out.get("items") or []):
                            out["items"] = ritems
                        out["v7_used"] = True   # surfaces in model_used label
                        out["trace"].append({"phase": "decl_rescue", "ok": True,
                                             "total_customs_value": rtotal,
                                             "items": len(out.get("items") or [])})
                        # Accumulate rescue cost/tokens
                        v7_res = v7_res or {}
                        v7_res["tokens_in"] = int(v7_res.get("tokens_in", 0)) + int(rescue_raw.get("tokens_in", 0) or 0)
                        v7_res["tokens_out"] = int(v7_res.get("tokens_out", 0)) + int(rescue_raw.get("tokens_out", 0) or 0)
                        v7_res["cost"] = float(v7_res.get("cost", 0) or 0) + float(rescue_raw.get("cost", 0) or rescue_raw.get("cost_usd", 0) or 0)
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RESCUE", "label": "Veritas rescue",
                            "duration_s": round(time.time() - _rs_t0, 2),
                            "total_customs_value": rtotal,
                            "items": len(out.get("items") or [])})
                    else:
                        out["trace"].append({"phase": "decl_rescue", "ok": False,
                                             "reason": "full V7 also produced no total"})
                        _emit(job_id, "STAGE_DONE", {
                            "pipeline": "V7_RESCUE", "label": "Veritas rescue — no header",
                            "duration_s": round(time.time() - _rs_t0, 2)})
                except Exception as _rse:
                    out["trace"].append({"phase": "decl_rescue", "error": str(_rse)})
                    _emit(job_id, "STAGE_DONE", {"pipeline": "V7_RESCUE",
                                                 "label": "Veritas rescue", "error": str(_rse)})
        except Exception as _e:
            out["trace"].append({"phase": "decl_rescue_guard", "error": str(_e)})

        # ─── Phase 4.35: CUSDEC tax/total rescue ───
        # Bundled release-order PDFs carry an authoritative MACCS CUSDEC page
        # whose tax block (CD/CT/AT/SF/MF) + real total/rate the LLM often misses
        # when it anchors on the Import-Licence pages. Fill them deterministically.
        try:
            from v11.tools import cusdec_rescue as _cusdec
            _n_before = len(out.get("items") or [])
            out["declaration"], out["items"], _cus_used = _cusdec.apply_cusdec(
                out.get("declaration") or {}, pdf_path, out.get("items") or [])
            if _cus_used:
                _swapped = "; items←CUSDEC" if len(out.get("items") or []) != _n_before else ""
                _emit(job_id, "STAGE_DETAIL", {"label": "ATLAS V14", "step": "cusdec",
                                               "msg": "CUSDEC tax/total rescue applied" + _swapped})
        except Exception as _ce:
            out.setdefault("trace", []).append({"phase": "cusdec_rescue", "error": str(_ce)})

        # ─── Phase 4.36: Scanned-CUSDEC vision rescue (L2) ───
        # When triage says the CUSDEC page is a SCAN (no text layer), the
        # deterministic text rescue above can't run — so a targeted single vision
        # read of that page recovers rate/date/taxes/total. Only fires on scanned
        # docs still missing those fields, so digital docs pay nothing. Vision-read
        # values fill only blanks (never clobber a deterministic value); CUSDEC
        # remains the legal source. Never raises.
        try:
            _decl = out.get("declaration") or {}
            # Fires on any scanned-CUSDEC doc (the deterministic text rescue can't
            # run there). One focused vision call (~$0.01 / ~30s, scanned docs only)
            # recovers the authoritative header — cheaper and more accurate than
            # trusting V7's whole-doc vision guess for these fields.
            if triage.get("needs_vision_rescue"):
                from v11.tools.vision_rescue import vision_cusdec_fields
                _vf = vision_cusdec_fields(pdf_path, triage.get("cusdec_page"))
                # An empty rescue used to leave no trace at all. On one document in
                # the 20-document run it returned nothing, six fields were lost, and
                # the identical call succeeded on re-run in 44 seconds — so the cause
                # was transient and completely undiagnosable after the fact. Retry
                # once, and record the failure either way.
                if not _vf:
                    out["trace"].append({"phase": "vision_rescue", "result": "empty",
                                         "cusdec_page": triage.get("cusdec_page"),
                                         "action": "retrying once"})
                    print("[vision] rescue returned nothing — retrying once")
                    _vf = vision_cusdec_fields(pdf_path, triage.get("cusdec_page"))
                    if _vf:
                        out.setdefault("sanity_flags", []).append("vision_rescue_retried")
                    else:
                        out.setdefault("sanity_flags", []).append("vision_rescue_empty")
                        out["trace"].append({"phase": "vision_rescue",
                                             "result": "empty after retry"})
                        _emit(job_id, "STAGE_DETAIL", {
                            "label": "ATLAS V14", "step": "vision_rescue",
                            "msg": "scanned CUSDEC could not be read — header fields "
                                   "will be blank", "level": "warn"})
                if _vf:
                    # On a scanned CUSDEC the page-focused vision read is more
                    # authoritative than V7's whole-doc vision guess, so for the
                    # legal-source header fields the CUSDEC value WINS (same as the
                    # deterministic text rescue's _PREFER) — otherwise a wrong-but-
                    # non-blank rate from V7 would never be replaced. Softer fields
                    # only fill blanks. The rate guard (Phase 4.4) still validates.
                    _authoritative = {
                        "exchange_rate", "currency", "declaration_date",
                        "total_customs_value", "declaration_no",
                        "import_export_customs_duty", "commercial_tax_ct",
                        "advance_income_tax_at", "security_fee_sf", "maccs_service_fee_mf",
                        "freight_value", "insurance_value", "adjustment_value",
                        # The typed lane reads these off whichever page carries
                        # characters, which on a scanned-CUSDEC bundle is an Import
                        # Licence or a waybill — a real consignment with different
                        # figures. The declaration page wins.
                        "invoice_number", "invoice_price_fc", "invoice_price_mmk",
                        "arrival_date",
                    }
                    _filled = []
                    _fev = _decl.get("_field_engine") or {}
                    for _k, _v in _vf.items():
                        if _k.startswith("_") or _v is None:
                            continue
                        if _k in _authoritative or not _decl.get(_k):
                            _decl[_k] = _v
                            _filled.append(_k)
                            # Tagged by the writer. A scanned page has no text to
                            # check a reading against, and the review screen has to
                            # be able to say so rather than presenting it like a
                            # text-layer read.
                            _fev[_k] = "vision_cusdec"
                    _decl["_field_engine"] = _fev
                    out["declaration"] = _decl
                    # Where on the page the model read each value. A scanned
                    # declaration has no text layer, so Phase 4.5's search finds
                    # nothing and the reviewer is told "location not known" for
                    # every field on roughly half the corpus — while the model
                    # that read the page was looking straight at them.
                    #
                    # Only for fields this rescue actually WROTE. A box belongs
                    # to the reading it came from: if a deterministic value won
                    # the field, the stored number came off a different page and
                    # the vision coordinate would be pointing at a figure that
                    # is not the one on screen.
                    _vbox = (_vf.get("_boxes") or {}) if isinstance(_vf, dict) else {}
                    if _vbox and _filled:
                        out["vision_boxes"] = {k: v for k, v in _vbox.items()
                                               if k in set(_filled)}
                    if _filled:
                        out.setdefault("sanity_flags", []).append("vision_cusdec_rescue")
                        _emit(job_id, "STAGE_DETAIL", {"label": "ATLAS V14", "step": "vision_rescue",
                            "msg": f"scanned CUSDEC vision rescue: {', '.join(_filled[:6])}"})
        except Exception as _ve:
            out.setdefault("trace", []).append({"phase": "vision_rescue", "error": str(_ve)})

        # ─── Phase 4.365: let the item block correct a stamped total ───
        # The assessed total sits under the customs PASS stamp on these forms, and
        # vision misreads a digit inside it — 64,691,681.2 for a printed
        # 64,691,431.29, twice, in agreement, at two resolutions. Two votes cannot
        # catch that: they misread the same pixels the same way, so agreement means
        # consistency, not accuracy.
        #
        # The same figure is printed a second time in the item block, which carries
        # no stamp, and the reconcile gate already computes that sum. A SMALL
        # disagreement between the two means the digits differ; a LARGE one means
        # items are missing, which is a different problem and must still fail the
        # gate. So only a sub-1% gap is treated as a misread and corrected — the
        # item block is the corroborating reading, not an override.
        try:
            _d365 = out.get("declaration") or {}
            _items365 = out.get("items") or []
            def _fv(v):
                try:
                    return float(str(v).replace(",", "").strip())
                except (TypeError, ValueError, AttributeError):
                    return None
            _decl_tot = _fv(_d365.get("total_customs_value"))
            _sums = [_fv(i.get("customs_value_mmk")) for i in _items365]
            _sums = [s for s in _sums if s is not None]
            if _sums and len(_sums) == len(_items365) and sum(_sums) > 0:
                _isum = sum(_sums)
                # A stamped cell does not fail the same way twice. One run of this
                # document returned a misread total, the next returned none at all
                # and the field defaulted to 0.0 — which reads as "the declaration
                # says zero" rather than "nobody could see it". Both cases resolve
                # to the same corroborating source, so handle them together:
                # missing/zero adopts the item sum outright, present-but-close is a
                # digit correction. A wide gap still means missing items and is left
                # for the gate to fail.
                _gap365 = abs(_isum - _decl_tot) if _decl_tot else None
                # A MEASURED total is not corrected by a corroboration. This phase
                # exists for a total read off a page by a model — the figure under
                # the customs PASS stamp, where two votes misread the same pixels
                # the same way and agreement proves nothing. A deterministic reader
                # is a different kind of answer: it did not look at the number, it
                # copied the characters the page actually carries.
                #
                # On 100329052130 the text layer read `(10) Total customs value
                # 109,138,893.66` exactly, three item rows summed 197,001 higher —
                # 0.18%, inside this window — and the corroborator overwrote the
                # evidence with the thing it was meant to check. The gate then
                # passed, because it compared the replacement against the very rows
                # that produced it.
                #
                # A blank still adopts the item sum whatever the writer says: no
                # reading was destroyed, because there was none.
                _fe_now = (_d365.get("_field_engine") or {}).get("total_customs_value")
                _measured = _fe_now in ("textlayer", "cusdec_text")
                if _decl_tot and _measured:
                    out["trace"].append({"phase": "total_item_corroboration",
                                         "skipped": "total was read deterministically",
                                         "writer": _fe_now, "decl": _decl_tot,
                                         "item_sum": round(_isum, 2)})
                    print(f"[total] kept {_decl_tot} from {_fe_now} — item sum "
                          f"{round(_isum, 2)} does not overrule a measured value")
                elif (not _decl_tot) or (0 < _gap365 <= _decl_tot * 0.01):
                    _d365["total_customs_value"] = round(_isum, 2)
                    _fe365 = _d365.get("_field_engine") or {}
                    _fe365["total_customs_value"] = "item_sum_corroborated"
                    _d365["_field_engine"] = _fe365
                    out["declaration"] = _d365
                    out.setdefault("sanity_flags", []).append("total_from_item_sum")
                    out["trace"].append({"phase": "total_item_corroboration",
                                         "was": _decl_tot, "now": round(_isum, 2),
                                         "gap": round(_gap365, 2) if _gap365 else None})
                    print(f"[total] stamped read {_decl_tot} -> item sum "
                          f"{round(_isum, 2)}"
                          + (f" (gap {round(_gap365, 2)})" if _gap365 else " (was blank)"))
        except Exception as _te365:
            out.setdefault("trace", []).append({"phase": "total_item_corroboration",
                                                "error": str(_te365)})

        # ─── Phase 4.37: derive the adjustment the page will not give up ───
        # On these forms the round customs PASS stamp is printed straight across the
        # "Adjustment value" cell. Four vision reads of one such page returned null,
        # 2156382.176, 24882.176 and 339882.176 — the model invents digits it cannot
        # see, and a wrong build-up quietly tightens the CIF tolerance as though a
        # real one existed. The value is recoverable by arithmetic instead: the CIF
        # identity the reconcile gate already enforces is
        #     (invoice + freight + insurance + adjustment) x rate = total
        # so the adjustment is whatever the declared total does not otherwise
        # explain. On 100306920231 that yields 334,852.25 against a printed
        # 334,852.176. Only filled when blank, only from values that are all present,
        # and always marked derived so no one mistakes it for something read.
        try:
            def _f(v):
                """Strict float or None. `_num` in this module is
                `keep_if_unparseable`, which hands back the ORIGINAL string when it
                is not an amount — fine for the DB bridge it was written for, fatal
                for arithmetic."""
                if v is None or v == "":
                    return None
                try:
                    return float(str(v).replace(",", "").strip())
                except (TypeError, ValueError):
                    return None

            _d4 = out.get("declaration") or {}
            if _d4.get("adjustment_value") in (None, ""):
                _tot = _f(_d4.get("total_customs_value"))
                _rate = _f(_d4.get("exchange_rate"))
                _inv = _f(_d4.get("invoice_price_fc")) or _f(_d4.get("invoice_price"))
                if _tot and _rate and _inv and _rate > 0:
                    _gap = (_tot / _rate) - _inv - (_f(_d4.get("freight_value")) or 0) \
                           - (_f(_d4.get("insurance_value")) or 0)
                    # A rounding remainder is not an adjustment, and a gap wider than
                    # the invoice itself means one of the inputs is wrong — in either
                    # case leave it blank rather than manufacture a build-up.
                    if abs(_gap) > max(1.0, _inv * 0.001) and abs(_gap) <= _inv * 2:
                        _d4["adjustment_value"] = round(_gap, 4)
                        _fe4 = _d4.get("_field_engine") or {}
                        _fe4["adjustment_value"] = "derived_cif"
                        _d4["_field_engine"] = _fe4
                        out["declaration"] = _d4
                        out.setdefault("sanity_flags", []).append("adjustment_derived")
                        out["trace"].append({"phase": "adjustment_derived",
                                             "value": round(_gap, 4)})
        except Exception as _ade:
            out.setdefault("trace", []).append({"phase": "adjustment_derived",
                                                "error": str(_ade)})

        # ─── Phase 4.38: refuse a value that is really its neighbour's ───
        # Two fields came back holding, verbatim, another field of the same
        # declaration: Commercial Tax carrying the Exemption/Reduction figure, and the
        # invoice number carrying the declaration number. Both are the same old
        # failure — a label was matched and the nearest value taken without checking
        # the value belonged to that label — and both are impossible on a real form.
        # Blank beats a confident wrong number: a reviewer fills a blank, and nobody
        # questions a plausible figure sitting in the right-looking column.
        try:
            _d38 = out.get("declaration") or {}

            def _same(a, b):
                if a in (None, "") or b in (None, ""):
                    return False
                try:
                    fa, fb = float(a), float(b)
                except (TypeError, ValueError):
                    return str(a).strip().upper() == str(b).strip().upper()
                # Two zeros are not a copied neighbour. On these declarations a duty
                # of 0 and an Exemption/Reduction of 0 are BOTH printed on the form,
                # and equality between them carries no information at all — every
                # blank tax on every clean document matches this way. Blanking there
                # deletes the one reading a reviewer cannot reconstruct: a stored
                # NULL says "nobody could read it", while the form says zero.
                # Measured on 100325461351 and 100329052130, where the text layer
                # read duty 0.0 correctly and this guard removed it.
                if fa == 0 and fb == 0:
                    return False
                return abs(fa - fb) <= 0.01

            _cleared38 = []
            for _tax in ("commercial_tax_ct", "import_export_customs_duty",
                         "advance_income_tax_at"):
                if _same(_d38.get(_tax), _d38.get("exemption_reduction")):
                    _d38[_tax] = None
                    _cleared38.append(f"{_tax}=exemption_reduction")
            if _same(_d38.get("invoice_number"), _d38.get("declaration_no")):
                _d38["invoice_number"] = None
                _cleared38.append("invoice_number=declaration_no")
            if _cleared38:
                out["declaration"] = _d38
                out.setdefault("sanity_flags", []).append("neighbour_value_rejected")
                out["trace"].append({"phase": "neighbour_guard", "cleared": _cleared38})
                print(f"[guard] blanked field(s) holding a neighbour's value: "
                      f"{', '.join(_cleared38)}")
        except Exception as _ge:
            out.setdefault("trace", []).append({"phase": "neighbour_guard", "error": str(_ge)})

        # ─── Phase 4.4: Reconciliation gate (the common invariant) ───
        # One guard, one chokepoint: the declared customs total must equal the
        # sum of item customs values. Any upstream leak — misclassified page,
        # split bug, V7/V10 item miss — surfaces here as a broken equation.
        # When it breaks and ATTACHMENT pages exist, try to RECOVER (those pages
        # are often misrouted item pages). If still off, FLAG for human review.
        current_stage = "reconcile"
        try:
            verdict = _reconcile.reconcile(out.get("declaration") or {}, out.get("items") or [])
            recovery = {"attempted": False, "added_items": 0, "from_pages": []}

            # Recovery re-extracts dropped item pages — only worth its cost when the
            # ITEM SUM is genuinely short (declared > Σitems beyond tolerance). A pure
            # rate / CIF / tax imbalance (balanced=False for those reasons) is NOT a
            # missing-item problem, so re-running full V7 on the attachment pages can't
            # help — the rate guard + review handle it. Gating here avoids the wasteful
            # second full-V7 vision pass on scanned docs whose only issue is the rate.
            _item_short = (verdict.get("checked")
                           and verdict.get("gap_pct", 0) > verdict.get("tolerance_pct", 5)
                           and (verdict.get("gap_value") or 0) > 0)
            if (_item_short
                    and _reconcile.recovery_enabled()
                    and splits.get("ATTACHMENT")):
                recovery["attempted"] = True
                recovery["from_pages"] = list(buckets.get("ATTACHMENT") or [])
                attach_pdf = splits.get("ATTACHMENT")
                out["trace"].append({"phase": "reconcile_recover",
                                     "gap_pct": verdict["gap_pct"],
                                     "attachment_pages": recovery["from_pages"]})
                try:
                    # Re-extract the dropped slice with Veritas, normalize via merger.
                    rec_raw = _call_v7(attach_pdf)
                    rec_norm = merge_results(rec_raw, None)
                    rec_items = rec_norm.get("items") or []
                    before_n = len(out["items"])
                    candidate = _reconcile.merge_recovered_items(out["items"], rec_items)
                    cand_verdict = _reconcile.reconcile(out.get("declaration") or {}, candidate)
                    # Keep recovered items only if they move us toward balance.
                    if abs(cand_verdict["gap_value"]) <= abs(verdict["gap_value"]):
                        out["items"] = candidate
                        recovery["added_items"] = len(out["items"]) - before_n
                        verdict = cand_verdict
                        # Stash recovered usage; applied after token aggregation
                        # below (which would otherwise overwrite tokens_in/out).
                        recovery["cost"] = float(rec_raw.get("cost") or rec_raw.get("cost_usd") or 0)
                        recovery["tokens_in"] = int(rec_raw.get("tokens_in") or 0)
                        recovery["tokens_out"] = int(rec_raw.get("tokens_out") or 0)
                        recovery["cost_breakdown"] = [{**e, "branch": "v7_recover"}
                                                      for e in (rec_raw.get("cost_breakdown") or [])]
                except Exception as _re:
                    out["trace"].append({"phase": "reconcile_recover", "error": str(_re)})

            # Verdict drives the human-review gate. Unbalanced OR un-checkable
            # (no trustworthy anchor) → force review. Never ship a silent gap.
            out["cross_val_passed"] = bool(verdict["checked"] and verdict["balanced"])
            out["reconcile"] = {**verdict, "recovery": recovery}
            if not out["cross_val_passed"]:
                out["needs_review"] = True

            # ─── Exchange-rate guard: auto-correct + fail-closed (v2026.6.17) ───
            # The FX rate is the single most error-prone header field — a magnitude-
            # capped regex, scrambled MACCS text layers, and scanned CUSDECs all
            # produce silently-wrong rates (500 for USD, 636.2576 for THB, an
            # invoice fragment like 887.18). reconcile() now cross-checks it against
            # the math-derived rate (total ÷ CIF/item basis). On a suspect rate,
            # adopt the derived value when we have one and ALWAYS force review — a
            # wrong rate must never reach the export unflagged. Original + derived
            # are preserved in out["reconcile"] (extracted_rate/derived_rate).
            if verdict.get("rate_suspect"):
                _decl = out.get("declaration") or {}
                _dr = verdict.get("derived_rate")
                # Auto-correct ONLY when the derivation is trustworthy (full CIF basis
                # present). On an incomplete basis `derived_rate` over-estimates, so we
                # never overwrite with it — we flag for human review instead. Fail-closed
                # either way: a suspect rate never ships silently.
                # Never overwrite a printed rate that is already inside its currency
                # band — that value was read straight off the doc and the derivation
                # can be skewed by a misread basis (adjustment code "2" vs the real
                # 44,612.82). Auto-correct only when the extracted rate is missing or
                # out-of-band (the genuine failures: 500 for USD, 636 for THB) AND the
                # derivation is trustworthy. Fail-closed either way: still forced review.
                _corrected = bool(_dr and verdict.get("derived_trustworthy")
                                  and not verdict.get("extracted_in_band"))
                if _corrected:
                    _decl["exchange_rate"] = _dr   # existing column; audit in reconcile
                    out["declaration"] = _decl
                out.setdefault("sanity_flags", []).append("exchange_rate_suspect")
                out["needs_review"] = True
                _emit(job_id, "STAGE_DETAIL", {"label": "ATLAS V14", "step": "rate_guard",
                    "msg": ("exchange rate suspect (extracted="
                            f"{verdict.get('extracted_rate')}, derived={_dr}) → "
                            + ("auto-corrected + flagged" if _corrected else "flagged for review"))})

            # Per-row math gate: a suspect individual item (value ≠ qty×price×rate)
            # → force review even if the total balances.
            if verdict.get("rows_checked") and not verdict.get("rows_ok"):
                out["needs_review"] = True
                out["bad_rows"] = verdict.get("bad_rows")

            # JUDGE — confidence score → auto-ok vs review (additive, advisory).
            try:
                from v11.tools import judge as _judge
                jv = _judge.judge(out, verdict)
                out["confidence"] = jv
                if jv.get("needs_review"):
                    out["needs_review"] = True
            except Exception:
                pass

            # LEARNER priors — advisory cross-check (e.g. exchange rate out of the
            # importer's learned range). Warnings only; never blocks. Safe on empty DB.
            try:
                from v11.learn import priors as _priors
                warns = _priors.check_against_priors(out.get("declaration") or {})
                if warns:
                    out["prior_warnings"] = warns
                    out["needs_review"] = True
            except Exception:
                pass

            out["trace"].append({"phase": "reconcile",
                                 "balanced": verdict["balanced"],
                                 "checked": verdict["checked"],
                                 "gap_pct": verdict["gap_pct"],
                                 "items_sum": verdict["items_sum"],
                                 "declared_total": verdict["declared_total"],
                                 "recovered": recovery["added_items"]})
            _emit(job_id, "RECONCILE", {
                "balanced": verdict["balanced"],
                "checked": verdict["checked"],
                "gap_pct": verdict["gap_pct"],
                "gap_value": verdict["gap_value"],
                "items_sum": verdict["items_sum"],
                "declared_total": verdict["declared_total"],
                "anchor": verdict["anchor"],
                "recovered_items": recovery["added_items"],
                "recovered_from_pages": recovery["from_pages"],
                "needs_review": out.get("needs_review", False),
            })
            _log_event("RECONCILE", out["reconcile"], job_id=job_id)
        except Exception as e:
            out["trace"].append({"phase": "reconcile", "error": str(e)})

        out["duration_seconds"] = round(time.time() - t0, 1)

        # Aggregate tokens from V7 + V10 PRO
        out["tokens_in"] = (v7_res.get("tokens_in", 0) if v7_res else 0) + \
                           (v10_res.get("tokens_in", 0) if v10_res else 0)
        out["tokens_out"] = (v7_res.get("tokens_out", 0) if v7_res else 0) + \
                            (v10_res.get("tokens_out", 0) if v10_res else 0)
        breakdown = []
        if v7_res:
            breakdown += [{**e, "branch": "v7"} for e in (v7_res.get("cost_breakdown") or [])]
        if v10_res:
            breakdown += [{**e, "branch": "v10_pro"} for e in (v10_res.get("cost_breakdown") or [])]
        # Fold in recovery pass usage (reconciliation re-extraction), if any.
        _rec = out.get("reconcile", {}).get("recovery", {}) if isinstance(out.get("reconcile"), dict) else {}
        if _rec.get("added_items"):
            out["tokens_in"] = int(out.get("tokens_in") or 0) + int(_rec.get("tokens_in") or 0)
            out["tokens_out"] = int(out.get("tokens_out") or 0) + int(_rec.get("tokens_out") or 0)
            out["cost"] = round(float(out.get("cost") or 0) + float(_rec.get("cost") or 0), 6)
            breakdown += _rec.get("cost_breakdown") or []
        out["cost_breakdown"] = breakdown

        # ─── Phase 4.5: Compute field bboxes (best-effort, fitz text search) ───
        # Scoped to the declaration's own pages. These bundles print the importer
        # name, the invoice number and the declaration number on the invoice and
        # the packing list too, so a first-hit search over the whole PDF sent the
        # reviewer to the wrong document to confirm a customs figure — measured
        # at 31 of 54 boxes across the UAT corpus, every one a real occurrence of
        # the right text on the wrong page.
        try:
            _decl = out.get("declaration") or {}
            _bb_pages = _declaration_pages(
                pdf_path,
                (triage or {}).get("cusdec_page"),
                _decl.get("declaration_no"),
            )
            out["bbox_pages"] = _bb_pages
            out["field_bboxes"] = _compute_field_bboxes(
                pdf_path, _decl, out.get("items") or [], pages=_bb_pages
            )
            # A photographed declaration has nothing for the search above to
            # match, so on those documents it returns an empty set and every
            # field reports "location not known". Phase 4.36 already asked the
            # model that read the page where it read each value; fold those in.
            #
            # ADDITIVE only. The text-layer hit is a measurement of the printed
            # string; the model's box is a report of where it looked. When both
            # exist the measurement wins, and in practice they never both exist
            # — a page with a text layer never reaches the vision rescue.
        except Exception as _bbe:
            out["field_bboxes"] = {}
            out["trace"].append({"phase": "bbox", "error": str(_bbe)})
        try:
            _vb = out.get("vision_boxes") or {}
            if _vb:
                _dst = out.setdefault("field_bboxes", {}).setdefault("declaration", {})
                _added = [f for f in _vb if f not in _dst]
                for _f in _added:
                    _dst[_f] = _vb[_f]
                if _added:
                    out["trace"].append({"phase": "bbox", "vision_boxes": len(_added)})
        except Exception as _vbe:
            out["trace"].append({"phase": "bbox", "vision_boxes_error": str(_vbe)})

        # Derive model_used label BEFORE save (DB stores it; History reads it).
        _v7_used = bool(out.get("v7_used"))
        _v10_used = bool(out.get("v10_used"))
        _eng_choice = (engine or "auto").lower()
        # Atlas version scheme: V14 = unified; V14-1 = Swift (typed),
        # V14-2 = Vision (handwriting). Legacy paths keep their Gen-1 names.
        _ran = []
        if locals().get("_use_presto"):
            _ran.append("V14-1 Swift")
        elif _v7_used:
            _ran.append("Atlas Classic")
        if locals().get("_use_scribe"):
            _ran.append("V14-2 Vision")
        elif _v10_used:
            _ran.append("Atlas Heritage")
        _ran_str = (" (" + " + ".join(_ran) + ")") if _ran else ""
        if _eng_choice == "atlas":
            out["model_used"] = "Atlas V14" + _ran_str
        elif _eng_choice == "presto":
            out["model_used"] = "Atlas V14-1 Swift" + _ran_str
        elif _eng_choice == "classic":
            out["model_used"] = "Atlas Classic" + _ran_str
        else:
            out["model_used"] = "Atlas V14 Core" + _ran_str

        # ─── Phase 5: Save merged result to DB ───
        current_stage = "db_save"
        db_job_id = None
        try:
            # Retry on transient SQLite "database is locked" errors (event_logger contention).
            _save_err = None
            for _attempt in range(5):
                try:
                    db_job_id = _save_to_db(out, pdf_path)
                    _save_err = None
                    break
                except Exception as _se:
                    _save_err = _se
                    _msg = str(_se).lower()
                    if "locked" in _msg or "busy" in _msg:
                        time.sleep(0.5 * (_attempt + 1))
                        continue
                    raise
            if _save_err is not None:
                raise _save_err
            out["job_id"] = db_job_id
            out["trace"].append({"phase": "db_save", "ok": True, "job_id": db_job_id})
            _emit(job_id, "DB_SAVE", {
                "job_id": db_job_id,
                "decls": 1 if out.get("declaration") else 0,
                "items": len(out.get("items") or []),
            })
        except Exception as e:
            out["trace"].append({"phase": "db_save", "error": str(e)})
            _emit(job_id, "DB_SAVE", {
                "job_id": None,
                "decls": 0,
                "items": len(out.get("items") or []),
                "error": str(e),
            })

        # ─── Final: DONE ───
        total_s = round(time.time() - t0, 2)

        # (model_used label already computed before save above.)
        out["processed_at"] = datetime.utcnow().isoformat() + "Z"

        done_payload = {
            "total_s": total_s,
            "total_cost": float(out.get("cost") or 0),
            "total_tokens_in": int(out.get("tokens_in") or 0),
            "total_tokens_out": int(out.get("tokens_out") or 0),
        }
        _emit(job_id, "DONE", done_payload)

        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_SUCCESS", user=None, job_id=db_job_id or job_id,
                    status="OK",
                    duration_ms=int(total_s * 1000),
                    details=f"V11 extracted {len(out['items'])} items, ${out.get('cost', 0):.4f}",
                    payload={"pipeline": "v11",
                             "live_job_id": job_id,
                             "items": len(out["items"]),
                             "cost": out.get("cost", 0),
                             "tokens_in": out.get("tokens_in", 0),
                             "tokens_out": out.get("tokens_out", 0),
                             "v7_used": out.get("v7_used"),
                             "v10_used": out.get("v10_used"),
                             "needs_review": out.get("needs_review")},
                )
        except Exception:
            pass

        _close(job_id)
        return out
    except Exception as e:
        # ─── FAIL ───
        try:
            _emit(job_id, "FAIL", {"stage": current_stage, "error": str(e)})
        except Exception:
            pass
        try:
            if event_logger:
                event_logger.log_job(
                    action="JOB_FAIL", user=None, job_id=job_id,
                    status="FAILED",
                    duration_ms=int((time.time() - t0) * 1000),
                    details=f"V11 failed: {e}",
                    error=str(e),
                    payload={"pipeline": "v11", "pdf": pdf_path,
                             "stage": current_stage,
                             "error": str(e)[:500]},
                )
        except Exception:
            pass
        _close(job_id)
        raise


# CLI
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) < 2:
        print("Usage: python -m v11.workflow <pdf_path> [job_id]")
        sys.exit(1)
    _job = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(run(sys.argv[1], job_id=_job), indent=2, ensure_ascii=False, default=str))
