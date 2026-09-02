"""Vision CUSDEC rescue for SCANNED release-order pages (no text layer).

~69% of real bundled release orders are SCANNED — the MACCS CUSDEC page is an
IMAGE with no text layer, so the deterministic text-based rescue
(`cusdec_rescue.cusdec_fields`) reads nothing, and the full V7 vision pipeline is
slow (~340s). This module is the fast middle ground: a FOCUSED, single vision
read of just the CUSDEC page to recover the high-value header fields —
exchange_rate, declaration_date (RO/ID), total_customs_value, declaration_no, the
tax block (customs_duty / commercial_tax / advance_income_tax / security_fee /
maccs_service_fee), plus currency and the CIF build-up (freight / insurance /
adjustment) when visible.

It returns the SAME dict shape as `cusdec_rescue.cusdec_fields`, so it is a
drop-in fallback: when the text-layer rescue returns None (scanned page), the
caller can try `vision_cusdec_fields(pdf_path)`.

OpenRouter only (hard project rule — no direct Google/OpenAI/Anthropic SDK). One
vision call per page, model `google/gemini-3-flash-preview` (same as Presto /
Scribe). Never raises — returns None on any failure or missing key.
"""
import base64
import io
import os
import re
import time
from typing import Optional

import numeric

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover
    fitz = None

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

import requests

try:
    import config
except Exception:  # pragma: no cover — allow standalone import
    config = None

# Reuse the deterministic rescue's JSON-parse + rate-band logic so the two paths
# behave identically (same currency sanity windows, same numeric coercion).
try:
    from v11.tools import cusdec_rescue as _cr
except Exception:  # pragma: no cover
    _cr = None

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model + render knobs (env-overridable, mirroring v11/config + v13/config style).
# Measured on the failing page (100306920231 p1, four runs each, same prompt and
# image): gemini-3.6-flash 15-17/18, gemini-3-flash-preview 14/18, grok-4.5 14/18,
# claude-haiku-4.5 7/18. 3-flash-preview misread the assessed total as the invoice
# (MMK) line printed above it; grok returned `109306070231` for a declaration
# number reading `100306920231`, which is the digit-flip the rover notes already
# warn about. 3.6-flash costs ~10x the output tokens and ~4x the wall clock, paid
# only on scanned documents.
VISION_RESCUE_MODEL = os.getenv(
    "VISION_RESCUE_MODEL", "google/gemini-3.6-flash")
# 3.6-flash spends tokens before it answers. At the previous 2000 cap it truncated
# mid-JSON and the whole rescue returned nothing — a silent total failure, not a
# degraded read.
VISION_RESCUE_MAX_TOKENS = int(os.getenv("VISION_RESCUE_MAX_TOKENS", "6000"))
# Two independent reads of the same page. Stamp-obscured cells are where this
# model invents: `adjustment_value` came back null, 2156382.176, 24882.176 and
# 339882.176 across four runs of the identical image. A field only survives when
# the votes agree.
VISION_RESCUE_VOTES = int(os.getenv("VISION_RESCUE_VOTES", "2"))
# Scanned CUSDEC text is fine printed — 250 DPI reads it without ballooning the
# image (kept ≤ 2200px longest edge like Scribe).
VISION_RESCUE_DPI = int(os.getenv("VISION_RESCUE_DPI", "250"))
# When auto-locating the CUSDEC page, cap how many candidate pages we'll spend a
# vision call on before giving up (bounds cost on large bundled PDFs).
VISION_RESCUE_MAX_PAGES = int(os.getenv("VISION_RESCUE_MAX_PAGES", "6"))
VISION_RESCUE_TIMEOUT = int(os.getenv("VISION_RESCUE_TIMEOUT", "120"))

