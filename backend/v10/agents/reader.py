"""V9 — Holistic PDF reader (Gemini Pro Latest).
Single-call extraction with native PDF input. Outputs draft JSON for the
master agent to refine."""
import base64
import json
import re
from pathlib import Path
from typing import Dict, Optional

from agno.agent import Agent
from agno.models.openrouter import OpenRouter

from v10.config import READER_MODEL, OPENROUTER_API_KEY


READER_SYSTEM = """You are a Myanmar Customs document reader. Extract every field
from CUSDEC-1 / MACCS Release Order PDFs.

CRITICAL RULES
1. Read HANDWRITTEN numbers LITERALLY — exact pen-strokes, no inference.
2. Decimals: actual dot stroke. "60.8090" stays 60.8090.
3. INVOICE NUMBER: hunt in Box 38 description ("Invoice No: AM-PD-XXX/YYYY").
   Box 19 is B/L, NOT invoice.
4. CROSS-FIELD ARITHMETIC self-check:
   - Total Customs Value (Box 44) ÷ F.E. Rate (Box 14) ≈ Invoice Price
   - 15% × CV ≈ Customs Duty
   - 5% × (CV + Duty) ≈ Commercial Tax
   - 2% × CV ≈ MACCS Service Fee
5. CURRENCY: 3-letter ISO. Rate band: THB 40-110, USD 1300-5500, KRW 1.0-5.0,
   JPY 10-50, CNY 200-900, EUR 1500-6500.
6. ORIGIN: Box 22 + Box 42 + truck receipt — cross-vote.
7. document_format = "CUSDEC1" (handwritten) or "MACCS" (typed).

Output ONLY valid JSON, no markdown fences, no preamble."""


READER_PROMPT = """Read this PDF and return JSON below. Each field as
{value, confidence (high|med|low), evidence (max 8 words)}.

{
  "document_format": "CUSDEC1|MACCS",
  "declaration": {
    "declaration_no": {...},
    "declaration_date": {...},
    "importer_name": {...},
    "consignor_name": {...},
    "invoice_number": {...},
    "invoice_number_customs": {...},
    "invoice_number_commercial": {...},
    "currency": {...},
    "currency_2": {...},
    "exchange_rate": {...},
    "invoice_price": {...},
    "total_customs_value": {...},
    "country_origin": {...},
    "customs_duty": {...},
    "commercial_tax": {...},
    "advance_income_tax": {...},
    "security_fee": {...},
    "maccs_service_fee": {...},
    "exemption": {...}
  },
  "items": [{
    "item_name": {...},
    "customs_duty_rate": {...},
    "quantity": {...},
    "invoice_unit_price": {...},
    "cif_unit_price": {...},
    "currency": {...},
    "commercial_tax_pct": {...},
    "exchange_rate": {...},
    "hs_code": {...},
    "origin": {...},
    "customs_value_mmk": {...}
  }],
  "anomalies": ["..."],
  "uncertain_fields": ["dotted.path.to.field"]
}

For unfilled: {"value": null, "confidence": "low", "evidence": "n/a"}.
Output JSON only."""


def _build_agent() -> Agent:
    return Agent(
        name="HolisticReader",
        model=OpenRouter(id=READER_MODEL, api_key=OPENROUTER_API_KEY),
        instructions=READER_SYSTEM,
        markdown=False,
    )


def read_pdf(pdf_path: str) -> Optional[Dict]:
    """Send PDF directly to OpenRouter (Agno's adapter doesn't carry type:file).
    Returns parsed (raw, with conf/evidence) JSON or None on failure."""
    import requests, time
    API_URL = "https://openrouter.ai/api/v1/chat/completions"
    pdf_b64 = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    payload = {
        "model": READER_MODEL,
        "messages": [
            {"role": "system", "content": READER_SYSTEM},
            {"role": "user", "content": [
                {"type": "file",
                 "file": {"filename": Path(pdf_path).name,
                          "file_data": f"data:application/pdf;base64,{pdf_b64}"}},
                {"type": "text", "text": READER_PROMPT},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 32000,
    }
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=300)
            dt = time.time() - t0
            print(f"[Reader] HTTP {r.status_code} {dt:.1f}s ({READER_MODEL})")
            if r.status_code == 200:
                j = r.json()
                if "choices" in j and j["choices"]:
                    raw = j["choices"][0]["message"]["content"]
                    parsed = _parse_json(raw)
                    if parsed:
                        parsed["_usage"] = j.get("usage", {})
                        return parsed
                    print(f"[Reader] JSON parse fail. raw[:300]: {raw[:300]}")
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                print(f"[Reader] body: {r.text[:300]}")
        except Exception as e:
            print(f"[Reader] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def flatten(parsed: Dict) -> Dict:
    """Convert {value, confidence, evidence} envelope to flat values."""
    if not parsed:
        return {}
    fmt = parsed.get("document_format")
    if isinstance(fmt, dict):
        fmt = fmt.get("value")
    out = {"document_format": fmt, "declaration": {}, "items": [],
           "anomalies": parsed.get("anomalies", []),
           "uncertain_fields": parsed.get("uncertain_fields", [])}
    for k, v in (parsed.get("declaration") or {}).items():
        out["declaration"][k] = v.get("value") if isinstance(v, dict) else v
    for it in (parsed.get("items") or []):
        flat_it = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in it.items()}
        out["items"].append(flat_it)
    return out


def _parse_json(raw: str) -> Optional[Dict]:
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
    for cand in [s,
                 re.sub(r"(\}\s*)\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
                 re.sub(r"([\d\"\}\]])\s*\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
                 re.sub(r",(\s*[\}\]])", r"\1", s)]:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None
