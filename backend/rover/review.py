"""Fleet review — the human-in-the-loop layer that delivers verified 100% accuracy.

No LLM, no fitz. Pure stdlib. Turns flagged extractions into a compact review
list (the uncertain fields + their evidence + candidate values), lets a human's
confirmed corrections be applied back, and marks a document reviewed. Everything
reads/writes through ``fleet.store``.

A stored document carries:
  record        {col: cell_dict}  — the Cell evidence contract per column
  values        {col: value}      — the flat header values
  needs_review  bool              — did any gate flag the doc
  suspect       [col, ...]        — columns the pipeline is unsure about
  items         [ {...}, ... ]    — line items

Every public function is fail-safe: it degrades to an empty/None result rather
than raising into a caller.
"""
import os
import csv

from . import store

# Cell statuses that pull a column into the review list even if it never made
# the doc-level ``suspect`` list.
_REVIEW_STATUSES = {"suspect", "review"}


# --------------------------------------------------------------------------- #
# review records
# --------------------------------------------------------------------------- #
def review_item(result: dict) -> dict:
    """Build one compact review record for a single stored document.

    Shape::

        {"doc_id", "pdf", "needs_review",
         "fields": [ {"column", "value", "alternates", "evidence",
                      "confidence", "note", "reason"}, ... ]}

    ``fields`` has one entry per flagged column: every column in
    ``result["suspect"]`` plus any cell whose status is "suspect"/"review".
    A clean (un-flagged) document yields an empty ``fields`` list.
    """
    try:
        result = result or {}
        record = result.get("record") or {}
        values = result.get("values") or {}
        suspect = list(result.get("suspect") or [])

        # union of doc-level suspects and any cell self-flagged for review,
        # preserving suspect order then appending late cell-flagged columns.
        flagged = list(suspect)
        for col, cell in record.items():
            if not isinstance(cell, dict):
                continue
            if cell.get("status") in _REVIEW_STATUSES and col not in flagged:
                flagged.append(col)

        fields = []
        for col in flagged:
            cell = record.get(col) or {}
            if not isinstance(cell, dict):
                cell = {}
            value = cell.get("value", values.get(col))
            note = cell.get("note") or ""
            fields.append({
                "column": col,
                "value": value,
                "alternates": cell.get("alternates") or [],
                "evidence": cell.get("source") or "",
                "confidence": cell.get("confidence", 0.0),
                "note": note,
                "reason": note or "flagged for review",
            })

        return {
            "doc_id": result.get("_stored_id") or store._doc_id(result),
            "pdf": result.get("pdf"),
            "needs_review": bool(result.get("needs_review")),
            "fields": fields,
        }
    except Exception:
        return {
            "doc_id": "",
            "pdf": None,
            "needs_review": False,
            "fields": [],
        }


def review_queue() -> list:
    """Every flagged stored document as a review record, sorted by doc_id.

    Clean documents (``needs_review`` falsy) are skipped entirely.
    """
    try:
        queue = []
        for doc in store.all_documents():
            if not doc.get("needs_review"):
                continue
            queue.append(review_item(doc))
        return sorted(queue, key=lambda r: r.get("doc_id") or "")
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# applying human corrections
# --------------------------------------------------------------------------- #
def apply_corrections(doc_id: str, corrections: dict):
    """Apply a human's confirmed corrections back to a stored document.

    ``corrections`` is ``{column: confirmed_value}``. For each column present:
    set ``values[column]``, and if the column has a cell in ``record`` mark it
    confirmed (value/status/model + a "human-confirmed" note). Confirmed
    columns are removed from ``suspect``; ``needs_review`` is recomputed from
    what's left. The doc is flagged ``reviewed`` with the applied corrections
    and saved. Returns the updated doc, or None if it's missing/on any error.
    """
    try:
        doc = store.load_document(doc_id)
        if doc is None:
            return None

        corrections = corrections or {}
        values = doc.setdefault("values", {})
        record = doc.get("record") or {}

        for column, confirmed in corrections.items():
            values[column] = confirmed
            cell = record.get(column)
            if isinstance(cell, dict):
                cell["value"] = confirmed
                cell["status"] = "confirmed"
                cell["model"] = "human"
                note = cell.get("note") or ""
                cell["note"] = (note + " | human-confirmed").strip(" |") \
                    if note else "human-confirmed"

        # drop every confirmed column from the suspect list
        suspect = [c for c in (doc.get("suspect") or [])
                   if c not in corrections]
        doc["suspect"] = suspect
        doc["needs_review"] = len(suspect) > 0
        doc["reviewed"] = True
        doc["review_corrections"] = corrections

        store.save_document(doc)
        return doc
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# CSV export (offline review sheet)
# --------------------------------------------------------------------------- #
def export_review_csv(path: str) -> int:
    """Write the review queue to a CSV the human fills in, return row count.

    One row per flagged field across every queued document. The trailing
    ``confirmed_value`` column is left blank — that's where the reviewer types
    the correct value before the sheet is fed back in.
    """
    header = [
        "doc_id", "pdf", "field", "current_value",
        "alternates", "confidence", "reason", "confirmed_value",
    ]
    rows = 0
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            for rec in review_queue():
                for fld in rec.get("fields") or []:
                    alternates = fld.get("alternates") or []
                    writer.writerow([
                        rec.get("doc_id") or "",
                        rec.get("pdf") or "",
                        fld.get("column") or "",
                        fld.get("value"),
                        "; ".join(str(a) for a in alternates),
                        fld.get("confidence", 0.0),
                        fld.get("reason") or "",
                        "",  # confirmed_value — human fills this
                    ])
                    rows += 1
        return rows
    except Exception:
        return rows


# --------------------------------------------------------------------------- #
# summary
# --------------------------------------------------------------------------- #
def stats() -> dict:
    """Roll up the store: totals, clean vs flagged docs, flagged field count."""
    try:
        docs = store.all_documents()
        total = len(docs)
        flagged = 0
        flagged_fields = 0
        for doc in docs:
            if doc.get("needs_review"):
                flagged += 1
                flagged_fields += len(review_item(doc).get("fields") or [])
        return {
            "total": total,
            "clean": total - flagged,
            "flagged": flagged,
            "flagged_fields": flagged_fields,
        }
    except Exception:
        return {"total": 0, "clean": 0, "flagged": 0, "flagged_fields": 0}