# ─── Coordinates on a photographed page ──────────────────────────────────────
# `field_bbox.py` locates a value by SEARCHING the text layer for it. A scanned
# declaration has no text layer, so it finds nothing and the review screen says
# "location not known" for every field on roughly half the corpus. The model
# reading the page is looking straight at the value; asking it where it saw it
# costs nothing beyond the handful of output tokens the extra JSON key takes,
# because the call is already being made.
#
# Set VISION_RESCUE_BOXES=0 to stop asking. The values are the point of this
# module and the coordinates are a bonus; if the longer prompt is ever shown to
# cost accuracy, this switch turns it off without touching the read.
VISION_RESCUE_BOXES = os.getenv("VISION_RESCUE_BOXES", "1") not in ("0", "false", "False")

# What a plausible box round a printed value looks like, as a FRACTION of the
# page. These are rejection bounds, not guesses: anything outside them is the
# model shrugging (a box round the whole form, or round the table it is in)
# rather than pointing at a figure, and a wrong box is worse than none.
_BOX_MIN_W, _BOX_MIN_H = 0.005, 0.004
_BOX_MAX_W, _BOX_MAX_H = 0.70, 0.15
_BOX_MAX_AREA = 0.10
# Two independent reads must put the box in the same place. Measured across two
# runs of two declarations, agreeing reads landed within ~0.005 normalised; this
# window is an order of magnitude looser than that and still far tighter than
# the gap between two different rows on the form.
_BOX_AGREE_TOL = 0.05

# Pricing fallback (per 1M tokens) for gemini-3-flash when usage.cost is absent.
_FLASH_IN_PER_M = 0.50
_FLASH_OUT_PER_M = 3.00

# Per-currency FX-rate plausibility bands (invoice currency → MMK). Reused from
# cusdec_rescue when available; duplicated here so this module stands alone.
_RATE_BANDS = getattr(_cr, "_RATE_BANDS", None) or {
    "THB": (40.0, 90.0),
    "USD": (1500.0, 5000.0),
    "SGD": (1500.0, 2500.0),
    "CNY": (250.0, 600.0),
    "EUR": (2000.0, 5000.0),
    "JPY": (12.0, 40.0),
    "AUD": (1200.0, 3500.0),
}

# The exact output-dict keys (must match cusdec_rescue.cusdec_fields).
_TAX_KEYS = (
    "import_export_customs_duty",
    "commercial_tax_ct",
    "advance_income_tax_at",
    "security_fee_sf",
    "maccs_service_fee_mf",
)
_CORE_TAX_KEYS = (
    "import_export_customs_duty",
    "commercial_tax_ct",
    "advance_income_tax_at",
)

# Model-facing field name → cusdec_fields key. The prompt asks for the short,
# unambiguous names on the left; we remap to the DB-shaped keys on the right.
_FIELD_MAP = {
    "exchange_rate": "exchange_rate",
    "currency": "currency",
    "declaration_date": "declaration_date",
    "total_customs_value": "total_customs_value",
    "declaration_no": "declaration_no",
    "customs_duty": "import_export_customs_duty",
    "commercial_tax": "commercial_tax_ct",
    "advance_income_tax": "advance_income_tax_at",
    "security_fee": "security_fee_sf",
    "maccs_service_fee": "maccs_service_fee_mf",
    "freight_value": "freight_value",
    "insurance_value": "insurance_value",
    "adjustment_value": "adjustment_value",
    # Added after a bundled release order shipped the licence's invoice instead of
    # the declaration's. All four are printed on the same page this call already
    # reads; until now they were simply never asked for, so whatever the typed lane
    # scraped off an attachment survived unchallenged.
    "invoice_number": "invoice_number",
    "invoice_price_fc": "invoice_price_fc",
    "invoice_price_mmk": "invoice_price_mmk",
    "arrival_date": "arrival_date",
}

# Numeric fields to coerce (strip commas, cast to float). declaration_no /
# currency / declaration_date stay as strings.
_NUMERIC_KEYS = {
    "exchange_rate", "total_customs_value",
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf",
    "freight_value", "insurance_value", "adjustment_value",
    "invoice_price_fc", "invoice_price_mmk",
}

