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
VISION_RESCUE_MODEL = os.getenv(
    "VISION_RESCUE_MODEL", "google/gemini-3-flash-preview")
# Scanned CUSDEC text is fine printed — 250 DPI reads it without ballooning the
# image (kept ≤ 2200px longest edge like Scribe).
VISION_RESCUE_DPI = int(os.getenv("VISION_RESCUE_DPI", "250"))
# When auto-locating the CUSDEC page, cap how many candidate pages we'll spend a
# vision call on before giving up (bounds cost on large bundled PDFs).
VISION_RESCUE_MAX_PAGES = int(os.getenv("VISION_RESCUE_MAX_PAGES", "6"))
VISION_RESCUE_TIMEOUT = int(os.getenv("VISION_RESCUE_TIMEOUT", "120"))

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
}

# Numeric fields to coerce (strip commas, cast to float). declaration_no /
# currency / declaration_date stay as strings.
_NUMERIC_KEYS = {
    "exchange_rate", "total_customs_value",
    "import_export_customs_duty", "commercial_tax_ct", "advance_income_tax_at",
    "security_fee_sf", "maccs_service_fee_mf",
    "freight_value", "insurance_value", "adjustment_value",
}

PROMPT = """This is a SCANNED image of a Myanmar MACCS customs declaration
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
- total_customs_value = the total assessed customs value in MMK (the large figure
  the item customs values sum to).
- declaration_no = the MACCS declaration number (typically 12 digits).
- The tax block is usually a labelled table: IMPORT/EXPORT CUSTOMS DUTY,
  COMMERCIAL TAX, ADVANCED INCOME TAX, SECURITY FEE, MACCS SERVICE FEE — read each
  amount as a number; a dash "-" means null (not 0).
- freight_value / insurance_value / adjustment_value = the CIF build-up amounts in
  the INVOICE currency (not MMK); a dash "-" means null. adjustment_value is signed
  (negative for a deduction).
- is_cusdec = true only if THIS page is the customs declaration / release order
  carrying the tax block or the assessed total; false for licences / invoices /
  packing lists / blank pages.

Return ONLY the JSON object.
"""


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
    """Coerce a model value to float (strip commas/parens/currency), or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:
            return None
    s = str(v).strip()
    if not s or s in ("-", "—", "–"):
        return None
    neg = s.startswith("(") and s.endswith(")")  # accounting negative
    s = s.strip("()")
    s = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    if s in ("", "-", "."):
        return None
    try:
        f = float(s)
        return -f if (neg and f > 0) else f
    except Exception:
        return None


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
        {"type": "text", "text": PROMPT},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
    ]
    payload = {
        "model": VISION_RESCUE_MODEL,
        "messages": [{"role": "user", "content": parts}],
        "temperature": 0,
        "max_tokens": 2000,
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
        if dst == "declaration_date":
            out[dst] = _to_iso(val)
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

    # Reject non-positive tax/value noise (a model sometimes emits 0 for absent).
    for k in ("total_customs_value",) + _TAX_KEYS:
        if out.get(k) is not None and out[k] <= 0:
            out[k] = None

    # Usable only if we recovered at least one high-value field.
    signal_keys = ("total_customs_value", "exchange_rate", "declaration_no") + _TAX_KEYS
    if not any(out.get(k) is not None for k in signal_keys):
        return None

    out["_vision"] = True
    ti = int(usage.get("prompt_tokens", 0) or 0)
    to = int(usage.get("completion_tokens", 0) or 0)
    out["tokens_in"] = ti
    out["tokens_out"] = to
    out["cost"] = _cost_from_usage(usage)
    return out


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
            b64 = _render_page_png_b64(doc, cusdec_page)
            if not b64:
                return None
            parsed, usage = _call_vision(b64)
            return _shape(parsed or {}, usage)

        # ── Auto-locate ──────────────────────────────────────────────────
        order = _candidate_order(doc)[:max(1, VISION_RESCUE_MAX_PAGES)]
        best = None            # best "usable but no tax block" fallback
        best_taxed = None      # best read that has a tax block (no core tax yet)
        for idx in order:
            b64 = _render_page_png_b64(doc, idx)
            if not b64:
                continue
            parsed, usage = _call_vision(b64)
            if not parsed:
                continue
            fields = _shape(parsed, usage)
            if fields is None:
                continue
            # Ideal hit: a real CUSDEC tax table with a core duty/CT/AT — return now.
            if _has_core_tax(fields):
                return fields
            if _has_tax_block(fields) and best_taxed is None:
                best_taxed = fields
            if best is None:
                best = fields
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
