#!/usr/bin/env python3
"""Settings routes for RO-ED AI Agent — Keycloak configuration (admin only)"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
import config
import database
from middleware import require_admin, get_current_user
from schemas import KeycloakSettingsRequest, KeycloakSettingsResponse, KeycloakTestResponse

router = APIRouter()


# ─── Auto-approve settings ──────────────────────────────────────

class AutoApproveSettings(BaseModel):
    enabled: bool = False
    threshold: float = Field(0.95, ge=0.0, le=1.0)


@router.get("/auto-approve")
async def get_auto_approve(current_user: dict = Depends(get_current_user)):
    """Return current auto-approve config (enabled flag + confidence threshold)."""
    enabled = (database.get_app_setting("auto_approve_enabled", "false") or "false").lower() == "true"
    try:
        threshold = float(database.get_app_setting("auto_approve_threshold", "0.95") or 0.95)
    except (TypeError, ValueError):
        threshold = 0.95
    last_run = database.get_app_setting("auto_approve_last_run", None)
    last_count = database.get_app_setting("auto_approve_last_count", None)
    return {
        "enabled": enabled,
        "threshold": threshold,
        "last_run": last_run,
        "last_count": int(last_count) if last_count and str(last_count).isdigit() else 0,
    }


@router.put("/auto-approve")
async def set_auto_approve(body: AutoApproveSettings, admin: dict = Depends(require_admin)):
    """Update auto-approve config (admin only)."""
    if body.threshold < 0.0 or body.threshold > 1.0:
        raise HTTPException(400, "threshold must be between 0 and 1")
    database.set_app_setting("auto_approve_enabled", body.enabled, updated_by=admin.get("username", "admin"))
    database.set_app_setting("auto_approve_threshold", body.threshold, updated_by=admin.get("username", "admin"))
    database.log_activity(
        admin["id"], admin["username"], "UPDATE_SETTINGS",
        f"auto_approve enabled={body.enabled} threshold={body.threshold}",
    )
    return {"enabled": body.enabled, "threshold": body.threshold}


# ─── Engine availability (super-admin controls which pipelines users can pick) ──

import json as _json

ALL_ENGINES = [
    {"id": "atlas",   "label": "ATLAS V14",         "desc": "V14 · flagship — V14-1 Swift (typed) + V14-2 Vision (handwriting) + gates"},
    {"id": "presto",  "label": "ATLAS V14-1 SWIFT", "desc": "V14-1 · fast typed-digital"},
    {"id": "classic", "label": "ATLAS CLASSIC",     "desc": "Gen 1 · legacy typed ensemble"},
    {"id": "auto",    "label": "AUTO",          "desc": "follows the admin default"},
]
# By default ONLY the latest engine is enabled; legacy ones are off.
_DEFAULT_ENABLED = ["atlas"]
_DEFAULT_ENGINE = "atlas"


def _engine_config() -> dict:
    raw = database.get_app_setting("engines_enabled", None)
    try:
        enabled = _json.loads(raw) if raw else list(_DEFAULT_ENABLED)
    except Exception:
        enabled = list(_DEFAULT_ENABLED)
    if not enabled:
        enabled = list(_DEFAULT_ENABLED)
    default = database.get_app_setting("engine_default", None) or _DEFAULT_ENGINE
    if default not in enabled:
        default = enabled[0]
    return {"all": ALL_ENGINES, "enabled": enabled, "default": default}


class EngineSettings(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["atlas"])
    default: str = "atlas"


@router.get("/engines")
async def get_engines(current_user: dict = Depends(get_current_user)):
    """Which extraction engines are available to pick + the default. All users
    read this (the agent page uses it); only admins can change it."""
    return _engine_config()


@router.put("/engines")
async def set_engines(body: EngineSettings, admin: dict = Depends(require_admin)):
    """Super-admin: enable/disable engines + set the default. Default unset =
    only ATLAS enabled (legacy off)."""
    valid_ids = {e["id"] for e in ALL_ENGINES}
    enabled = [e for e in body.enabled if e in valid_ids]
    if not enabled:
        enabled = list(_DEFAULT_ENABLED)
    default = body.default if body.default in enabled else enabled[0]
    database.set_app_setting("engines_enabled", _json.dumps(enabled), updated_by=admin.get("username", "admin"))
    database.set_app_setting("engine_default", default, updated_by=admin.get("username", "admin"))
    try:
        database.log_activity(admin["id"], admin["username"], "UPDATE_SETTINGS",
                              f"engines enabled={enabled} default={default}")
    except Exception:
        pass
    return _engine_config()


@router.get("/keycloak", response_model=KeycloakSettingsResponse)
async def get_keycloak_settings(admin: dict = Depends(require_admin)):
    """Get current Keycloak settings (admin only)."""
    settings = database.get_settings_by_prefix("keycloak_")

    return KeycloakSettingsResponse(
        realm_url=settings.get("keycloak_realm_url", {}).get("value", ""),
        client_id=settings.get("keycloak_client_id", {}).get("value", ""),
        client_secret=settings.get("keycloak_client_secret", {}).get("value", ""),
        admin_role=settings.get("keycloak_admin_role", {}).get("value", "admin"),
        enabled=settings.get("keycloak_enabled", {}).get("value", "false") == "true",
        updated_at=settings.get("keycloak_realm_url", {}).get("updated_at"),
    )


@router.put("/keycloak", response_model=KeycloakSettingsResponse)
async def save_keycloak_settings(
    request: KeycloakSettingsRequest,
    admin: dict = Depends(require_admin),
):
    """Save Keycloak settings (admin only). Invalidates cache immediately."""
    username = admin["username"]

    database.set_setting("keycloak_realm_url", request.realm_url.rstrip("/"), username)
    database.set_setting("keycloak_client_id", request.client_id, username)
    database.set_setting("keycloak_client_secret", request.client_secret, username)
    database.set_setting("keycloak_admin_role", request.admin_role or "admin", username)
    database.set_setting("keycloak_enabled", "true" if request.enabled else "false", username)

    # Invalidate cache so changes take effect immediately
    config.invalidate_keycloak_cache()

    database.log_activity(
        admin["id"], username, "UPDATE_SETTINGS",
        f"Keycloak {'enabled' if request.enabled else 'disabled'} — realm: {request.realm_url}",
    )

    return KeycloakSettingsResponse(
        realm_url=request.realm_url,
        client_id=request.client_id,
        client_secret=request.client_secret,
        admin_role=request.admin_role,
        enabled=request.enabled,
    )


@router.post("/keycloak/test", response_model=KeycloakTestResponse)
async def test_keycloak_connection(
    request: KeycloakSettingsRequest,
    admin: dict = Depends(require_admin),
):
    """Test Keycloak connection by fetching JWKS from realm URL (admin only)."""
    if not request.realm_url:
        return KeycloakTestResponse(success=False, message="Realm URL is required", keys_found=0)

    result = auth.test_keycloak_connection(request.realm_url.rstrip("/"))
    return KeycloakTestResponse(**result)
