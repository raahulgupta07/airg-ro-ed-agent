"""Locate a labelled value on a customs form, or refuse to answer.

One mistake was made six times in this codebase, in six different readers, by two
different authors. Every occurrence has the same shape: **search the page for a
label, take the nearest number, never verify that the label found is the right
one.** The six, all reproduced on real Myanmar customs declarations:

  1. `\\b(\\d{12})\\b` — the first 12-digit number anywhere on the page — as the
     declaration number. An Ex-bond release carries TWO: its own `Declaration No.`
     boxed at the top right, and the `First approval declaration No.` of the
     earlier bonded entry it clears, printed mid-page. It took the wrong one.
  2. The label `Adjustment` matched and the row beside it read. `Adjustment` holds
     a small classification CODE (`2`); `Adjustment value`, a different row 33.4
     points lower, holds the money (`44,612.82`). The short label is a PREFIX of
     the long one.
  3. `Invoice` matched inside `Invoice price`.
  4. The two-letter tax code `CT` matched a `CT` printed as the package UNIT
     ("203 CT") and as a fragment in the Burmese Notes1 line, and a loose
     below-the-label search then returned the net weight `1,948.800` instead of
     the commercial tax `6,061,781`. Both numbers are on the page — presence is
     not belonging.
  5. `Adjustment value` on a form where that row is genuinely blank (`- -`)
     scanned right, past the dash, and returned `32,445,915.66` — the total
     customs value, from the row below and a different column.
  6. `SF` was looked for on a form that prints the word `Security`. Nothing was
     found and the field was reported missing, on a page that plainly shows `0`.

This module is the one place that lookup happens, and it is correct by
construction about all six:

  * A label is matched BY POSITION, never by list order. `page.get_text("words")`
    is not in visual order on these forms — page 1 of a real bundle begins
    "Declarant reference No. 00053 Serial No. for users Notes2 Notes1". The first
    token is anchored, then each following token must sit on the same baseline a
    short hop to the right.
  * An occurrence contained inside a LONGER label is discarded. Longer forms are
    derived automatically from the spec registry (`Adjustment` is thrown out on
    the `Adjustment value` row because `Adjustment value` is also registered) and
    may additionally be stated by the caller. Containment catches prefixes
    (`Adjustment` in `Adjustment value`), suffixes (`Declaration No.` in `First
    approval declaration No.`) and infixes alike.
  * Every surviving occurrence is read, and the ANSWERS are compared. One
    distinct answer is returned; two or more is `AMBIGUOUS` and returns nothing.
    Refusing is the correct output — a guess is what produced every bug above.
  * A dash in the first cell after the label means the form says empty. That is
    `BLANK`, a different outcome from "I could not tell" (#5).
  * A value may sit at most `max_dx` to the right, because these forms have two
    columns sharing y coordinates: the tax block at x~63 and Exchange Rate at
    x~361 are on the same rows, and `Taxes and fees 20,172,675` sits on the CD
    row 468 points from the `CD` label.
  * A value directly BELOW the label is read too — the boxed `Declaration No.`
    header prints its number underneath — but only when it overlaps the label's
    own x-span. That single constraint is what kills #4 and #5: the net weight is
    45 points left of the `CT` unit marker, and the total customs value is 65
    points right of the `Adjustment value` label.
  * Synonyms are tried in order until one resolves UNIQUELY, which is how `SF` /
    `SECURITY FEE` / `Security` all reach the same field, and how the ambiguous
    `CT` steps aside for the unambiguous `COMMERCIAL TAX`.

Every reading carries provenance: which synonym matched, which occurrence, the
label and value rectangles, and — when nothing came back — the reason.

    import fitz, formread
    page = fitz.open(path)[0]
    r = formread.read(page, formread.CUSDEC["declaration_no"])
    r.ok, r.value, r.reason
    # True, 100319576711, 'label occurrence 1 of 1; value below label'

Run this file directly to read the three reference documents in
`~/Desktop/pgro` and print what it finds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:  # the repo's single numeric parser, when the backend is importable
    from numeric import to_float as _repo_to_float
except Exception:  # pragma: no cover - standalone use
    _repo_to_float = None


# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------

class Outcome:
    """Why a lookup ended the way it did. `BLANK` and `AMBIGUOUS` are the two
    that used to be silently collapsed into "None", which is how a form that
    says `0` was reported missing and a form that says nothing was reported as
    the neighbouring column's total."""

    FOUND = "found"            # exactly one answer, and here it is
    BLANK = "blank"            # the form prints a dash: this field IS empty
    AMBIGUOUS = "ambiguous"    # the label resolves >1 way — deliberately no answer
    NO_VALUE = "no_value"      # label located, nothing readable within range
    NOT_FOUND = "not_found"    # no occurrence of any synonym on this page
    NO_TEXT = "no_text"        # the page is a scan; there is no text layer


