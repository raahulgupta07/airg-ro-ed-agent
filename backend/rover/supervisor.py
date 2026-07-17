"""Deterministic supervisor — the COMPILER + JUDGE. Never an LLM. Merges the
deterministic and vision cells, runs cross-field math invariants, and marks
columns 'suspect' (which the challenger tier then re-checks). Fail-closed:
any suspect column -> needs_review."""
from typing import Dict, List, Tuple

from .schema import Cell

# invoice-currency -> MMK plausibility band (mirrors reconcile._CCY_BANDS)
BANDS = {"THB": (40, 90), "USD": (1500, 5000), "SGD": (1500, 2500),
         "CNY": (250, 600), "EUR": (2000, 5000), "JPY": (12, 40), "AUD": (1200, 3500)}


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


# OCR digit confusions seen on scanned CUSDEC barcodes (S↔5, O↔0, I/l↔1, B↔8…).
_OCR_MAP = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0", "I": "1",
                          "l": "1", "L": "1", "B": "8", "Z": "2", "G": "6",
                          "Q": "0", "D": "0"})


def normalize_declaration_no(rec: Dict[str, Cell]):
    """Fix A — repair OCR digit confusions in declaration_no and validate it is a
    clean 11–12 digit id. If it cannot be made clean, flag the column for review."""
    c = rec.get("declaration_no")
    if not c or c.value in (None, ""):
        return
    raw = str(c.value).strip()
    fixed = raw.translate(_OCR_MAP)
    digits = "".join(ch for ch in fixed if ch.isdigit())
    if 11 <= len(digits) <= 12:
        if digits != raw:
            c.note = (c.note + "; " if c.note else "") + f"ocr-normalized from {raw}"
        c.value = digits
    else:
        c.status = "suspect"
        c.note = (c.note + "; " if c.note else "") + f"decl_no not 11-12 digits ({raw})"


