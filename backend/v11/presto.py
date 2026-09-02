"""V12 "Presto" — typed fast-path extractor.

For digital (text-layer) typed MACCS pages: pull the exact text + word boxes
with PyMuPDF, then make ONE schema-constrained LLM call to structure it into a
declaration + line items. No image render, no per-page vision, no verifier on
the happy path — the reconcile gate (Phase 2) decides if escalation is needed.

Returns a dict shaped like the V7 pipeline result, so it drops straight into the
existing V11 merge + save path. NOT wired into routing yet (Phase 3); callable
directly for shadow-mode comparison (Phase 4).
"""
import json
import re
import time
from typing import Dict, List, Optional

import fitz
import requests

import config
from v11.presto_schema import PrestoResult

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Pricing fallback (per 1M tokens) for gemini-3-flash; OpenRouter usage.cost
# is preferred when present.
_FLASH_IN_PER_M = 0.50
_FLASH_OUT_PER_M = 3.00


PROMPT = """You are a Myanmar customs declaration parser. You are given the EXACT
text extracted from the PDF's text layer (already accurate characters — do NOT
re-read or guess digits; only organize what is given).

Extract the declaration header and every line item into strict JSON matching this
shape (omit nothing you can find; use null when truly absent):

{
  "declaration": {
    "declaration_no","declaration_no_official","declaration_date",
    "importer_name","consignor_name",
    "invoice_number","invoice_number_customs","invoice_number_commercial",
    "currency","currency_2","exchange_rate","invoice_price",
    "invoice_price_fc","invoice_price_mmk",
    "freight_value","freight_currency","insurance_value","insurance_currency",
    "adjustment_value","adjustment_currency","total_customs_value",
    "customs_duty","commercial_tax","advance_income_tax","security_fee",
    "maccs_service_fee","exemption"
  },
  "items": [ {
    "item_name","hs_code","quantity","invoice_unit_price","cif_unit_price",
    "customs_value_mmk","customs_duty_rate","commercial_tax_pct","origin",
    "currency","exchange_rate"
  } ],
  "document_format": "MACCS" or "CUSDEC1",
  "field_confidence": 0.0-1.0
}

Rules:
- Numbers as numbers (strip thousands separators). Rates as fractions (15% -> 0.15).
- Keep quantity with its unit as a string (e.g. "108 KG").
- Include ALL line items — do not merge or drop rows.

Per-field guidance (read carefully — these columns are easy to confuse):
- declaration_no: ALWAYS the "Declaration No." printed at the TOP of the form, in
  the header line beside Customs station / Section. Digits only.
  Do NOT use "First approval declaration No." — that is a DIFFERENT, earlier
  declaration that this one refers back to. On an Ex-bond release the two numbers
  differ, and only the top-of-form one identifies THIS document.
  (No example numbers are given here on purpose: quoting a specific wrong value in
  an instruction makes a model more likely to return that very value.)
  Record the first-approval number separately in declaration_no_official if the form
  prints one, and null when it does not.
- total_customs_value: the "Total customs value" figure in the HEADER value block
  (the MMK one, not the "(USD)" line under it). It equals the sum of the items'
  "Customs value". Do NOT take the amount printed beside "Commercial tax" or
  "SPECIFIC GOODS TAX" in the item block — that is the TAX BASE, which is the
  customs value PLUS the customs duty and is therefore always larger. On a real
  declaration the header said 198,450,000 while the commercial-tax base said
  204,403,500 (= 198,450,000 + 5,953,500 duty); taking the latter is wrong.
- origin: ISO 3166-1 alpha-2 country CODE, not the name. ITALY->IT, AUSTRIA->AT,
  THAILAND->TH, CHINA->CN, MYANMAR->MM, etc.
- invoice_unit_price: the per-unit price in the INVOICE currency (the small number,
  same currency as `currency`, e.g. 356.323 THB).
- cif_unit_price: the CIF per-unit price in the INVOICE currency (same scale as
  invoice_unit_price, e.g. 356.323). If no separate CIF unit price is shown, set
  it equal to invoice_unit_price. DO NOT put any MMK figure here.
- An item row has TWO MMK numbers: a per-unit "unit price of customs value"
  (e.g. 20452.37) and the item TOTAL "customs value" (= that unit price ×
  quantity, e.g. 20452.37 × 108 = 2208855.96).
- customs_value_mmk: the item TOTAL customs value in MMK — the LARGER number
  (unit-MMK × quantity, e.g. 2208855.96). NEVER the per-unit MMK price. Sanity:
  the sum of all items' customs_value_mmk must roughly equal the declaration's
  total_customs_value.
- invoice_number_customs / invoice_number_commercial: the invoice number(s),
  typically like "AM-PD-012/2024". NOT a Bill of Lading / container / BoL number
  (e.g. TCLBIL...). If only one invoice number is shown, use it for both.
- currency: the invoice currency code (e.g. THB); currency_2 the secondary (e.g. USD).
- invoice_price_fc: the "Invoice price" amount on the FOREIGN-currency line — the
  figure printed just before the "(MMK)" line, e.g. "A - C&F - CNY - 82,022.1072".
- invoice_price_mmk: the "Invoice price" amount on the "(MMK)" line, e.g.
  24,307,579.55. These are the SAME money in two units. Return BOTH when both are
  printed; null for either that is absent. Do not put one in the other's field.

- THE BUILD-UP LINES CARRY THEIR OWN CURRENCY CODE. Freight, Insurance and
  Adjustment value each print a code immediately before the amount, e.g.
  "Freight - CNY - 1,234.5", "Insurance E - MMK - 267,383.37",
  "Adjustment value AD - CNY - 1,051.894". READ THAT CODE. Do NOT assume the
  invoice currency: on real declarations Insurance is frequently already in MMK
  while Adjustment on the same form is in the invoice currency. Report each
  amount exactly as printed, and report the code you read in
  freight_currency / insurance_currency / adjustment_currency.
- freight_value: the freight / shipping cost. Labelled Freight / FRT / Carriage.
- insurance_value: the insurance cost. Labelled Insurance / INS.
- adjustment_value: other additions or deductions to the customs value — other
  charges, discounts. SIGNED: negative for a deduction. This is the money amount
  next to "Adjustment value", NOT the small integer code printed beside the word
  "Adjustment" on its own line.
- A blank line, a dash, or an absent field is NULL — never 0. Return 0 ONLY when
  the form actually prints a zero. "Not shown" and "shown as zero" are different
  facts and the reviewer needs to tell them apart: a CIF/CIP shipment with no
  freight line has null freight, whereas an ex-bond entry really can print
  "IMPORT/EXPORT CUSTOMS DUTY  0".
- exchange_rate: the rate converting the INVOICE currency (`currency`) to MMK.
  The form may print SEVERAL rates (e.g. a THB rate ~57 and a USD rate ~2100) —
  pick the one for `currency`. Self-check, converting each build-up line from ITS
  OWN currency: invoice_price_mmk + (each of freight/insurance/adjustment, already
  MMK if its code says MMK, else × exchange_rate) must be approximately
  total_customs_value. If it fails badly, you picked the wrong rate, mis-read a
  value, or assumed the wrong currency for one of the lines.

Return ONLY the JSON object.

PDF TEXT:
"""