Rect = Tuple[float, float, float, float]


@dataclass(frozen=True)
class Candidate:
    """One occurrence of a label and what it says, kept even when rejected."""

    label: str
    raw: Optional[str]
    value: object
    outcome: str
    label_rect: Rect
    value_rect: Optional[Rect]
    page_number: Optional[int]
    how: str                      # 'right' | 'below' | ''

    def __str__(self) -> str:
        x0, y0, x1, y1 = (round(v, 2) for v in self.label_rect)
        shown = "<blank>" if self.outcome == Outcome.BLANK else self.raw
        return (f"{self.label!r}@({x0},{y0},{x1},{y1})"
                f"{f' p{self.page_number}' if self.page_number else ''}"
                f" -> {shown!r}{f' ({self.how})' if self.how else ''}")


@dataclass(frozen=True)
class Reading:
    """A value plus everything needed to argue about it later."""

    field: str
    outcome: str
    value: object = None
    raw: Optional[str] = None
    label: Optional[str] = None
    label_rect: Optional[Rect] = None
    value_rect: Optional[Rect] = None
    page_number: Optional[int] = None
    how: str = ""
    reason: str = ""
    candidates: Tuple[Candidate, ...] = ()

    @property
    def ok(self) -> bool:
        """True only for a real value. A BLANK field is not a value — callers
        that store `0` for blank recreate the bug where "could not read" and
        "the form says zero" became indistinguishable."""
        return self.outcome == Outcome.FOUND

    @property
    def certain(self) -> bool:
        """The form was read successfully, whether it holds a value or a dash."""
        return self.outcome in (Outcome.FOUND, Outcome.BLANK)

    def __bool__(self) -> bool:
        return self.ok

    def describe(self) -> str:
        head = f"{self.field}={self.value!r}" if self.ok else f"{self.field}: {self.outcome}"
        return f"{head}  [{self.reason}]"


# --------------------------------------------------------------------------
# what a field looks like on the page
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Spec:
    """How to find one field.

    labels    synonyms, tried IN ORDER; the first that resolves uniquely wins.
              `("SF", "SECURITY FEE", "Security")` — one form prints the code,
              another prints the word.
    kind      number | date | text — what a value token has to look like.
    max_dx    how far right of the label a value may sit. The default 260 is
              measured: on a real CUSDEC the `CD` tax amount is 237 points from
              its code, and the neighbouring column's `Taxes and fees` total is
              468 — so 260 admits the first and excludes the second.
    max_dy    how far below the label a value may sit (~1.5 rows).
    allow_below   read the cell underneath as well as the one beside.
    avoid     longer labels this one must not be part of. Registered specs
              contribute these automatically; state extras here for decoys that
              are not themselves fields.
    """

    field: str
    labels: Tuple[str, ...]
    kind: str = "number"
    max_dx: float = 260.0
    max_dy: float = 18.0
    allow_below: bool = True
    avoid: Tuple[str, ...] = ()
    pattern: Optional[str] = None
    row_tol: float = 4.0          # baseline tolerance for "same row"
    token_gap: float = 14.0       # max gap between words of ONE label
    below_pad: float = 6.0        # x-overlap slack for the cell underneath
    case_sensitive: bool = False
    note: str = ""

    def __post_init__(self):
        if isinstance(self.labels, str):        # a lone label is a common slip
            object.__setattr__(self, "labels", (self.labels,))


# --------------------------------------------------------------------------
# token predicates
# --------------------------------------------------------------------------

