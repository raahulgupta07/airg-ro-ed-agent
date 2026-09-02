"""Read header fields straight from the PDF's text layer, by coordinates.

Where a page carries embedded text, the characters ARE the answer. Asking a model
to pick which label a number belongs to adds a guess to something that is already
unambiguous, and the guesses have been wrong in ways no amount of prompt wording
fixed:

  * `declaration_no` came back as the "First approval declaration No." — a
    different, earlier declaration printed lower on the same page. The instruction
    to use only the top-of-form number was verified inside the running container,
    and the model returned the other number anyway, with and without example values
    in the prompt.
  * `adjustment_value` came back as `2` — the classification code printed beside
    the word "Adjustment", 33 points above the "Adjustment value" row that holds
    the money.

Both sit at a known label on a readable page. This module finds the label's box
and takes the value beside it, so those fields stop depending on a model's choice.

Why coordinates rather than the flattened text: `get_text()` emits these forms in
an order that interleaves labels and values from different cells — on a real
document "Total customs value" lands eight lines from its own number with three
other labels in between. And `get_text("words")` is not in visual order either, so
a label is matched by POSITION: anchor the first token, then require each following
token on the same baseline a short hop to the right.

Returns only what it actually read. A scanned page yields {} and the model's
reading stands.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import fitz

_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_DATE = re.compile(r"^\d{4}/\d{2}/\d{2}$")
_DASH = ("-", "–", "—")

# label -> (field, kind, max_dx, stop_words)
# stop_words guard against a label that is the PREFIX of a longer one: "Invoice"
# also starts "Invoice price", and "Adjustment" also starts "Adjustment value".
_SPEC = (
    ("Declaration No.", "declaration_no", "num", 300, ()),
    ("Declaration date", "declaration_date", "date", 300, ()),
    ("Arrival date", "arrival_date", "date", 300, ()),
    ("Release order", "release_order_date", "date", 300, ("notification",)),
    ("Declaration completion", "completion_date", "date", 300, ()),
    ("Total customs value", "total_customs_value", "num", 300, ()),
    ("Adjustment value", "adjustment_value", "num", 300, ()),
    ("Exchange Rate", "exchange_rate", "num", 300, ()),
    # The tax table. Added after Security Fee and MACCS Service Fee came back
    # transposed on two documents — 20,000 and 30,000 each stored under the other's
    # name, on pages that carry a full text layer. The flattened text is no help
    # (`MACCS SERVICE FEE 20,000 * MF * 30,000 SECURITY FEE SF` is one real example
    # of the order `get_text` returns), but the rows are unambiguous by position.
    # One of the documents genuinely has MF = 30,000 while another has MF = 20,000,
    # so this cannot be settled by convention — only by reading the row.
    ("IMPORT/EXPORT CUSTOMS DUTY", "import_export_customs_duty", "num", 260, ()),
    ("COMMERCIAL TAX", "commercial_tax_ct", "num", 260, ()),
    ("ADVANCED INCOME TAX", "advance_income_tax_at", "num", 260, ()),
    ("SECURITY FEE", "security_fee_sf", "num", 260, ()),
    ("MACCS SERVICE FEE", "maccs_service_fee_mf", "num", 260, ()),
)


def _find_label(words, label: str, stop_words=()):
    parts = label.split()
    for w0 in (w for w in words if w[4] == parts[0]):
        chain = [w0]
        for part in parts[1:]:
            prev = chain[-1]
            mid = (prev[1] + prev[3]) / 2
            nxt = [w for w in words
                   if w[4] == part and abs((w[1] + w[3]) / 2 - mid) <= 3
                   and 0 <= w[0] - prev[2] <= 14]
            if not nxt:
                break
            chain.append(min(nxt, key=lambda w: w[0] - prev[2]))
        else:
            box = (min(w[0] for w in chain), min(w[1] for w in chain),
                   max(w[2] for w in chain), max(w[3] for w in chain))
            if stop_words:
                mid = (box[1] + box[3]) / 2
                after = [w for w in words
                         if abs((w[1] + w[3]) / 2 - mid) <= 3 and 0 <= w[0] - box[2] <= 14]
                if after and min(after, key=lambda w: w[0] - box[2])[4] in stop_words:
                    continue          # this occurrence is a LONGER label
            return box
    return None


def _value(page, label: str, kind: str, max_dx: float, stop_words=()) -> Optional[str]:
    words = page.get_text("words")
    if not words:
        return None
    box = _find_label(words, label, stop_words)
    if not box:
        return None
    x0, y0, x1, y1 = box
    rx = _DATE if kind == "date" else _NUM
    mid = (y0 + y1) / 2

    row = sorted((w for w in words
                  if w[0] >= x1 - 1 and abs((w[1] + w[3]) / 2 - mid) <= 4
                  and w[0] - x1 <= max_dx), key=lambda w: w[0])
    # A dash in the first cell means the field is blank; a number further right
    # belongs to a neighbouring column. This is how "Adjustment value" once picked
    # up the total customs value.
    if row and row[0][4] in _DASH and kind == "num":
        return None
    hit = [w for w in row if rx.match(w[4])]
    if hit:
        return hit[0][4]

    # Some labels sit ABOVE their value — the boxed "Declaration No." header.
    below = [w for w in words
             if rx.match(w[4]) and 0 < w[1] - y1 <= 18 and abs(w[0] - x0) <= 130]
    if below:
        return min(below, key=lambda w: (w[1] - y1, abs(w[0] - x0)))[4]
    return None


def _coerce(field: str, raw: str):
    """Form text -> a value the DB column accepts.

    The form prints "198,450,000" and "2,100"; the columns are numeric and Postgres
    rejects a comma outright. One bad value aborts the whole INSERT, the declaration
    is lost, and the job still reports success — which is exactly what happened the
    first time this module returned raw form text. Dates are normalised to the
    ISO form the date columns already hold.
    """
    if raw is None:
        return None
    t = str(raw).strip()
    if field.endswith("_date"):
        return t.replace("/", "-")
    if field == "declaration_no":
        return re.sub(r"[^0-9]", "", t) or None
    try:
        return float(t.replace(",", ""))
    except ValueError:
        return None


def read(pdf_path: str, pages: Optional[List[int]] = None) -> Dict[str, object]:
    """Header fields recoverable from the text layer. {} when there is none."""
    out: Dict[str, str] = {}
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return out
    try:
        idxs = [p - 1 for p in (pages or range(1, doc.page_count + 1))
                if 1 <= p <= doc.page_count]
        for label, field, kind, dx, stop in _SPEC:
            for i in idxs:
                try:
                    v = _value(doc[i], label, kind, dx, stop)
                except Exception:
                    v = None
                if v is not None:
                    cv = _coerce(field, v)
                    if cv is not None:
                        out[field] = cv
                    break
    finally:
        doc.close()
    return out