_PROMPT_HEAD = """This is a SCANNED image of a Myanmar MACCS customs declaration
(CUSDEC) / release-order page. Read the printed values carefully and return the
header fields as STRICT JSON. Use null for any field you cannot find on THIS page.

Return EXACTLY this shape (no extra keys, no commentary):

{
  "exchange_rate": number|null,
  "currency": "THB"|"USD"|"SGD"|"CNY"|"EUR"|"JPY"|"AUD"|null,
  "declaration_date": "yyyy-mm-dd"|null,
  "total_customs_value": number|null,
  "declaration_no": "string"|null,
  "customs_duty": number|null,
  "commercial_tax": number|null,
  "advance_income_tax": number|null,
  "security_fee": number|null,
  "maccs_service_fee": number|null,
  "freight_value": number|null,
  "insurance_value": number|null,
  "adjustment_value": number|null,
  "invoice_number": "string"|null,
  "invoice_price_fc": number|null,
  "invoice_price_mmk": number|null,
  "arrival_date": "yyyy-mm-dd"|null,
  "is_cusdec": true|false
}

Guidance (Myanmar CUSDEC specifics — read carefully):
- All amounts are NUMBERS ONLY — strip thousands separators (2,208,855.96 -> 2208855.96).
- exchange_rate = the rate converting the INVOICE currency to Myanmar Kyat (MMK).
  The form may print several rates; pick the one for `currency`. Plausible ranges:
  THB ~ 40-90, USD ~ 1500-5000, SGD ~ 1500-2500, CNY ~ 250-600, EUR ~ 2000-5000,
  JPY ~ 12-40, AUD ~ 1200-3500. Never return a customs figure as the rate.
- currency = the 3-letter invoice currency code shown next to the exchange rate.
- declaration_date = the RO/ID registration ("Declaration date") in ISO yyyy-mm-dd.
  This is NOT the "Expected declaration date". Convert any dd/mm/yyyy you read.
- total_customs_value = the amount on the "Total customs value" row. On this form a
  "(MMK)" figure is printed one or two rows ABOVE it, on the "Invoice price" block —
  that is the invoice converted to kyat, NOT the assessed total, and it is always the
  SMALLER of the two. Never return the same number for both.
- declaration_no = the number in the "Declaration No." box at the TOP of the form.
  It is NOT the "First approval declaration No." printed lower down, which belongs to
  an earlier declaration. Typically 12 digits — read each digit individually.
- invoice_number = the value on the "Invoice" row (often prefixed "A-" — drop the
  prefix). It is NOT the "B/L (AWB) No." printed above it in the transport block,
  which on these bundles looks like a similar code.
- invoice_price_fc = the amount on the "Invoice price" row, in the INVOICE currency.
- invoice_price_mmk = the "(MMK)" figure directly below it. Same money, converted.
- arrival_date = the "Arrival date" in the transport block on the left, ISO yyyy-mm-dd.
- The tax block is usually a labelled table: IMPORT/EXPORT CUSTOMS DUTY,
  COMMERCIAL TAX, ADVANCED INCOME TAX, SECURITY FEE, MACCS SERVICE FEE — read each
  amount as a number; a dash "-" means null (not 0).
- freight_value / insurance_value / adjustment_value = the CIF build-up amounts in
  the INVOICE currency (not MMK); a dash "-" means null. adjustment_value is signed
  (negative for a deduction). The lone small integer printed beside the word
  "Adjustment" one row above "Adjustment value" is a CLASSIFICATION CODE, not money —
  never return it as adjustment_value.
- These forms are stamped and signed over. Ink CROSSING a value is normal — if you
  can still make out every digit, read it. Return null only when a digit is actually
  hidden and you would have to guess it. A partly-guessed amount is worse than no
  amount: once stored it is indistinguishable from a real reading.
- is_cusdec = true only if THIS page is the customs declaration / release order
  carrying the tax block or the assessed total; false for licences / invoices /
  packing lists / blank pages.
"""

