"""Where each declaration figure came from, in the reviewer's words.

The pipeline already records this. `_field_engine` is written by the typed lane,
the text-layer read, the item-sum corroboration and the CIF derivation, and it is
then printed to the worker log and thrown away. A reviewer looking at a customs
total has no way to tell whether it was read off the page, agreed on by two
independent sources, or worked out with arithmetic — and those deserve very
different amounts of trust.

`build_evidence` turns that record into the shape `routes/review._evidence_safe`
already serves: `{field: {value, source, status, note}}`, keyed by DB column.

Two rules this module keeps:

  * **No invented confidence.** There is no honest number to put on "a vision
    model read this once". A status word the reviewer can act on beats a
    percentage that looks like a measurement and is not one.
  * **Absent is absent.** A field with no provenance gets no entry, not a
    reassuring default. The UI is documented to read absence as "no evidence",
    never as "nothing wrong".

★ The two key spaces. `_field_engine` is keyed the way the WRITER spelled the
field, and the writers disagree: the typed lane emits `customs_duty`, the
deterministic rescue emits `import_export_customs_duty`. `_save_to_db` resolves
that with `_pick(decl, "<db name>", "<engine name>", ...)`. `_ALIASES` below
mirrors those groups so evidence lands under the DB spelling the UI asks for.
`tests/test_provenance.py` reads the real whitelist and fails when the two drift
— the same mistake once dropped a verified tax read on the floor.
"""
from typing import Any, Dict, List, Optional

# `trust` — how the value came to be, which is what the reviewer reasons about.
# corroborated : two independent sources agreed. Trust unless the doc disagrees.
# read         : read once from the document. Normal. Spot-check.
# derived      : arithmetic, not read. Correct only if its inputs are.
CORROBORATED = "corroborated"
READ = "read"
DERIVED = "derived"

# `status` — the vocabulary `routes/evidence.py` already queues on. Kept separate
# from `trust` on purpose: one says how the value was obtained, the other says
# whether a human still has to act. A vision read of a scanned page is `read` and
# also `review`; a text-layer read is `read` and `ok`.
_NEEDS_HUMAN = "review"
_OK = "ok"

# Which fields earn a place in the queue when they were read once off a
# photograph, or calculated rather than read.
#
# Not every field. On a scanned declaration almost everything is a single vision
# read, and flagging all twenty rows makes the queue unreadable and therefore
# unused. These are the rows with legal or financial consequence — the ones the
# UAT errors actually landed on — so the queue stays short enough to work.
_HIGH_STAKES = {
    "declaration_no", "declaration_date", "exchange_rate", "invoice_number",
    "invoice_price_fc", "invoice_price_mmk", "total_customs_value",
    "freight_value", "insurance_value", "adjustment_value",
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf", "exemption_reduction",
}

# Writers whose read is coordinate-anchored on the page. `routes/evidence._located`
# reports `exact` only for `model == "geometry"`, and only these two earned it:
# both find the value by its printed label and take it from the text layer at that
# position, so they cannot pick up the neighbouring row.
_GEOMETRY_WRITERS = {"textlayer", "cusdec_text"}

# Writers whose value was not read off the page at all, or was read once off a
# photograph with nothing to check it against.
_UNCHECKED_WRITERS = {"vision_cusdec", "derived_cif", "v7"}

# How each writer describes itself. `source` is shown to the reviewer, so it
# names the DOCUMENT the value came off, not the module that read it.
_SOURCES = {
    "presto": (READ, "typed text layer of the declaration page",
               "Read from the PDF's own text, not an image. Exact when present."),
    "v7": (READ, "whole-document vision read",
           "A model read the rendered pages. Single characters can be misread; "
           "digits inside long numbers are the usual place."),
    "textlayer": (READ, "label-anchored read of the declaration page",
                  "Located by its printed label and taken from the text layer, "
                  "so it cannot be confused with a neighbouring row."),
    "cusdec_text": (READ, "CUSDEC text layer — the legal source",
                    "Taken from the customs declaration itself, which overrides "
                    "the licence and invoice pages in the same bundle."),
    "vision_cusdec": (READ, "single vision read of the scanned CUSDEC page",
                      "The declaration page is a photograph, so there is no text "
                      "to check against. Worth confirming by eye."),
    "item_sum_corroborated": (CORROBORATED, "declared total, agreed by the item rows",
                              "The printed total and the sum of the item lines "
                              "match, so two independent readings agree."),
    "derived_cif": (DERIVED, "calculated from the CIF build-up",
                    "Not read off the form. Worked back from invoice, freight, "
                    "insurance and the exchange rate, so it is only as good as "
                    "those four."),
}

