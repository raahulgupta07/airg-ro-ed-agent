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
    # WHERE on the page, when it is actually known. `page` is 1-based (matching
    # v11/tools/field_bbox.py); `bbox` is [x0, y0, x1, y1] in PDF points around
    # the value token; `row_bbox` spans the whole printed row (label + value),
    # which is what a legible crop needs. Only geometry-bound cells carry these —
    # a vision read on a scanned page has no coordinates, and the UI must show
    # "location unknown" rather than draw a box that was never measured.
    page: Optional[int] = None
    bbox: Optional[List[float]] = None
    row_bbox: Optional[List[float]] = None

    def as_dict(self):
        return {
            "value": self.value, "source": self.source,
            "confidence": self.confidence, "model": self.model,
            "status": self.status, "alternates": self.alternates,
            "note": self.note,
            "page": self.page, "bbox": self.bbox, "row_bbox": self.row_bbox,
        }


# The columns the fleet owns (Excel 'AI results' schema, trimmed to the scored set).
COLUMNS = [
    "declaration_no",            # business rule: First-approval No. when present
    "declaration_no_official",   # the doc's own 'Declaration No.' (may differ) — both kept
    "declaration_date",
    "arrival_date",              # ship arrival (page-1 header block)
    "release_order_date",        # customs decision block — team ledger "RO/ID Date"
    "completion_date",           # 'Declaration completion' in the decision block
    "importer_name",
    "consignor_name",           # overseas sender/exporter on the header block
    "invoice_number",
    "currency",                  # declaration currency (invoice currency)
    "exchange_rate",             # PRINTED "Exchange Rate (1)" — never derived
    "invoice_price_mmk",         # doc "Invoice price (MMK)" — accountant "Total Value"
    "invoice_price_fc",          # doc "Invoice price" foreign-currency line (THB/USD) — team ledger
    "freight_value",             # OGA "Freight" (invoice-currency; feeds CIF build-up)
    "insurance_value",           # OGA "Insurance" (invoice-currency)
    "adjustment_value",          # OGA "Adjustment value" (AD; signed, invoice-currency)
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