# Asked for only when VISION_RESCUE_BOXES is on. Kept as its own block so the
# value half of the prompt — which is tuned against real pages and measured —
# stays byte-identical whether or not coordinates are wanted.
_BOX_SECTION = """
ALSO include ONE extra key, "boxes", giving WHERE on this image you read each
value. Omit the key entirely if you would have to guess.

  "boxes": {
    "<field name from above>": [x0, y0, x1, y1],
    ...
  }

- Coordinates are FRACTIONS of the image, from 0 to 1. x0/x1 are measured from
  the LEFT edge, y0/y1 from the TOP edge. [0,0,1,1] would be the whole page.
- The box must be a TIGHT rectangle round the PRINTED VALUE itself — the digits
  or the text you read — not round its label, not round the row, and never round
  the whole table or the whole form.
- Include an entry ONLY for a field whose value you returned as non-null AND
  whose position on this image you are sure of. A field you did not read, or
  read from memory of the form's layout rather than from a place you can point
  at, must simply be left out of "boxes". Leaving it out is expected and costs
  nothing; a rectangle over the wrong number is the one thing that must not
  happen — it sends a human to confirm a customs figure against a number that
  is not it.
"""

_PROMPT_TAIL = """
Return ONLY the JSON object.
"""

# The value-only prompt, unchanged from before coordinates existed.
PROMPT = _PROMPT_HEAD + _PROMPT_TAIL


def _prompt() -> str:
    return _PROMPT_HEAD + (_BOX_SECTION if VISION_RESCUE_BOXES else "") + _PROMPT_TAIL


def _has_key() -> bool:
    key = getattr(config, "API_KEY", "") if config else ""
    if not key:
        key = os.getenv("OPENROUTER_API_KEY", "")
    return bool(key) and key != "sk-or-v1-your-openrouter-key-here"


def _api_key() -> str:
    key = getattr(config, "API_KEY", "") if config else ""
    return key or os.getenv("OPENROUTER_API_KEY", "")