def _extract_words(pdf_path: str, pages: Optional[List[int]] = None):
    """Return (full_text, words_by_page).

    words_by_page[page] = list of (x0,y0,x1,y1, word). 1-based page numbers.
    """
    doc = fitz.open(str(pdf_path))
    want = set(pages) if pages else None
    chunks = []
    words_by_page: Dict[int, list] = {}
    for i, page in enumerate(doc, 1):
        if want and i not in want:
            continue
        try:
            words = page.get_text("words") or []  # (x0,y0,x1,y1,word,block,line,word_no)
        except Exception:
            words = []
        words_by_page[i] = [(w[0], w[1], w[2], w[3], w[4]) for w in words]
        try:
            txt = page.get_text() or ""
        except Exception:
            txt = ""
        chunks.append(f"\n----- PAGE {i} -----\n{txt}")
    doc.close()
    return "".join(chunks), words_by_page


def _parse_json(raw: str):
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
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in (s, re.sub(r",(\s*[\}\]])", r"\1", s)):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


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
    return round(ti / 1_000_000 * _FLASH_IN_PER_M + to / 1_000_000 * _FLASH_OUT_PER_M, 6)


def _norm_box(b):
    return {"x": round(b[0], 2), "y": round(b[1], 2),
            "w": round(b[2] - b[0], 2), "h": round(b[3] - b[1], 2)}


