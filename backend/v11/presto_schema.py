"""V12 "Presto" — strict output schema for the typed fast-path.

Phase 0: defined only (not yet wired). In Phase 1 the Presto extractor asks the
LLM for JSON conforming to PrestoResult (structured output / JSON schema), which
removes parse-retries and constrains hallucination. Field names match the
snake_case keys the merger + database.save_declarations/save_items already
consume, so a Presto result drops into the existing V11 save path unchanged.

Pydantic v2. All fields Optional so a partial extraction still validates (the
reconcile gate, not the schema, decides completeness).
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class PrestoItem(BaseModel):
    """One customs line item (matches the `items` table / merger item keys)."""
    item_name: Optional[str] = None
    hs_code: Optional[str] = None
    quantity: Optional[str] = None                 # e.g. "108 KG" (unit kept)
    invoice_unit_price: Optional[float] = None
    cif_unit_price: Optional[float] = None
    customs_value_mmk: Optional[float] = None       # used by the reconcile gate
    customs_duty_rate: Optional[float] = None        # fraction, e.g. 0.15
    commercial_tax_pct: Optional[float] = None       # fraction, e.g. 0.05
    origin: Optional[str] = None                     # ISO-2, e.g. "IT"
    currency: Optional[str] = None
    exchange_rate: Optional[float] = None


class PrestoDeclaration(BaseModel):
    """Customs declaration header (matches the `declarations` table keys)."""
    declaration_no: Optional[str] = None
    declaration_date: Optional[str] = None           # ISO yyyy-mm-dd
    importer_name: Optional[str] = None
    consignor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_number_customs: Optional[str] = None
    invoice_number_commercial: Optional[str] = None
    currency: Optional[str] = None
    currency_2: Optional[str] = None
    exchange_rate: Optional[float] = None
    invoice_price: Optional[float] = None
    total_customs_value: Optional[float] = None       # reconcile anchor
    customs_duty: Optional[float] = None
    commercial_tax: Optional[float] = None
    advance_income_tax: Optional[float] = None
    security_fee: Optional[float] = None
    maccs_service_fee: Optional[float] = None
    exemption: Optional[float] = None


class PrestoResult(BaseModel):
    """Full Presto extraction: header + line items + a self-reported format.

    `document_format` mirrors V7 ("MACCS" / "CUSDEC1"); `field_confidence` lets
    the extractor flag low-trust fields so the math-gate / verifier (Phase 2)
    can target re-checks instead of re-running everything.
    """
    declaration: PrestoDeclaration = Field(default_factory=PrestoDeclaration)
    items: List[PrestoItem] = Field(default_factory=list)
    document_format: Optional[str] = None
    field_confidence: Optional[float] = None          # 0..1 overall self-estimate

    def to_pipeline_dict(self) -> dict:
        """Shape Presto output like a V7/merger result for the existing save path."""
        return {
            "declaration": self.declaration.model_dump(exclude_none=False),
            "items": [it.model_dump(exclude_none=False) for it in self.items],
            "document_format": self.document_format,
        }


# JSON Schema handed to the model for structured output (Phase 1 will use this).
PRESTO_JSON_SCHEMA = PrestoResult.model_json_schema()
