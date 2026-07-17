"""Evidence contract. Every column value — deterministic or LLM — is a Cell:
a value plus WHERE it came from and how sure. No source => value is rejected."""
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Cell:
    column: str
    value: Any = None
    source: str = ""            # page/line/box text proving the value
    confidence: float = 0.0
    model: str = ""             # "deterministic" | "grok-4.5" | "gpt-..." ...
    status: str = "ok"          # ok | suspect | review | empty
    alternates: List[Any] = field(default_factory=list)
    note: str = ""

    def as_dict(self):
        return {
            "value": self.value, "source": self.source,
            "confidence": self.confidence, "model": self.model,
            "status": self.status, "alternates": self.alternates,
            "note": self.note,
        }


# The columns the fleet owns (Excel 'AI results' schema, trimmed to the scored set).
COLUMNS = [
    "declaration_no",            # business rule: First-approval No. when present
    "declaration_no_official",   # the doc's own 'Declaration No.' (may differ) — both kept
    "declaration_date",
    "importer_name",
    "invoice_number",
    "currency",                  # declaration currency (invoice currency)
    "exchange_rate",             # PRINTED "Exchange Rate (1)" — never derived
    "invoice_price_mmk",         # doc "Invoice price (MMK)" — accountant "Total Value"
    "total_customs_value",       # doc "Total customs value" (assessed, w/ uplift)
    "import_export_customs_duty",
    "commercial_tax_ct",
    "advance_income_tax_at",
    "security_fee_sf",
    "maccs_service_fee_mf",
    "exemption_reduction",
]


def blank_record() -> dict:
    return {c: Cell(column=c, status="empty") for c in COLUMNS}
