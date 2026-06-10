#!/usr/bin/env python3
"""
Pydantic schemas for RO-ED AI Agent API
"""

from pydantic import BaseModel, Field
from typing import Optional, List


# =============================================================================
# AUTH
# =============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    role: str
    is_active: bool = True
    must_change_password: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


# =============================================================================
# USERS
# =============================================================================

class CreateUserRequest(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=4)
    display_name: str = ""
    role: str = "user"


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = None


class UserListResponse(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    role: str
    is_active: bool
    created_at: Optional[str] = None
    last_login: Optional[str] = None


# =============================================================================
# JOBS
# =============================================================================

class JobResponse(BaseModel):
    job_id: str
    pdf_name: str
    pdf_hash: Optional[str] = None
    pdf_size: Optional[int] = None
    total_pages: Optional[int] = None
    text_pages: Optional[int] = None
    image_pages: Optional[int] = None
    status: str
    user_id: Optional[int] = None
    username: Optional[str] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    processing_time_seconds: Optional[float] = None
    cost_usd: Optional[float] = None
    accuracy_percent: Optional[float] = None
    error_message: Optional[str] = None
    pipeline_mode: Optional[str] = None
    pipeline_version: Optional[str] = None
    review_status: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_notes: Optional[str] = None
    edits_count: Optional[int] = 0
    document_type: Optional[str] = None
    field_bboxes: Optional[dict] = None
    tokens_in: Optional[int] = 0
    tokens_out: Optional[int] = 0
    model_used: Optional[str] = None
    processed_at: Optional[str] = None


class JobDetailResponse(JobResponse):
    items: List[dict] = []
    declarations: List[dict] = []
    logs: List[dict] = []
    pdf_metadata: Optional[dict] = None
    cross_validation: Optional[dict] = None


class DuplicateCheckResponse(BaseModel):
    is_duplicate: bool
    existing_job: Optional[JobResponse] = None


# =============================================================================
# ITEMS (Format 1 — 6 fields per product)
# =============================================================================

class ItemResponse(BaseModel):
    id: int
    job_id: str
    item_name: Optional[str] = None
    customs_duty_rate: Optional[float] = None
    quantity: Optional[str] = None
    invoice_unit_price: Optional[str] = None
    cif_unit_price: Optional[str] = None
    commercial_tax_percent: Optional[float] = None
    exchange_rate: Optional[str] = None
    hs_code: Optional[str] = None
    origin_country: Optional[str] = None
    customs_value_mmk: Optional[float] = None
    created_at: Optional[str] = None


# =============================================================================
# DECLARATIONS (Format 2 — 16 fields)
# =============================================================================

class DeclarationResponse(BaseModel):
    id: int
    job_id: str
    declaration_no: Optional[str] = None
    declaration_date: Optional[str] = None
    importer_name: Optional[str] = None
    consignor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_number_customs_declaration: Optional[str] = None
    invoice_number_commercial_invoice: Optional[str] = None
    invoice_price: Optional[float] = None
    currency: Optional[str] = None
    exchange_rate: Optional[float] = None
    currency_2: Optional[str] = None
    freight_value: Optional[float] = None
    insurance_value: Optional[float] = None
    adjustment_value: Optional[float] = None
    total_customs_value: Optional[float] = None
    import_export_customs_duty: Optional[float] = None
    commercial_tax_ct: Optional[float] = None
    advance_income_tax_at: Optional[float] = None
    security_fee_sf: Optional[float] = None
    maccs_service_fee_mf: Optional[float] = None
    exemption_reduction: Optional[float] = None
    created_at: Optional[str] = None


# =============================================================================
# SEARCH
# =============================================================================

class SearchRequest(BaseModel):
    query: str = ""
    pdf_name: Optional[str] = None
    page_type: Optional[str] = None
    limit: int = 100


class SearchResultResponse(BaseModel):
    id: int
    job_id: str
    pdf_name: Optional[str] = None
    page_number: int
    page_type: Optional[str] = None
    content: Optional[str] = None
    snippet: Optional[str] = None
    char_count: int = 0


# =============================================================================
# ACTIVITY LOGS
# =============================================================================

class ActivityLogResponse(BaseModel):
    id: int
    user_id: Optional[int] = None
    username: str
    action: str
    detail: Optional[str] = None
    created_at: Optional[str] = None


# =============================================================================
# STATS
# =============================================================================

class StatsResponse(BaseModel):
    total_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    total_items: int = 0
    total_declarations: int = 0
    avg_accuracy: float = 0.0
    total_cost: float = 0.0


# =============================================================================
# PIPELINE WEBSOCKET MESSAGES
# =============================================================================

class PipelineStepMessage(BaseModel):
    """Sent over WebSocket during pipeline execution."""
    step: int
    name: str
    status: str  # "running", "done", "error"
    detail: Optional[str] = None
    duration: Optional[float] = None
    cost: Optional[float] = None


# =============================================================================
# KEYCLOAK / OIDC
# =============================================================================

class OIDCConfigResponse(BaseModel):
    """Returned by GET /api/auth/config to tell frontend which auth mode to use."""
    mode: str  # "local" or "keycloak"
    auth_url: Optional[str] = None
    token_url: Optional[str] = None
    logout_url: Optional[str] = None
    client_id: Optional[str] = None


class TokenExchangeRequest(BaseModel):
    """Frontend sends auth code + PKCE verifier for Keycloak token exchange."""
    code: str
    redirect_uri: str
    code_verifier: str


class TokenExchangeResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: int = 300
    token_type: str = "Bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class KeycloakSettingsRequest(BaseModel):
    realm_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    admin_role: str = "admin"
    enabled: bool = False


class KeycloakSettingsResponse(BaseModel):
    realm_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    admin_role: str = "admin"
    enabled: bool = False
    updated_at: Optional[str] = None


class KeycloakTestResponse(BaseModel):
    success: bool
    message: str
    keys_found: int = 0


# =============================================================================
# GROUPS / RBAC
# =============================================================================

class GroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    page_agent: bool = True
    page_history: bool = True
    page_items: bool = True
    page_declarations: bool = True
    page_costs: bool = True
    page_settings: bool = False
    action_run_pipeline: bool = True
    action_upload_pdf: bool = True
    action_download_excel: bool = True
    action_delete_jobs: bool = False
    action_export_data: bool = True
    data_scope: str = "own"
    member_ids: List[int] = []


class GroupResponse(BaseModel):
    id: int
    name: str
    description: str = ""
    page_agent: bool = True
    page_history: bool = True
    page_items: bool = True
    page_declarations: bool = True
    page_costs: bool = True
    page_settings: bool = False
    action_run_pipeline: bool = True
    action_upload_pdf: bool = True
    action_download_excel: bool = True
    action_delete_jobs: bool = False
    action_export_data: bool = True
    data_scope: str = "own"
    member_count: int = 0
    members: List[dict] = []
    created_at: Optional[str] = None


class PermissionsResponse(BaseModel):
    pages: dict = {}
    actions: dict = {}
    data_scope: str = "own"


class EnhancedUserResponse(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    role: str
    email: Optional[str] = None
    auth_type: str = "local"
    is_active: bool = True
    group: Optional[dict] = None
    permissions: Optional[dict] = None
    created_at: Optional[str] = None
    last_login: Optional[str] = None


class AssignGroupRequest(BaseModel):
    group_id: Optional[int] = None
