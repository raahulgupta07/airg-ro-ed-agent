"""V9 — Pydantic schemas for type-safe extraction output."""
from pydantic import BaseModel, Field
from typing import Optional, List

class Declaration(BaseModel):
    declaration_no: str = Field(..., description="Declaration No (string, not float)")
    declaration_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    importer_name: Optional[str] = None
    consignor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_number_customs: Optional[str] = Field(None, description="With 'A - ' prefix")
    invoice_number_commercial: Optional[str] = Field(None, description="Bare form")
    currency: Optional[str] = Field(None, description="3-letter ISO")
    currency_2: Optional[str] = None
    exchange_rate: Optional[float] = None
    invoice_price: Optional[float] = None
    total_customs_value: Optional[float] = None
    country_origin: Optional[str] = None
    customs_duty: float = 0
    commercial_tax: float = 0
    advance_income_tax: float = 0
    security_fee: float = 0
    maccs_service_fee: float = 0
    exemption: float = 0

class Item(BaseModel):
    item_name: str
    customs_duty_rate: float = 0
    quantity: Optional[str] = Field(None, description="e.g. '1234.56 KG'")
    invoice_unit_price: Optional[float] = None
    cif_unit_price: Optional[float] = None
    currency: Optional[str] = None
    commercial_tax_pct: float = 0.05
    exchange_rate: Optional[float] = None
    hs_code: Optional[str] = None
    origin: Optional[str] = None
    customs_value_mmk: Optional[float] = None

class ExtractionResult(BaseModel):
    document_format: str = Field(..., description="CUSDEC1 or MACCS")
    declaration: Declaration
    items: List[Item] = []
    pages_kept: List[int] = []
    anomalies: List[str] = []
    confidence: dict = {}
    duration_seconds: float = 0
    cost_usd: float = 0
    postfix_notes: List[str] = []