# En dash, em dash, figure dash, minus sign — these forms use several.
_DASHES = frozenset("-‐‑‒–—―−")

# A number as these forms print it: 198,450,000  44,612.82  58.3322  0
# Deliberately NOT matching "(1)", "2%", "3R" or a bare dash.
_NUM = re.compile(r"^-?(?=[\d,]*\d)[\d,]+(?:\.\d+)?$")
# yyyy/mm/dd and dd/mm/yyyy, either separator.
_DATE = re.compile(r"^(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4})$")
# An identifier: invoice numbers, references. Long enough not to catch the
# single-letter cell codes ("A", "M", "C") that litter the MACCS layout.
_TEXT = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9/\-_.]{3,}$")


def _is_dash(tok: str) -> bool:
    s = (tok or "").strip()
    return bool(s) and all(ch in _DASHES for ch in s)


def _matcher(spec: Spec):
    if spec.pattern:
        rx = re.compile(spec.pattern)
    elif spec.kind == "date":
        rx = _DATE
    elif spec.kind == "text":
        rx = _TEXT
    else:
        rx = _NUM
    return lambda tok: bool(rx.match(tok))


def _to_number(raw: str):
    """Form text -> a number. `198,450,000` is an int; `44,612.82` is a float.

    Keeping integers integral matters downstream: a declaration number is an id,
    not a quantity, and `1.00319576711e+11` in a log is unreadable. Commas are
    stripped because Postgres rejects them outright and one bad value aborts the
    whole INSERT.
    """
    t = (raw or "").strip()
    if _repo_to_float is not None:
        f = _repo_to_float(t)
        if f is None:
            return None
    else:
        try:
            f = float(t.replace(",", ""))
        except ValueError:
            return None
    if "." not in t and float(f).is_integer():
        return int(f)
    return f


def _parse(spec: Spec, raw: str):
    if spec.kind == "number":
        return _to_number(raw)
    if spec.kind == "date":
        return raw.replace("/", "-")
    return raw


# --------------------------------------------------------------------------
# finding a label by position
# --------------------------------------------------------------------------

def _norm(tok: str) -> str:
    return tok.strip().rstrip(".:").lower()


def _words(page) -> List[tuple]:
    try:
        w = page.get_text("words")
    except Exception:
        return []
    return list(w or [])


def _mid(w) -> float:
    return (w[1] + w[3]) / 2.0


def _locate(words, label: str, spec: Spec) -> List[Rect]:
    """Every place `label` is printed, as a rectangle, matched BY POSITION.

    `get_text("words")` does not return words in visual order on these forms, so
    matching consecutive list indices finds the label somewhere else or not at
    all. Anchor the first token, then require each following token on the same
    baseline within `token_gap` to the right.
    """
    parts = label.split()
    if not parts:
        return []
    key = (lambda t: t) if spec.case_sensitive else _norm
    want = [key(p) for p in parts]

    out: List[Rect] = []
    for anchor in words:
        if key(anchor[4]) != want[0]:
            continue
        chain = [anchor]
        for part in want[1:]:
            prev = chain[-1]
            mid = _mid(prev)
            nxt = [w for w in words
                   if key(w[4]) == part
                   and abs(_mid(w) - mid) <= spec.row_tol - 1
                   and -1.5 <= w[0] - prev[2] <= spec.token_gap]
            if not nxt:
                break
            chain.append(min(nxt, key=lambda w: w[0] - prev[2]))
        else:
            rect = (min(w[0] for w in chain), min(w[1] for w in chain),
                    max(w[2] for w in chain), max(w[3] for w in chain))
            if rect not in out:
                out.append(rect)
    out.sort(key=lambda r: (r[1], r[0]))
    return out


def _contained(inner: Rect, outer: Rect) -> bool:
    """`inner` is a strict sub-span of `outer` on the same row.

    This is the whole prefix/suffix defence. `Declaration No.` at x 336-390 sits
    inside `First approval declaration No.` at x 285-390, and `Adjustment` at
    295-337 sits inside `Adjustment value` at 295-359 — one is a suffix of the
    longer label, the other a prefix, and containment catches both without
    caring which.
    """
    if inner == outer:
        return False
    same_row = min(inner[3], outer[3]) - max(inner[1], outer[1]) > 0
    return (same_row
            and inner[0] >= outer[0] - 1.0
            and inner[2] <= outer[2] + 1.0)