# Mirrors the `_pick(decl, "<db>", "<engine>", ...)` groups in
# `workflow._save_to_db`. DB name first, exactly as there.
_ALIASES: Dict[str, List[str]] = {
    "declaration_no": ["Declaration No"],
    "declaration_date": ["Declaration Date"],
    "arrival_date": ["Arrival Date"],
    "release_order_date": ["Release Order Date"],
    "completion_date": ["Completion Date"],
    "importer_name": ["Importer (Name)"],
    "consignor_name": ["Consignor (Name)"],
    "invoice_number": ["Invoice Number"],
    "currency": ["Currency"],
    "currency_2": ["Currency 2"],
    "exchange_rate": ["Exchange Rate"],
    "invoice_price": ["Invoice Price"],
    "invoice_price_fc": ["Invoice Price (FC)", "invoice_price", "Invoice Price"],
    "invoice_price_mmk": ["Invoice Price (MMK)"],
    "total_customs_value": ["Total Customs Value"],
    "freight_value": ["Freight"],
    "insurance_value": ["Insurance"],
    "adjustment_value": ["Adjustment"],
    "import_export_customs_duty": ["customs_duty", "Import/Export Customs Duty"],
    "commercial_tax_ct": ["commercial_tax", "Commercial Tax (CT)"],
    "advance_income_tax_at": ["advance_income_tax", "Advance Income Tax (AT)"],
    "security_fee_sf": ["security_fee", "Security Fee (SF)"],
    "maccs_service_fee_mf": ["maccs_service_fee", "MACCS Service Fee (MF)"],
    "exemption_reduction": ["exemption", "Exemption/Reduction"],
}

# Gate outcomes that change how much a specific field can be trusted. Appended to
# the note rather than replacing it: the flag is extra information about a value,
# not a different account of where it came from.
_FLAG_NOTES = {
    "exchange_rate_suspect": ("exchange_rate",
                              "The CIF arithmetic does not close at this rate."),
    "vision_rescue_empty": (None,
                            "The scanned declaration could not be read at all; "
                            "header fields may be blank or from another page."),
    "vision_rescue_retried": (None,
                              "The first read of the scanned page returned "
                              "nothing and it was read again."),
    "total_from_item_sum": ("total_customs_value",
                            "The printed total was replaced by the sum of the "
                            "item rows."),
    "adjustment_derived": ("adjustment_value",
                           "Calculated to close the CIF identity, not read."),
    "neighbour_value_rejected": (None,
                                 "A value equal to the row beside it was "
                                 "rejected as a mis-read label."),
    "taxes_missing": (None, "The declaration has a total but no tax block."),
}


def _lookup(field_engine: Dict[str, str], db_field: str) -> Optional[str]:
    """The writer that supplied `db_field`, under whichever name it used."""
    for key in [db_field] + _ALIASES.get(db_field, []):
        src = field_engine.get(key)
        if src:
            return src
    return None


def build_evidence(db_declaration: Dict[str, Any],
                   field_engine: Optional[Dict[str, str]] = None,
                   flags: Optional[List[str]] = None,
                   bboxes: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Dict[str, Any]]:
    """One evidence cell per field, in the shape `routes/evidence.py` consumes.

    `{db_field: {value, source, status, trust, note, writer, model, page, bbox}}`.
    Never raises.

    `bboxes` is `field_bboxes` — `{"declaration": {field: {page,x,y,w,h}}, ...}`.
    Passing it lets the Checks screen crop the exact patch of paper the value was
    read from. Absent, the crop endpoint falls back to the whole page, which is
    the documented behaviour: a reviewer shown a full page understands "we could
    not pinpoint this"; a reviewer shown a tight box around the wrong number
    does not.

    Only fields that actually carry a value get an entry — a note attached to a
    blank would read as an account of a reading that never happened.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        field_engine = field_engine or {}
        flags = list(flags or [])
        decl_boxes = ((bboxes or {}).get("declaration") or {})

        for db_field, value in (db_declaration or {}).items():
            if db_field.startswith("_") or value in (None, ""):
                continue
            writer = _lookup(field_engine, db_field)
            if writer is None:
                continue
            trust, source, note = _SOURCES.get(
                writer, (READ, writer, "Recorded by the pipeline under this name."))
            cell: Dict[str, Any] = {
                "value": value, "source": source, "trust": trust,
                "note": note, "writer": writer,
                # `geometry` is what marks a box as measured rather than guessed.
                # Claiming it for a writer that did not measure would make
                # `_located` report `exact` about a coordinate nobody checked.
                "model": "geometry" if writer in _GEOMETRY_WRITERS else writer,
                # A single unchecked reading of a figure that carries money or
                # identity is worth a human glance. Everything else is `ok` and
                # keeps its note, so the account is still there when asked for.
                "status": (_NEEDS_HUMAN
                           if writer in _UNCHECKED_WRITERS and db_field in _HIGH_STAKES
                           else _OK),
            }
            box = decl_boxes.get(db_field)
            if isinstance(box, dict) and box.get("page"):
                cell["page"] = box["page"]
                cell["bbox"] = box
            out[db_field] = cell

        # Gate outcomes. A flag naming a field annotates it; a document-wide flag
        # annotates every entry, because it is a statement about the read as a
        # whole and a reviewer scanning one row should still see it.
        for flag in flags:
            target, extra = _FLAG_NOTES.get(flag, (None, None))
            if extra is None:
                continue
            targets = [target] if target else list(out)
            for name in targets:
                if name in out:
                    out[name]["note"] = f"{out[name]['note']} {extra}".strip()
                    out[name].setdefault("flags", []).append(flag)
            # A gate that names a field is the strongest signal there is: an
            # arithmetic check ran and disagreed. That outranks how the value was
            # obtained, so it always reaches the queue — a text-layer read of an
            # exchange rate the CIF identity refuses is still wrong.
            if target and target in out:
                out[target]["status"] = "suspect"
    except Exception:
        return out
    return out
