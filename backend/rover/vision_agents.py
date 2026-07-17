"""Tier 1 — primary vision fleet. Family column-agents, all on ONE shared model
(so the doc images are ingested consistently; challenger diversity comes later).
Each family returns its columns as {value, source, confidence}. Business rules that
today's bake-off proved necessary are baked into every prompt."""
from typing import Dict

from .context import DocContext
from .schema import Cell
from . import llm

_EVIDENCE = (
    "For EVERY field return an object: "
    '{"value": <value or null>, "source": "<short quote of the exact label+value '
    'you read off the page>", "confidence": <0..1>}. '
    "If a field is truly absent, value null, confidence 0. "
    "Return ONLY one JSON object, no prose."
)

FAMILIES: Dict[str, Dict] = {
    "identity": {
        "columns": ["importer_name"],
        "prompt": "Read this Myanmar Customs Declaration (CUSDEC/MACCS)."
                  " Extract: importer_name.",
    },
    "invoice": {
        "columns": ["invoice_number"],
        "prompt": "From the Invoice block extract: invoice_number "
                  "(the 'Invoice' value, e.g. 'A- 960773210' -> return the core "
                  "number and keep any A-/INV- prefix in the source quote).",
    },
    "value_basis": {
        "columns": ["currency", "exchange_rate", "invoice_price_mmk",
                    "total_customs_value"],
        "prompt": (
            "Extract the value/currency block. RULES:\n"
            "- currency: the INVOICE currency of the declaration (e.g. USD/THB), "
            "from 'Invoice price' / 'Exchange Rate (n) <CCY>'. NOT the consignor "
            "country.\n"
            "- exchange_rate: the PRINTED 'Exchange Rate (1)' number. Read it "
            "verbatim. NEVER compute or derive it.\n"
            "- invoice_price_mmk: the 'Invoice price' value on the (MMK) line.\n"
            "- total_customs_value: the 'Total customs value' number (the MMK one, "
            "not the (USD) line). This is the assessed value and may differ from "
            "invoice_price_mmk — return it as printed, do not reconcile them."
        ),
    },
    "taxes": {
        "columns": ["import_export_customs_duty", "commercial_tax_ct",
                    "advance_income_tax_at", "security_fee_sf",
                    "maccs_service_fee_mf", "exemption_reduction"],
        "prompt": (
            "From the 'Taxes and fees' table extract each fee from its OWN labelled "
            "row; do NOT shift values between rows:\n"
            "- import_export_customs_duty (code CD)\n"
            "- commercial_tax_ct (code CT) — 0 if the CT row shows 0\n"
            "- advance_income_tax_at (code AT)\n"
            "- security_fee_sf (the 'Security' amount)\n"
            "- maccs_service_fee_mf (code MF, 'MACCS SERVICE FEE') — keep SEPARATE "
            "from Security\n"
            "- exemption_reduction (the 'Exemption/Reduction' value) — do NOT confuse "
            "it with the 'Taxes and fees' total."
        ),
    },
}


def run_family(ctx: DocContext, family: str, model: str = None) -> Dict[str, Cell]:
    model = model or llm.PRIMARY
    spec = FAMILIES[family]
    prompt = f"{spec['prompt']}\n\n{_EVIDENCE}"
    parsed, raw, usage, err = llm.call(model, prompt, ctx.image_content())
    out = {}
    for col in spec["columns"]:
        c = Cell(column=col, model=model)
        obj = (parsed or {}).get(col) if isinstance(parsed, dict) else None
        if isinstance(obj, dict):
            c.value = obj.get("value")
            c.source = str(obj.get("source") or "")[:160]
            c.confidence = float(obj.get("confidence") or 0)
            c.status = "ok" if c.value not in (None, "") else "empty"
        elif obj is not None:  # model returned a bare value
            c.value = obj
            c.confidence = 0.5
            c.status = "ok"
        else:
            c.status = "empty"
            if err:
                c.note = err
        out[col] = c
    out["_usage"] = usage
    out["_err"] = err
    return out