def _parse_json(raw: str):
    """Robust JSON extraction (same style as presto/scribe _parse_json)."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    if "{" not in s:
        return None
    try:
        s = s[s.index("{"):s.rindex("}") + 1]
    except Exception:
        return None
    import json
    for cand in (s, re.sub(r",(\s*[\}\]])", r"\1", s)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _num(v):
    """Coerce a model value to float (commas, parens, currency), or None.

    This used to strip every non-digit character, which quietly turned the date
    "2026/01/08" into 20260108 and the MA-series id "MA0259/100405" into a
    number. The shared parser refuses both instead of inventing a value.
    """
    return numeric.to_float(v)


def _to_iso(v):
    """Normalise a date string to ISO yyyy-mm-dd via cusdec_rescue helper if
    present, else a small local fallback. Returns None if unusable."""
    if not v:
        return None
    if _cr is not None:
        try:
            iso = _cr._to_iso(str(v))
            if iso:
                return iso
        except Exception:
            pass
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", str(v))
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", str(v))
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None
    return None


def _rate_in_band(rate, currency) -> bool:
    """True if `rate` is plausible for `currency` (reuses cusdec_rescue band
    logic when available; unknown currency → broad 1 < rate < 10000 window)."""
    if rate is None:
        return False
    if _cr is not None:
        try:
            return _cr._rate_in_band(rate, currency)
        except Exception:
            pass
    band = _RATE_BANDS.get((currency or "").upper())
    if band:
        return band[0] <= rate <= band[1]
    return 1.0 < rate < 10000.0


def _norm_box(raw) -> Optional[tuple]:
    """A model-reported box as a validated (x0, y0, x1, y1) in 0..1, or None.

    Everything here is a REJECTION rule. The one thing this function must never
    do is repair a box into something plausible: a rectangle that has been
    nudged into range is indistinguishable, once stored, from one the model
    actually pointed at, and it lands on the page with the same authority.

    Rejected: wrong arity, non-numbers, out of range, inverted or zero-sized,
    and — the one that matters most — a box so large it is the model gesturing
    at the form rather than at a figure. `_BOX_MAX_*` are drawn round what a
    printed amount occupies on these pages with a wide margin; a box round the
    tax table or the whole sheet fails them.
    """
    if isinstance(raw, dict):
        raw = [raw.get("x0"), raw.get("y0"), raw.get("x1"), raw.get("y1")]
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    for v in (x0, y0, x1, y1):
        if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
            return None
        if not (0.0 <= v <= 1.0):
            return None
    w, h = x1 - x0, y1 - y0
    if w < _BOX_MIN_W or h < _BOX_MIN_H:
        return None
    if w > _BOX_MAX_W or h > _BOX_MAX_H or (w * h) > _BOX_MAX_AREA:
        return None
    return (x0, y0, x1, y1)


def _extract_boxes(parsed: dict, out: dict) -> dict:
    """`{db_field: (x0,y0,x1,y1)}` for fields this read actually returned.

    A box only survives when the SAME read produced a value for that field. A
    coordinate without a value marks a place on the page where nothing is being
    claimed, and a coordinate for a field whose value was later dropped (an
    out-of-band rate, a decoy total) would point at exactly the figure the
    pipeline just decided not to trust.
    """
    boxes = parsed.get("boxes")
    if not isinstance(boxes, dict):
        return {}
    keep = {}
    for src, dst in _FIELD_MAP.items():
        if out.get(dst) is None:
            continue
        box = _norm_box(boxes.get(src))
        if box:
            keep[dst] = box
    return keep


def _boxes_agree(a: tuple, b: tuple) -> bool:
    """Did two reads point at the same place? Corner-wise, in page fractions."""
    return all(abs(p - q) <= _BOX_AGREE_TOL for p, q in zip(a, b))


def _to_page_rect(box: tuple, page) -> Optional[dict]:
    """Normalised box -> `{page,x,y,w,h}` in this page's own coordinates.

    `page.rect` is the real, rendered size of THIS page — the same space
    `search_for` reports in and `add_highlight_annot` consumes, and the space
    `get_pixmap` rasterised, so a fraction of the image is a fraction of the
    rect. Assuming A4 would be wrong on every page that is not A4, and these
    bundles mix sizes.
    """
    try:
        r = page.rect
        w, h = float(r.width), float(r.height)
        if w <= 0 or h <= 0:
            return None
        x0 = float(r.x0) + box[0] * w
        y0 = float(r.y0) + box[1] * h
        bw = (box[2] - box[0]) * w
        bh = (box[3] - box[1]) * h
    except Exception:
        return None
    if bw <= 0 or bh <= 0:
        return None
    return {"page": page.number + 1, "x": round(x0, 2), "y": round(y0, 2),
            "w": round(bw, 2), "h": round(bh, 2),
            # Which tier measured this. `provenance`/`routes.evidence` report
            # `exact` only for the geometry tier; a model-reported box is
            # `estimated`, and the difference has to survive into storage.
            "source": "vision"}


def _render_page_png_b64(doc, page_index: int) -> Optional[str]:
    """Render one page to a base64 PNG at VISION_RESCUE_DPI. None on failure."""
    try:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=VISION_RESCUE_DPI)
    except Exception:
        return None
    try:
        if Image is not None:
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if max(img.size) > 2200:
                img.thumbnail((2200, 2200), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
        # Pillow unavailable → let fitz emit PNG bytes directly.
        return base64.b64encode(pix.tobytes("png")).decode()
    except Exception:
        return None


def _call_vision(b64_png: str):
    """One OpenRouter vision call. Returns (parsed_dict|None, usage_dict).
    Never raises."""
    parts = [
        {"type": "text", "text": _prompt()},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
    ]
    payload = {
        "model": VISION_RESCUE_MODEL,
        "messages": [{"role": "user", "content": parts}],
        "temperature": 0,
        "max_tokens": VISION_RESCUE_MAX_TOKENS,
    }
    headers = {"Authorization": f"Bearer {_api_key()}",
               "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            r = requests.post(API_URL, headers=headers, json=payload,
                              timeout=VISION_RESCUE_TIMEOUT)
            if r.status_code == 200:
                body = r.json()
                raw = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {}) or {}
                return _parse_json(raw), usage
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None, {}


def _cost_from_usage(usage: dict) -> float:
    if not usage:
        return 0.0
    if usage.get("cost") is not None:
        try:
            return float(usage["cost"])
        except Exception:
            pass
    ti = int(usage.get("prompt_tokens", 0) or 0)
    to = int(usage.get("completion_tokens", 0) or 0)
    return round(ti / 1_000_000 * _FLASH_IN_PER_M
                 + to / 1_000_000 * _FLASH_OUT_PER_M, 6)


def _shape(parsed: dict, usage: dict) -> Optional[dict]:
    """Map a model read to the cusdec_fields dict shape, coerce + band-check.
    Returns None if the read yields nothing usable."""
    if not isinstance(parsed, dict):
        return None
    out: dict = {}
    for src, dst in _FIELD_MAP.items():
        if src not in parsed:
            continue
        val = parsed.get(src)
        if dst in ("declaration_date", "arrival_date"):
            out[dst] = _to_iso(val)
        elif dst == "invoice_number":
            # The form prints "A- 2025SIE172"; the ledger keys on the bare
            # reference. Keep every other character — these are not all digits.
            s = re.sub(r"^\s*[A-Z]{1,2}\s*-\s*", "", str(val or "").strip())
            out[dst] = s or None
        elif dst == "currency":
            cur = str(val).upper().strip() if val else None
            out[dst] = cur if (cur and re.fullmatch(r"[A-Z]{3}", cur)) else None
        elif dst == "declaration_no":
            if val in (None, ""):
                out[dst] = None
            else:
                digits = re.sub(r"\D", "", str(val))
                out[dst] = digits or str(val).strip() or None
        elif dst in _NUMERIC_KEYS:
            out[dst] = _num(val)
        else:
            out[dst] = val

    # Same currency sanity band cusdec_rescue applies: an out-of-band rate is
    # junk (a stray customs figure), so drop it rather than return it.
    rate = out.get("exchange_rate")
    currency = out.get("currency")
    if rate is not None and not _rate_in_band(rate, currency):
        out["exchange_rate"] = None

    # An assessed total of zero is noise — there is no such declaration.
    if out.get("total_customs_value") is not None and out["total_customs_value"] <= 0:
        out["total_customs_value"] = None
    # A tax of zero is NOT noise. Commercial Tax is genuinely 0 on many of these
    # declarations and is printed as `0`; the form uses a dash for absent, and the
    # prompt asks for null there. Nulling a real zero here made an exempt document
    # look unread, and `_pick()` downstream exists precisely to keep the two apart.
    # Only a negative is impossible.
    for k in _TAX_KEYS:
        if out.get(k) is not None and out[k] < 0:
            out[k] = None

    # The assessed total and the invoice-in-kyat are printed two rows apart and the
    # smaller one has been returned for both. They are never equal on a real
    # declaration — the assessed value carries the uplift — so an exact match means
    # the wrong row was read. Drop the total rather than ship the invoice as it.
    _tcv, _ipm = out.get("total_customs_value"), out.get("invoice_price_mmk")
    if _tcv is not None and _ipm is not None and abs(_tcv - _ipm) <= 1.0:
        out["total_customs_value"] = None
        out["_decoy_total"] = True

    # Usable only if we recovered at least one high-value field.
    signal_keys = ("total_customs_value", "exchange_rate", "declaration_no") + _TAX_KEYS
    if not any(out.get(k) is not None for k in signal_keys):
        return None

    # LAST, deliberately: every drop above (out-of-band rate, decoy total,
    # negative tax) has already happened, so a box can never outlive the value
    # it points at.
    if VISION_RESCUE_BOXES:
        nb = _extract_boxes(parsed, out)
        if nb:
            out["_boxes_norm"] = nb

    out["_vision"] = True
    ti = int(usage.get("prompt_tokens", 0) or 0)
    to = int(usage.get("completion_tokens", 0) or 0)
    out["tokens_in"] = ti
    out["tokens_out"] = to
    out["cost"] = _cost_from_usage(usage)
    return out


def _agree(a, b) -> bool:
    """Do two independent reads of the same cell say the same thing?

    Numbers get a hair of tolerance for the last printed digit; everything else is
    compared on its normalised text. Two Nones agree — both reads saying "covered
    by a stamp" is a consistent answer, and the right one.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(0.01, abs(a) * 0.0005)
    return re.sub(r"\W", "", str(a)).upper() == re.sub(r"\W", "", str(b)).upper()