# --------------------------------------------------------------------------
# reading the value beside (or under) one occurrence
# --------------------------------------------------------------------------

def _value_at(words, rect: Rect, spec: Spec) -> Tuple[str, Optional[str], Optional[Rect], str]:
    """(outcome, raw, value_rect, how) for a single label occurrence."""
    ok = _matcher(spec)
    mid = (rect[1] + rect[3]) / 2.0

    # --- the cell beside it -------------------------------------------------
    right = sorted((w for w in words
                    if w[0] >= rect[2] - 1.0
                    and abs(_mid(w) - mid) <= spec.row_tol
                    and w[0] - rect[2] <= spec.max_dx),
                   key=lambda w: w[0])
    if right and _is_dash(right[0][4]):
        # A dash in the FIRST cell is the form saying "empty". Anything numeric
        # further right belongs to a neighbouring column: this is exactly how a
        # blank `Adjustment value  -  -` returned the total customs value.
        # Note the test is the first TOKEN, not "a dash anywhere" — the filled
        # row reads `Adjustment value AD - USD - 44,612.82`, where the dashes
        # are cell separators sitting between the code, the currency and the
        # money.
        return Outcome.BLANK, None, (right[0][0], right[0][1], right[0][2], right[0][3]), "right"
    for w in right:
        if ok(w[4]):
            return Outcome.FOUND, w[4], (w[0], w[1], w[2], w[3]), "right"

    # --- the cell underneath ------------------------------------------------
    if spec.allow_below:
        pad = spec.below_pad
        below = sorted((w for w in words
                        if w[1] > rect[3] - 1.0
                        and w[1] - rect[3] <= spec.max_dy
                        # must share the label's own column, not merely be near
                        and w[2] > rect[0] - pad and w[0] < rect[2] + pad),
                       key=lambda w: (w[1], w[0]))
        if below and _is_dash(below[0][4]):
            b = below[0]
            return Outcome.BLANK, None, (b[0], b[1], b[2], b[3]), "below"
        for w in below:
            if ok(w[4]):
                return Outcome.FOUND, w[4], (w[0], w[1], w[2], w[3]), "below"

    # Nothing matched, but the row carried a dash somewhere: the form is empty
    # here rather than unreadable. `Freight  -  -` is a blank freight, not a
    # failure to read one.
    if any(_is_dash(w[4]) for w in right):
        return Outcome.BLANK, None, None, "right"
    return Outcome.NO_VALUE, None, None, ""


# --------------------------------------------------------------------------
# the public read
# --------------------------------------------------------------------------

def _answer_key(cand: Candidate):
    return "<blank>" if cand.outcome == Outcome.BLANK else cand.value


def _read_one_label(page, words, spec: Spec, label: str,
                    avoid_rects: Sequence[Rect], page_number: Optional[int]
                    ) -> Tuple[str, List[Candidate], str]:
    """(outcome, candidates, reason) for a single synonym on a single page."""
    rects = _locate(words, label, spec)
    if not rects:
        return Outcome.NOT_FOUND, [], f"label {label!r} not printed on the page"

    kept, swallowed = [], 0
    for r in rects:
        if any(_contained(r, a) for a in avoid_rects):
            swallowed += 1
            continue
        kept.append(r)
    if not kept:
        return (Outcome.NOT_FOUND, [],
                f"label {label!r} appears {swallowed}x but every occurrence is "
                f"part of a longer label")

    cands: List[Candidate] = []
    for r in kept:
        outcome, raw, vrect, how = _value_at(words, r, spec)
        cands.append(Candidate(
            label=label, raw=raw,
            value=_parse(spec, raw) if raw is not None else None,
            outcome=outcome, label_rect=r, value_rect=vrect,
            page_number=page_number, how=how))

    answered = [c for c in cands if c.outcome in (Outcome.FOUND, Outcome.BLANK)]
    distinct = {_answer_key(c) for c in answered}
    swallow_note = f", {swallowed} discarded as part of a longer label" if swallowed else ""

    if not answered:
        return (Outcome.NO_VALUE, cands,
                f"label {label!r} found {len(kept)}x but no value within "
                f"{spec.max_dx:g}pt right or {spec.max_dy:g}pt below{swallow_note}")
    if len(distinct) > 1:
        shown = ", ".join(sorted(str(d) for d in distinct))
        return (Outcome.AMBIGUOUS, cands,
                f"label {label!r} resolves {len(distinct)} different ways ({shown}) "
                f"— refusing to guess{swallow_note}")

    winner = answered[0]
    where = "value below label" if winner.how == "below" else "value beside label"
    idx = kept.index(winner.label_rect) + 1
    return (winner.outcome, cands,
            f"label {label!r} occurrence {idx} of {len(kept)}; {where}"
            f"{'; other occurrences agree' if len(answered) > 1 else ''}{swallow_note}")