def _digits(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def cross_check_declaration_no(det: Dict[str, Cell], vis: Dict[str, Cell],
                               rec: Dict[str, Cell]):
    """Catch a silently-wrong declaration_no. On a multi-document bundle the
    text-layer reader can grab a valid-length id off the WRONG sub-document (a port
    doc / permit). We have a second, independent read from the vision model — if the
    two disagree, we cannot know which is right, so we FLAG for review and keep both.
    A wrong document identity must never ship unflagged."""
    c = rec.get("declaration_no")
    if c is None or c.value in (None, ""):
        return
    dcell, vcell = det.get("declaration_no"), vis.get("declaration_no")
    dv = _digits(getattr(dcell, "value", None))
    vv = _digits(getattr(vcell, "value", None))

    # A First-approval text read (conf >= 0.95) targets the customs-specific label and
    # is trusted on its own. Everything else — a plain 'Declaration No.' fallback, or a
    # vision-only read — must be CORROBORATED by an agreeing second read; on a
    # multi-doc bundle the fallback can grab an id off the wrong sub-document.
    det_strong = (dcell is not None and getattr(dcell, "status", "empty") != "empty"
                  and getattr(dcell, "confidence", 0) >= 0.95)
    if det_strong:
        if dv and vv and dv != vv:                      # even a strong read: note dissent
            c.note = (c.note + "; " if c.note else "") + \
                f"vision decl_no differs ({vv}) — kept first-approval"
            if vv not in [_digits(a) for a in c.alternates]:
                c.alternates.append(vv)
        return

    corroborated = bool(dv and vv and dv == vv)
    if not corroborated:
        c.status = "suspect"
        why = (f"declaration_no disagreement (text={dv} vision={vv})" if dv and vv
               else "declaration_no uncorroborated (single fallback read on a bundle)")
        c.note = (c.note + "; " if c.note else "") + why + " — confirm"
        for alt in (dv, vv):
            if alt and _digits(c.value) != alt and alt not in [_digits(a) for a in c.alternates]:
                c.alternates.append(alt)


def merge(det: Dict[str, Cell], vis: Dict[str, Cell]) -> Dict[str, Cell]:
    """Merge deterministic + vision. A high-confidence deterministic read
    (>=0.95: First-approval decl_no, anchored date) WINS over vision. A weaker
    deterministic fallback only FILLS when vision left the column empty."""
    rec = dict(vis)
    for col, c in det.items():
        if c.status == "empty":
            continue
        vcell = rec.get(col)
        v_empty = (vcell is None) or getattr(vcell, "status", "empty") == "empty" \
            or getattr(vcell, "value", None) in (None, "")
        if c.confidence >= 0.95 or v_empty:
            rec[col] = c
    return rec


def check(rec: Dict[str, Cell]) -> Tuple[List[str], List[str]]:
    """Run invariants. Returns (suspect_columns, notes)."""
    suspect, notes = [], []

    def flag(col, why):
        if col in rec:
            rec[col].status = "suspect"
            rec[col].note = (rec[col].note + "; " if rec[col].note else "") + why
        if col not in suspect:
            suspect.append(col)
        notes.append(f"{col}: {why}")

    cur = str((rec.get("currency") and rec["currency"].value) or "").upper()[:3]
    rate = _num(rec.get("exchange_rate") and rec["exchange_rate"].value)
    band = BANDS.get(cur)

    # 1. exchange rate must sit in its currency band (the real UAT failure class).
    if rate is None:
        flag("exchange_rate", "no rate extracted")
    elif band and not (band[0] <= rate <= band[1]):
        flag("exchange_rate", f"{rate} outside {cur} band {band}")

    # 2. at least one CORE tax present (duty/CT/AT) when a value exists.
    cd = _num(rec.get("import_export_customs_duty") and rec["import_export_customs_duty"].value)
    ct = _num(rec.get("commercial_tax_ct") and rec["commercial_tax_ct"].value)
    at = _num(rec.get("advance_income_tax_at") and rec["advance_income_tax_at"].value)
    if not any(x is not None for x in (cd, ct, at)):
        flag("import_export_customs_duty", "no core tax present")

    # 3. CT == Exemption/Reduction (both nonzero) -> classic mislabel (gemini bug).
    exn = _num(rec.get("exemption_reduction") and rec["exemption_reduction"].value)
    if ct and exn and abs(ct - exn) < 1:
        flag("commercial_tax_ct", "CT equals Exemption/Reduction — likely mislabel")

    # 4. low-confidence any column.
    for col, c in rec.items():
        if isinstance(c, Cell) and c.status == "ok" and c.confidence and c.confidence < 0.75:
            flag(col, f"low confidence {c.confidence:.2f}")

    return suspect, notes


def value_block_empty(rec: Dict[str, Cell]) -> bool:
    """Fix B trigger — the whole value/tax block came back empty (scanned page the
    router missed). Used by the pipeline to escalate to more pages."""
    keys = ("exchange_rate", "total_customs_value", "import_export_customs_duty")
    def empty(k):
        c = rec.get(k)
        return c is None or getattr(c, "value", None) in (None, "")
    return all(empty(k) for k in keys)


def derive_rate_from_totals(rec: Dict[str, Cell]):
    """Fix L2 — when the printed exchange_rate is missing, DERIVE it exactly from
    the doc's own totals: rate = customs_value_MMK / customs_value_USD. These
    CUSDECs print the customs value in BOTH MMK and USD, so this is deterministic
    and exact (not a guess). Band-checked against the currency's plausibility band."""
    c = rec.get("exchange_rate")
    if c is not None and getattr(c, "value", None) not in (None, ""):
        return  # already have a printed rate — leave it alone

    mmk = _num(getattr(rec.get("total_customs_value"), "value", None))
    usd = _num(getattr(rec.get("customs_value_usd"), "value", None))
    if not (mmk and usd and usd > 0):
        return

    rate = round(mmk / usd, 4)
    cur = str((rec.get("currency") and rec["currency"].value) or "").upper()[:3]
    band = BANDS.get(cur)
    source = f"derived = {mmk} MMK / {usd} USD"

    # A derived rate depends on a SECOND extracted number (the USD total), which can
    # itself be misread — e.g. a per-item unit price mistaken for the total, giving a
    # plausible-but-wrong in-band rate. So a derivation is ADVISORY: it fills a
    # candidate value but NEVER clears review. The human confirms it. Only a rate the
    # model/text read DIRECTLY (model != 'derived') is trusted to auto-pass. This
    # keeps the fail-closed guarantee — no silent wrong rate ever ships.
    note = ("derived candidate from totals — CONFIRM"
            if (band and band[0] <= rate <= band[1])
            else "derived rate out of band")

    if c is None:
        c = Cell(column="exchange_rate", model="derived")
        rec["exchange_rate"] = c
    c.value = rate
    c.model = "derived"
    c.source = source
    c.confidence = 0.5          # < 0.75 -> check()'s low-confidence rule flags it,
    c.status = "ok"             # which sets suspect + forces review. Advisory only.
    c.note = (c.note + "; " if getattr(c, "note", None) else "") + note


def compile(det: Dict[str, Cell], vis: Dict[str, Cell]):
    rec = merge(det, vis)
    normalize_declaration_no(rec)                    # Fix A
    cross_check_declaration_no(det, vis, rec)        # Fix: catch wrong decl_no on bundles
    derive_rate_from_totals(rec)                     # Fix L2
    suspect, notes = check(rec)
    # Sweep: any cell a prior step already marked 'suspect' (cross-check, derivation)
    # joins the review set even if check()'s own rules didn't fire it.
    for col, c in rec.items():
        if isinstance(c, Cell) and c.status == "suspect" and col not in suspect:
            suspect.append(col)
            notes.append(f"{col}: {c.note}")
    needs_review = len(suspect) > 0
    return rec, suspect, notes, needs_review