def _vote(reads: list) -> Optional[dict]:
    """Collapse N reads of one page into the fields they agree on.

    A single read cannot tell a confident answer from an invented one. On the page
    that prompted this, four runs produced four different `adjustment_value`s over a
    stamped cell while every unobstructed field came back identical every time — so
    disagreement is the signal, and a field that differs is dropped rather than
    averaged or first-wins. Dropping leaves the cell blank for a reviewer, which is
    recoverable; a plausible wrong number is not.
    """
    reads = [r for r in reads if r]
    if not reads:
        return None
    if len(reads) == 1:
        return reads[0]
    base = dict(reads[0])
    dropped = []
    for k in list(base.keys()):
        if k.startswith("_") or k in ("tokens_in", "tokens_out", "cost"):
            continue
        if not all(_agree(base.get(k), r.get(k)) for r in reads[1:]):
            base[k] = None
            dropped.append(k)
    # Coordinates vote exactly like values do, and for the same reason: a single
    # read cannot tell a place the model saw from a place it produced. A box
    # survives only when every read put it in the same place AND the value it
    # points at survived the vote — a field whose value the reads disagreed on
    # has just been blanked, so a box for it would sit on a figure nobody is
    # claiming. Disagreement drops the box; it is never averaged.
    if VISION_RESCUE_BOXES:
        first = reads[0].get("_boxes_norm") or {}
        agreed = {}
        for field, box in first.items():
            if base.get(field) is None:
                continue
            others = [(r.get("_boxes_norm") or {}).get(field) for r in reads[1:]]
            if all(o and _boxes_agree(box, o) for o in others):
                agreed[field] = box
        if agreed:
            base["_boxes_norm"] = agreed
        else:
            base.pop("_boxes_norm", None)

    for k in ("tokens_in", "tokens_out", "cost"):
        base[k] = sum(float(r.get(k) or 0) for r in reads)
    if dropped:
        base["_vote_dropped"] = sorted(dropped)
    base["_votes"] = len(reads)
    return base