def read(page, spec: Spec, page_number: Optional[int] = None) -> Reading:
    """Read one field off one `fitz` page.

    Synonyms are tried in order and the first that resolves to exactly one
    answer wins. A synonym that resolves several ways does NOT end the search —
    that is how `CT` (which is also a package unit and a fragment of the Burmese
    notes line) steps aside for `COMMERCIAL TAX`. If every synonym is ambiguous,
    the ambiguity is what gets returned.
    """
    words = _words(page)
    if page_number is None:
        page_number = getattr(page, "number", None)
        page_number = None if page_number is None else page_number + 1
    if not words:
        return Reading(field=spec.field, outcome=Outcome.NO_TEXT, page_number=page_number,
                       reason="page has no text layer (scanned image)")

    avoid_rects: List[Rect] = []
    for a in spec.avoid:
        avoid_rects.extend(_locate(words, a, spec))

    all_cands: List[Candidate] = []
    fallback: Optional[Reading] = None
    for label in spec.labels:
        outcome, cands, reason = _read_one_label(
            page, words, spec, label, avoid_rects, page_number)
        all_cands.extend(cands)
        if outcome in (Outcome.FOUND, Outcome.BLANK):
            win = next(c for c in cands if c.outcome in (Outcome.FOUND, Outcome.BLANK))
            return Reading(field=spec.field, outcome=outcome, value=win.value,
                           raw=win.raw, label=label, label_rect=win.label_rect,
                           value_rect=win.value_rect, page_number=page_number,
                           how=win.how, reason=reason, candidates=tuple(all_cands))
        # Remember the most informative failure to report if nothing resolves.
        rank = {Outcome.AMBIGUOUS: 3, Outcome.NO_VALUE: 2, Outcome.NOT_FOUND: 1}
        cur = Reading(field=spec.field, outcome=outcome, label=label,
                      page_number=page_number, reason=reason,
                      candidates=tuple(all_cands))
        if fallback is None or rank[outcome] > rank[fallback.outcome]:
            fallback = cur

    return replace(fallback, candidates=tuple(all_cands))


def read_all(page, specs: Iterable[Spec], page_number: Optional[int] = None) -> Dict[str, Reading]:
    return {s.field: read(page, s, page_number) for s in specs}


