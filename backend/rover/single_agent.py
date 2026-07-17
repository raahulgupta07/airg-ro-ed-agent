"""L2 — one vision call returns EVERY column with per-column evidence. Same rules
as the 4 family agents, merged into a single prompt. Images ingested once."""
from typing import Dict, List

from .schema import Cell
from . import llm

_PROMPT = """You are reading a Myanmar Customs Declaration (CUSDEC / Release-Order
notification). Extract the fields below. For EACH field return an object
{"value": <value or null>, "source": "<exact label+value you read>", "confidence": <0..1>}.

RULES (follow exactly):
- declaration_no: prefer the 'First approval declaration No.' when present, else the 'Declaration No.' Return digits only.
- declaration_no_official: the doc's own 'Declaration No.' field (the top one), even when a separate First-approval number exists. Digits only; null if absent.
- declaration_date: the 'Declaration date' as YYYY-MM-DD.
- importer_name: the Importer line.
- invoice_number: the 'Invoice' value (keep any A-/INV- prefix in the source).
- currency: the INVOICE currency (USD/THB/…) from 'Invoice price' / 'Exchange Rate (n) <CCY>'. NOT the consignor country.
- exchange_rate: the PRINTED 'Exchange Rate (1)' number, read verbatim. NEVER compute it.
- invoice_price_mmk: the 'Invoice price' value on the (MMK) line.
- total_customs_value: the 'Total customs value' MMK number (not the (USD) line). May differ from invoice_price_mmk — return as printed, do not reconcile.
- customs_value_usd: the 'Total customs value' amount on the (USD) line (the smaller USD figure just under the MMK customs value). null if absent.
- import_export_customs_duty (CD), commercial_tax_ct (CT), advance_income_tax_at (AT): each from its OWN labelled row in 'Taxes and fees'; 0 if the row shows 0; never shift values between rows.
- security_fee_sf: the 'Security' amount. maccs_service_fee_mf: the 'MACCS SERVICE FEE' (code MF). Keep these two SEPARATE.
- exemption_reduction: the 'Exemption/Reduction' value — do NOT confuse it with the 'Taxes and fees' total.

Also extract the PRODUCT line items (the goods rows, 13.No / 14.Hscode / 15.Description / 18.Quantity / 19.Value) as:
- "items": [ {"no": <row no>, "hs_code": "...", "description": "...", "quantity": <num>, "unit": "...", "value": <invoice value>} ]
Return one row per product. If a field in a row is absent use null.

Return ONLY one JSON object with the field names above plus "items". No prose."""

_COLS = ["declaration_no", "declaration_no_official", "declaration_date",
         "importer_name", "invoice_number", "currency", "exchange_rate",
         "invoice_price_mmk", "total_customs_value", "customs_value_usd",
         "import_export_customs_duty",
         "commercial_tax_ct", "advance_income_tax_at", "security_fee_sf",
         "maccs_service_fee_mf", "exemption_reduction"]


def run(image_content: List[dict], model: str = None) -> Dict[str, Cell]:
    model = model or llm.PRIMARY
    parsed, raw, usage, err = llm.call(model, _PROMPT, image_content, max_tokens=8000)
    out = {"_usage": usage, "_err": err}
    items = (parsed or {}).get("items") if isinstance(parsed, dict) else None
    out["_items"] = items if isinstance(items, list) else []
    for col in _COLS:
        c = Cell(column=col, model=model)
        obj = (parsed or {}).get(col) if isinstance(parsed, dict) else None
        if isinstance(obj, dict):
            c.value = obj.get("value")
            c.source = str(obj.get("source") or "")[:160]
            c.confidence = float(obj.get("confidence") or 0)
            c.status = "ok" if c.value not in (None, "") else "empty"
        elif obj is not None:
            c.value, c.confidence, c.status = obj, 0.5, "ok"
        else:
            c.status = "empty"
            if err:
                c.note = err
        out[col] = c
    return out