def _read_page(doc, idx: int, votes: int) -> Optional[dict]:
    """One page, `votes` independent reads, merged. None if nothing usable."""
    b64 = _render_page_png_b64(doc, idx)
    if not b64:
        return None
    shaped = []
    for _ in range(max(1, votes)):
        parsed, usage = _call_vision(b64)
        if parsed:
            s = _shape(parsed, usage)
            if s:
                shaped.append(s)
    voted = _vote(shaped)
    if not voted:
        return None

    # Fractions of the image become points on THIS page, and the page number is
    # the page that was actually read — not the page the values were expected on.
    # Both halves matter equally: a correct rectangle on the wrong sheet of a
    # 28-page bundle is still a reviewer sent to the wrong document.
    norm = voted.pop("_boxes_norm", None)
    if norm:
        try:
            page = doc[idx]
            located = {}
            for field, box in norm.items():
                rect = _to_page_rect(box, page)
                if rect:
                    located[field] = rect
            if located:
                voted["_boxes"] = located
        except Exception:
            # A coordinate failure must never cost the values on the page.
            voted.pop("_boxes", None)
    return voted


def _has_tax_block(fields: dict) -> bool:
    return any(fields.get(k) is not None for k in _TAX_KEYS)


def _has_core_tax(fields: dict) -> bool:
    return any(fields.get(k) is not None for k in _CORE_TAX_KEYS)