def read_document(doc, specs: Iterable[Spec], pages: Optional[Sequence[int]] = None
                  ) -> Dict[str, Reading]:
    """Read across pages, with the SAME refusal rule applied between them.

    "First page that answers wins" is the document-level version of taking the
    nearest number: a bundled release order carries `Commercial tax 6,061,781`
    on the CUSDEC header page and `Commercial tax 204,403,500` — the taxable
    base — on the item page, and whichever came first would have been returned
    as the tax. Two pages that disagree is `AMBIGUOUS`; two that agree is fine.
    Pass `pages` to scope the read when the caller already knows which page is
    the header.
    """
    own = False
    if isinstance(doc, str):
        import fitz
        doc, own = fitz.open(doc), True
    try:
        idxs = [p - 1 for p in (pages or range(1, doc.page_count + 1))
                if 1 <= p <= doc.page_count]
        out: Dict[str, Reading] = {}
        for spec in specs:
            per_page = [read(doc[i], spec, page_number=i + 1) for i in idxs]
            answered = [r for r in per_page if r.certain]
            if not answered:
                amb = next((r for r in per_page if r.outcome == Outcome.AMBIGUOUS), None)
                nov = next((r for r in per_page if r.outcome == Outcome.NO_VALUE), None)
                out[spec.field] = amb or nov or (
                    per_page[0] if per_page else
                    Reading(field=spec.field, outcome=Outcome.NOT_FOUND,
                            reason="no pages selected"))
                continue
            distinct = {("<blank>" if r.outcome == Outcome.BLANK else r.value)
                        for r in answered}
            cands = tuple(c for r in per_page for c in r.candidates)
            if len(distinct) > 1:
                shown = ", ".join(sorted(str(d) for d in distinct))
                pgs = ", ".join(str(r.page_number) for r in answered)
                out[spec.field] = Reading(
                    field=spec.field, outcome=Outcome.AMBIGUOUS, page_number=None,
                    reason=f"pages {pgs} disagree ({shown}) — refusing to guess",
                    candidates=cands)
            else:
                w = answered[0]
                extra = (f"; {len(answered)} pages agree" if len(answered) > 1 else "")
                out[spec.field] = replace(w, reason=f"page {w.page_number}: {w.reason}{extra}",
                                          candidates=cands)
        return out
    finally:
        if own:
            doc.close()


# --------------------------------------------------------------------------
# the registry: specs that protect each other
# --------------------------------------------------------------------------

def _tokens(label: str) -> List[str]:
    return [_norm(t) for t in label.split()]


def _is_subphrase(short: List[str], long: List[str]) -> bool:
    if not short or len(short) >= len(long):
        return False
    return any(long[i:i + len(short)] == short
               for i in range(len(long) - len(short) + 1))


def registry(specs: Sequence[Spec]) -> Dict[str, Spec]:
    """Cross-link a set of specs so each label avoids every longer one.

    Registering the decoy IS the defence. `First approval declaration No.` and
    `Expected declaration date` are real fields on the form and are registered as
    such; the side effect is that `Declaration No.` and `Declaration date` can
    never bind to them. Nothing has to be listed twice, and a new field added
    later protects the existing ones for free.
    """
    known = [(s.field, lb, _tokens(lb)) for s in specs for lb in s.labels]
    out: Dict[str, Spec] = {}
    for s in specs:
        extra: List[str] = []
        for lb in s.labels:
            mine = _tokens(lb)
            for other_field, other_lb, other_toks in known:
                if other_lb == lb:
                    continue
                if _is_subphrase(mine, other_toks) and other_lb not in extra:
                    extra.append(other_lb)
        merged = tuple(dict.fromkeys(tuple(s.avoid) + tuple(extra)))
        out[s.field] = replace(s, avoid=merged)
    return out