def _find_bbox(value, words_by_page) -> Optional[dict]:
    """Best-effort: locate a value's words and return a bbox. Cheap, optional."""
    if value in (None, ""):
        return None
    target = re.sub(r"[\s,]", "", str(value)).lower()
    if not target:
        return None
    for page, words in words_by_page.items():
        toks = [(re.sub(r"[\s,]", "", w[4]).lower(), w[:4]) for w in words]
        for tok, box in toks:
            if tok and (tok == target or (len(target) >= 4 and target in tok)):
                bb = _norm_box(box)
                bb["page"] = page
                return bb
    return None


def _primary_hints(importer_name: Optional[str]) -> str:
    """Flag-gated learned-correction hint block (LEARN_FEWSHOT_PRIMARY). Never
    raises — an inert/empty learner degrades to no injection."""
    try:
        from v11.learn import fewshot
        return fewshot.primary_hint_block(importer_name) or ""
    except Exception:
        return ""


def run(pdf_path: str, pages: Optional[List[int]] = None,
        model: Optional[str] = None, importer_name: Optional[str] = None) -> Dict:
    """Extract a typed/digital PDF via the text layer + one LLM call.

    Returns a V7-shaped dict: declaration, items, document_format, cost_usd,
    tokens_in/out, cost_breakdown, duration_seconds, plus presto diagnostics.

    When LEARN_FEWSHOT_PRIMARY is on, a values-free attention list of the fields
    reviewers most often correct (+ per-importer value hints if importer_name is
    known) is prepended to the prompt — the Phase-1 self-improvement loop.
    """
    t0 = time.time()
    model = model or config.EXTRACTION_MODEL
    full_text, words_by_page = _extract_words(pdf_path, pages)

    hints = _primary_hints(importer_name)
    prompt = (hints + "\n" + PROMPT) if hints else PROMPT

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt + full_text}],
        "temperature": 0,
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},
    }

    raw, usage = "", {}
    err = None
    for attempt in range(3):
        try:
            r = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {config.API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=120,
            )
            if r.status_code == 200:
                body = r.json()
                raw = body["choices"][0]["message"]["content"]
                usage = body.get("usage", {}) or {}
                break
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            err = str(e)
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))

    parsed = _parse_json(raw) or {}
    try:
        result = PrestoResult.model_validate(parsed)
    except Exception:
        result = PrestoResult()

    out = result.to_pipeline_dict()
    items = out.get("items") or []
    decl = out.get("declaration") or {}

    # Field bboxes (best-effort, from word positions we already have).
    bboxes = {"declaration": {}, "items": {}}
    for f in ("declaration_no", "total_customs_value", "exchange_rate", "invoice_price"):
        bb = _find_bbox(decl.get(f), words_by_page)
        if bb:
            bboxes["declaration"][f] = bb
    for idx, it in enumerate(items):
        bb = _find_bbox(it.get("customs_value_mmk"), words_by_page)
        if bb:
            bboxes["items"][str(idx)] = {"customs_value_mmk": bb}

    cost = _cost_from_usage(usage)
    ti = int(usage.get("prompt_tokens", 0) or 0)
    to = int(usage.get("completion_tokens", 0) or 0)

    return {
        "pipeline_version": "presto",
        "pipeline_mode": "v12_presto",
        "declaration": decl,
        "items": items,
        "document_format": out.get("document_format") or "MACCS",
        "items_count": len(items),
        "duration_seconds": round(time.time() - t0, 2),
        "cost_usd": cost,
        "cost": cost,
        "tokens_in": ti,
        "tokens_out": to,
        "cost_breakdown": [{
            "step": "presto_structure", "model": model,
            "input_tokens": ti, "output_tokens": to, "cost": cost,
            "source": "openrouter", "branch": "presto",
        }],
        "field_bboxes": bboxes,
        "presto": {
            "pages": sorted(words_by_page.keys()),
            "field_confidence": result.field_confidence,
            "text_chars": len(full_text),
            "fewshot_injected": bool(hints),
            "error": err,
        },
    }


# CLI self-test (no routing): python -m v11.presto <pdf> [page,page,...]
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m v11.presto <pdf_path> [comma,sep,pages]")
        sys.exit(1)
    pg = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else None
    res = run(sys.argv[1], pg)
    print(json.dumps({k: v for k, v in res.items() if k != "field_bboxes"},
                     indent=2, ensure_ascii=False, default=str))