def _candidate_order(doc) -> list:
    """Page indices ordered best-first for the scanned CUSDEC. Scanned pages
    (little/no text layer) come first — those are exactly the ones the text
    rescue can't read and where the real CUSDEC image lives."""
    scored = []
    for i in range(doc.page_count):
        try:
            txt = doc[i].get_text() or ""
        except Exception:
            txt = ""
        scored.append((len(txt.strip()), i))
    # fewest text chars first (most likely a scanned image), then page order
    scored.sort(key=lambda t: (t[0], t[1]))
    return [i for _, i in scored]


def vision_cusdec_fields(pdf_path: str, cusdec_page: Optional[int] = None) -> Optional[dict]:
    """Vision-read the scanned CUSDEC page.

    Returns the same dict shape as cusdec_rescue.cusdec_fields (keys:
    exchange_rate, currency, declaration_date, total_customs_value,
    declaration_no, import_export_customs_duty, commercial_tax_ct,
    advance_income_tax_at, security_fee_sf, maccs_service_fee_mf, freight_value,
    insurance_value, adjustment_value) plus `_vision: True` and optional
    tokens_in/tokens_out/cost. Returns None if nothing usable. NEVER raises.

    When VISION_RESCUE_BOXES is on it may also carry `_boxes`:
    `{db_field: {page, x, y, w, h, source: "vision"}}` in PDF points on the page
    that was read — the same shape `field_bbox.compute_field_bboxes` produces,
    for the fields where the model pointed at a place and every read agreed. It
    is normal for this key to be absent, or to cover only some of the fields;
    that is the honest outcome and the caller must treat a missing box as "not
    located" rather than filling one in.

    `cusdec_page` is 0-based. If None, auto-locate the most CUSDEC-like page:
    render candidate pages (scanned/low-text first) and pick the read that yields
    a tax block (preferring a CORE duty/CT/AT), bounded by VISION_RESCUE_MAX_PAGES.
    Returns None if no OpenRouter key is configured.
    """
    if not fitz or not pdf_path or not _has_key():
        return None

    doc = None
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return None

    try:
        # ── Explicit page ────────────────────────────────────────────────
        if cusdec_page is not None:
            if not (0 <= cusdec_page < doc.page_count):
                return None
            return _read_page(doc, cusdec_page, VISION_RESCUE_VOTES)

        # ── Auto-locate ──────────────────────────────────────────────────
        # Search with ONE read per candidate — finding the page is a yes/no question
        # and does not need agreement. Only the page that wins is re-read to vote,
        # so the extra calls are spent on the page whose values are kept.
        order = _candidate_order(doc)[:max(1, VISION_RESCUE_MAX_PAGES)]
        best = None            # best "usable but no tax block" fallback
        best_taxed = None      # best read that has a tax block (no core tax yet)
        for idx in order:
            fields = _read_page(doc, idx, 1)
            if fields is None:
                continue
            # Ideal hit: a real CUSDEC tax table with a core duty/CT/AT — return now.
            if _has_core_tax(fields):
                if VISION_RESCUE_VOTES > 1:
                    voted = _read_page(doc, idx, VISION_RESCUE_VOTES)
                    if voted and _has_core_tax(voted):
                        voted["_page"] = idx + 1
                        return voted
                fields["_page"] = idx + 1
                return fields
            if _has_tax_block(fields) and best_taxed is None:
                best_taxed = fields
                best_taxed["_page"] = idx + 1
            if best is None:
                best = fields
                best["_page"] = idx + 1
        return best_taxed or best
    except Exception:
        return None
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass


# CLI self-test (no routing): python -m v11.tools.vision_rescue <pdf> [page0based]
if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m v11.tools.vision_rescue <pdf_path> [0based_page]")
        sys.exit(1)
    pg = int(sys.argv[2]) if len(sys.argv) > 2 else None
    res = vision_cusdec_fields(sys.argv[1], pg)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