#: The MACCS CUSDEC header, as printed. Field names match the `declarations`
#: columns so a caller can map straight through. Decoys are registered
#: alongside the real fields on purpose — see `registry()`.
CUSDEC: Dict[str, Spec] = registry([
    # --- identity -----------------------------------------------------------
    Spec("declaration_no", ("Declaration No.",), kind="number", max_dx=200,
         note="boxed top-right; the number prints UNDERNEATH the label"),
    Spec("first_approval_declaration_no", ("First approval declaration No.",),
         kind="number", allow_below=False,
         note="the earlier bonded entry an Ex-bond release clears — a DECOY for "
              "declaration_no, and a real field in its own right"),
    Spec("declaration_date", ("Declaration date",), kind="date", max_dx=120),
    Spec("expected_declaration_date", ("Expected declaration date",), kind="date",
         max_dx=120, note="decoy for declaration_date"),
    Spec("arrival_date", ("Arrival date",), kind="date", max_dx=120),
    Spec("release_order_date", ("Release order",), kind="date", max_dx=160,
         avoid=("Release order notification",),
         note="the page TITLE is 'Release order notification' — never a date"),
    Spec("completion_date", ("Declaration completion",), kind="date", max_dx=160),
    Spec("examination_completion_date", ("Examination completion",), kind="date",
         max_dx=160),

    # --- money --------------------------------------------------------------
    Spec("total_customs_value", ("Total customs value",), max_dx=160),
    Spec("total_item_value", ("Total item value",), max_dx=160),
    Spec("total_items", ("Total items",), max_dx=120),
    Spec("total_pages", ("Total pages",), max_dx=120),
    Spec("invoice_number", ("Invoice",), kind="text", max_dx=200, allow_below=False),

    # --- parties and currency ----------------------------------------------
    # Names run far to the right of their label and are multi-word, so they need a
    # wider window and the free-text kind. `Importer` also prints a registration
    # code BEFORE the company name ("C162371223-000 PREMIUM DISTRIBUTION ...") —
    # the caller strips it; capturing it here keeps the code recoverable.
    Spec("importer_name", ("Importer",), kind="text", max_dx=620, allow_below=False),
    Spec("consignor_name", ("Consignor",), kind="text", max_dx=620, allow_below=False,
         note="the overseas sender; NOT the importer and NOT the customs agency"),
    # The invoice-price line prints the currency immediately before the amount:
    # "Invoice price   A - DAP - THB -   669,704.3520". Reading it from that line
    # is safer than the 'Exchange Rate (1) THB' row, which repeats for rates 2 and 3.
    Spec("currency", ("Invoice price",), kind="currency", max_dx=200, allow_below=False,
         note="the code on the invoice-price line, e.g. THB / USD / CNY"),
    Spec("invoice_price_fc", ("Invoice price",), max_dx=240, allow_below=False,
         note="invoice CURRENCY, never MMK — the unit this column means"),
    Spec("invoice_price_mmk", ("(MMK)",), max_dx=140, allow_below=False),
    Spec("freight_value", ("Freight",), max_dx=240, allow_below=False),
    Spec("insurance_value", ("Insurance",), max_dx=240, allow_below=False),
    Spec("adjustment_code", ("Adjustment",), max_dx=120, allow_below=False,
         note="a small classification CODE (0, 2) — NOT money"),
    Spec("adjustment_value", ("Adjustment value",), max_dx=240, allow_below=False,
         note="the money. 33.4pt below the `Adjustment` row on the same form"),
    # Registered so `Adjustment` and `Insurance` cannot bind to them. The
    # build-up block prints five rows in eleven-point steps and three of them
    # begin with a word one of the short labels also uses.
    Spec("comprehensive_insurance_no", ("Comprehensive insurance No.",),
         kind="text", max_dx=200, allow_below=False, note="decoy for insurance_value"),
    Spec("comprehensive_adjustment_no", ("Comprehensive adjustment No.",),
         kind="text", max_dx=200, allow_below=False, note="decoy for adjustment_code"),
    Spec("exchange_rate", ("Exchange Rate",), max_dx=200, allow_below=False),

    # --- taxes and fees -----------------------------------------------------
    # Each is code-first then word, because a form that prints the code prints it
    # unambiguously; a form that does not needs the word. `CT` is deliberately
    # first even though it is ambiguous on some pages — an ambiguous synonym
    # yields to the next one rather than to a guess.
    Spec("import_export_customs_duty",
         ("CD", "IMPORT/EXPORT CUSTOMS DUTY", "Customs duty Amount"),
         max_dx=260, allow_below=False),
    Spec("commercial_tax_ct", ("CT", "COMMERCIAL TAX"), max_dx=260, allow_below=False),
    Spec("advance_income_tax_at", ("AT", "ADVANCED INCOME TAX", "ADVANCE INCOME TAX"),
         max_dx=260, allow_below=False),
    Spec("security_fee_sf", ("SF", "SECURITY FEE", "Security"),
         max_dx=260, allow_below=False,
         note="one form prints the code, another only the word. Order matters and "
              "is not alphabetical: on 100313868761 the tax block carries "
              "`SF SECURITY FEE 20,000` AND the right-hand column carries "
              "`Security 0` — two different figures. The code is the tax row, so "
              "it is tried first; the bare word is the last resort for the forms "
              "(100313488550, 100311799931, 100276747061) that print no SF row"),
    Spec("maccs_service_fee_mf", ("MF", "MACCS SERVICE FEE"), max_dx=260,
         allow_below=False),
    Spec("taxes_and_fees_total", ("Taxes and fees",), max_dx=200, allow_below=False),
    Spec("exemption_reduction", ("Exemption/Reduction",), max_dx=220, allow_below=False),
])


