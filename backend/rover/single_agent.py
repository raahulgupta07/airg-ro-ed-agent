"""L2 — one vision call returns EVERY column with per-column evidence. Same rules
as the 4 family agents, merged into a single prompt. Images ingested once."""
from typing import Dict, List

from .schema import Cell
from . import llm

_PROMPT = """You are reading a Myanmar Customs Declaration (CUSDEC / Release-Order
notification). Extract the fields below. For EACH field return an object
{"value": <value or null>, "source": "<exact label+value you read>", "confidence": <0..1>}.

RULES (follow exactly):
- declaration_no: ALWAYS the 'Declaration No.' printed at the TOP of the form (the
  header line beside Customs station / Section). Digits only. Do NOT use 'First
  approval declaration No.' — that identifies a DIFFERENT, earlier declaration that
  this one refers back to; only the top-of-form number identifies THIS document.
- declaration_no_official: the 'First approval declaration No.' when the form prints one — the earlier declaration this one clears. Digits only; null if absent.
- declaration_date: the 'Declaration date' as YYYY-MM-DD.
- arrival_date: the 'Arrival date' (ship arrival, near B/L and Conveyance) as YYYY-MM-DD; null if blank.
- release_order_date: the 'Release order' date from the customs-decision block on the LAST form page (near 'Declaration completion' / 'Examination completion', under the chief-officer section). YYYY-MM-DD; null if blank. This is NOT the page title 'Release order notification' — it is the dated 'Release order' row.
- completion_date: the 'Declaration completion' date from that same decision block, YYYY-MM-DD; null if blank.
- importer_name: the 'Importer' COMPANY NAME on the header block (the Myanmar buyer/consignee,
  e.g. 'PREMIUM DISTRIBUTION COMPANY LIMITED'). The line normally prints a registration code
  BEFORE the name — 'Importer  C162371223-000  PREMIUM DISTRIBUTION COMPANY LIMITED'. Return
  the COMPANY NAME ONLY: drop that leading code, never return the whole line. If no code is
  printed, return the name as it stands. This is NOT the consignor and NOT the customs agency.
  null if the row is blank or shows only a dash.
- importer_code: the importer registration code printed BEFORE the name on that same 'Importer'
  line, e.g. 'C162371223-000'. Copy it exactly as printed (keep the dash and any trailing -000).
  null if the line prints no code before the name.
- consignor_name: the 'Consignor' name on the header block (the overseas sender/exporter,
  e.g. 'ASIATIC MART HOLDING PTE LTD'). This is NOT the importer and NOT the customs agency.
  null if the row is blank or shows only a dash.
- invoice_number: the 'Invoice' value (keep any A-/INV- prefix in the source).
- currency: the INVOICE currency (USD/THB/…) from 'Invoice price' / 'Exchange Rate (n) <CCY>'. NOT the consignor country.
- exchange_rate: the PRINTED 'Exchange Rate (1)' number, read verbatim. NEVER compute it.
- invoice_price_mmk: the 'Invoice price' value on the (MMK) line.
- invoice_price_fc: the 'Invoice price' value on the FOREIGN-currency line (the amount printed just before '(MMK)', e.g. 'THB 1,118,431.80' or 'USD 49,887.18'). This is the invoice currency amount, NOT the MMK figure. null if only an MMK amount is printed.
- freight_value: the 'Freight' amount in the OGA / valuation block (invoice currency, e.g. THB). null if the row is blank or '-'.
- insurance_value: the 'Insurance' amount in that same block (invoice currency). null if blank or '-'.
- adjustment_value: the 'Adjustment value' amount (label 'Adjustment value AD - <CCY> - <n>'; invoice currency, keep the sign). This is the money value, NOT the small 'Adjustment' code integer. null if blank or '-'.
- total_customs_value: the 'Total customs value' MMK number (not the (USD) line). May differ from invoice_price_mmk — return as printed, do not reconcile.
- customs_value_usd: the 'Total customs value' amount on the (USD) line (the smaller USD figure just under the MMK customs value). null if absent.
- import_export_customs_duty (CD), commercial_tax_ct (CT), advance_income_tax_at (AT): each from its OWN labelled row in 'Taxes and fees'; 0 if the row shows 0; never shift values between rows.
- security_fee_sf: the 'Security' amount. maccs_service_fee_mf: the 'MACCS SERVICE FEE' (code MF). Keep these two SEPARATE.
- exemption_reduction: the 'Exemption/Reduction' value — do NOT confuse it with the 'Taxes and fees' total.

Also extract the PRODUCT line items as:
- "items": [ {"no": <row no>, "hs_code": "...", "description": "...", "quantity": <num>,
              "unit": "...", "value": <invoice-currency line value>,
              "unit_price": <invoice unit price>, "customs_value": <assessed MMK value>} ]
Return one row per product. If a field in a row is absent use null.

Read the items from the CUSDEC / release-order item block when it is present — the one that
reads 'No. 001  HS <code>' then 'Item name', 'Quantity (1)', 'Item value', 'Invoice unit price',
'Customs value'. Map it as: value <- 'Item value', unit_price <- 'Invoice unit price',
customs_value <- 'Customs value'.
  * 'Item value' and 'Invoice unit price' are in the INVOICE currency (THB/USD/…).
  * 'Customs value' is the assessed MMK figure for that row. It is a DIFFERENT UNIT and is
    usually far larger. NEVER copy 'Item value' into customs_value, and never multiply by the
    exchange rate to invent it — the assessed value may carry an uplift. If the row prints no
    'Customs value', return null for it.
Only if there is no CUSDEC item block, fall back to the Import Licence table
(13.No / 14.Hscode / 15.Description / 17.UnitPrice / 18.Quantity / 19.Value); there
19.Value is the invoice-currency amount, so set value from it and leave customs_value null.

Return ONLY one JSON object with the field names above plus "items". No prose."""

_COLS = ["declaration_no", "declaration_no_official", "declaration_date",
         "arrival_date", "release_order_date", "completion_date",
         "importer_name", "importer_code", "consignor_name", "invoice_number", "currency", "exchange_rate",
         "invoice_price_mmk", "invoice_price_fc",
         "freight_value", "insurance_value", "adjustment_value",
         "total_customs_value", "customs_value_usd",
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