def cusdec(*fields: str) -> List[Spec]:
    """The CUSDEC specs for `fields` (all of them when none are named)."""
    return [CUSDEC[f] for f in fields] if fields else list(CUSDEC.values())


# --------------------------------------------------------------------------
# verification against the reference documents
# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys
    from pathlib import Path

    import fitz

    CORPUS = Path("/Users/rahulgupta/Desktop/pgro")
    DOCS = [
        ("100313488550__100313488550.pdf",
         "Ex-bond: two declaration numbers, Adjustment vs Adjustment value",
         {"declaration_no": 100319576711,
          "first_approval_declaration_no": 100313488550,
          "adjustment_code": 2,
          "adjustment_value": 44612.82,
          "total_customs_value": 198450000}),
        ("100311799931__ATS ID NO-100321333561.pdf",
         "Burmese script on the page; the form prints `Security`, not `SF`",
         {"commercial_tax_ct": 6061781,
          "security_fee_sf": 0,
          "declaration_no": 100321333561,
          "total_customs_value": 105422284.8}),
        ("100276747061__RO_1 (2).pdf",
         "clean 5-page MACCS; Adjustment value is genuinely blank",
         {"declaration_no": 100276747061,
          "total_customs_value": 32445915.66,
          "exchange_rate": 58.3322}),
    ]

    ORDER = ["declaration_no", "first_approval_declaration_no", "declaration_date",
             "expected_declaration_date", "arrival_date", "release_order_date",
             "invoice_number", "invoice_price_fc", "invoice_price_mmk",
             "freight_value", "insurance_value", "adjustment_code",
             "adjustment_value", "total_customs_value", "total_item_value",
             "import_export_customs_duty", "commercial_tax_ct",
             "advance_income_tax_at", "security_fee_sf", "maccs_service_fee_mf",
             "exchange_rate", "total_items"]

    failures: List[str] = []
    for name, why, expected in DOCS:
        path = CORPUS / name
        print("=" * 100)
        print(f"{name}\n  {why}")
        if not path.exists():
            print("  MISSING — skipped")
            continue
        doc = fitz.open(path)
        page = doc[0]                       # the CUSDEC header page on all three
        print(f"  {doc.page_count} pages; reading page 1 "
              f"({len(_words(page))} words in the text layer)")
        print("-" * 100)
        print(f"{'field':<32}{'value':<20}{'outcome':<11}why")
        print("-" * 100)
        for fname in ORDER:
            r = read(page, CUSDEC[fname], page_number=1)
            shown = "" if r.value is None else str(r.value)
            mark = " " if fname not in expected else ("." if r.value == expected[fname] else "X")
            print(f"{mark}{fname:<31}{shown:<20}{r.outcome:<11}{r.reason}")
            if fname in expected and r.value != expected[fname]:
                failures.append(f"{name}: {fname} = {r.value!r}, expected {expected[fname]!r}")

        # Fields that live on a LATER page of the bundle, read document-wide.
        whole = read_document(doc, cusdec("release_order_date", "completion_date",
                                          "commercial_tax_ct"))
        for fname in ("release_order_date", "completion_date"):
            r = whole[fname]
            print(f"\n  whole-document `{fname}`: {r.outcome} "
                  f"{'' if r.value is None else r.value} — {r.reason}")

        # The document-level refusal, on the field that proves it is needed.
        ct = whole["commercial_tax_ct"]
        print(f"\n  whole-document `commercial_tax_ct`: {ct.outcome} — {ct.reason}")
        for c in ct.candidates:
            if c.outcome in (Outcome.FOUND, Outcome.BLANK):
                print(f"      {c}")

        # What the ambiguity guard looks like when it fires: the bare 12-digit
        # rule that bug #1 used, expressed as a spec with no decoy registered.
        naive = Spec("declaration_no_UNGUARDED", ("Declaration No.",),
                     kind="number", max_dx=200)
        n = read(page, naive, page_number=1)
        print(f"  same field with the decoy NOT registered: {n.outcome} — {n.reason}")
        doc.close()
        print()

    print("=" * 100)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        sys.exit(1)
    print("all expected values matched")
