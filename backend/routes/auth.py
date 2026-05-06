#!/usr/bin/env python3
"""Auth routes for RO-ED AI Agent — dual-mode (local + Keycloak OIDC)"""

import json
import logging
import time
from urllib.request import urlopen, Request as UrlRequest
from urllib.parse import urlencode
from urllib.error import URLError

from fastapi import APIRouter, Depends, HTTPException, status, Request

import auth
import config
import database
try:
    import event_logger  # central event logger (Phase 3)
except Exception:  # pragma: no cover
    event_logger = None
from middleware import get_current_user


def _safe_log_login(**kwargs):
    """Wrap event_logger.log_login so it never breaks the request."""
    try:
        if event_logger is not None:
            event_logger.log_login(**kwargs)
    except Exception:
        try:
            logger.warning("[event_logger.log_login] failed", exc_info=True)
        except Exception:
            pass


def _safe_log_event(action, **kwargs):
    """Wrap event_logger.log_event so it never breaks the request."""
    try:
        if event_logger is not None:
            event_logger.log_event(action, **kwargs)
    except Exception:
        try:
            logger.warning("[event_logger.log_event] failed", exc_info=True)
        except Exception:
            pass


def _req_ctx(request: Request):
    """Extract IP + UA from request (also from request.state if middleware set them)."""
    ip = None
    ua = ""
    try:
        ip = getattr(request.state, "client_ip", None)
        ua = getattr(request.state, "user_agent", "") or ""
    except Exception:
        pass
    if ip is None:
        try:
            ip = request.client.host if request.client else None
        except Exception:
            ip = None
    if not ua:
        try:
            ua = (request.headers.get("user-agent") or "")[:200]
        except Exception:
            ua = ""
    return ip, ua
from schemas import (
    LoginRequest, TokenResponse, UserResponse,
    OIDCConfigResponse, TokenExchangeRequest, TokenExchangeResponse,
    RefreshTokenRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# =============================================================================
# AUTH CONFIG — tells frontend which mode to use
# =============================================================================

@router.get("/config", response_model=OIDCConfigResponse)
async def get_auth_config():
    """Public endpoint: returns auth mode and OIDC endpoints if Keycloak enabled."""
    kc = config.get_keycloak_config()
    if kc:
        return OIDCConfigResponse(
            mode="keycloak",
            auth_url=kc["auth_url"],
            token_url=kc["token_url"],
            logout_url=kc["logout_url"],
            client_id=kc["client_id"],
        )
    return OIDCConfigResponse(mode="local")


# =============================================================================
# LOCAL LOGIN (used when Keycloak is disabled)
# =============================================================================

@router.post("/login", response_model=TokenResponse)
async def login(request: Request, payload: LoginRequest):
    """Authenticate user with username/password (works in both modes).

    Cascade order:
      1) LDAP — try all configured directories (auto-create user on first success)
      2) Local bcrypt password — preserved fallback for local-only users
    """
    t0 = time.time()
    ip, ua = _req_ctx(request)
    username = payload.username.strip()
    password = payload.password

    # ─── Try LDAP first ───────────────────────────────────────────────────
    ldap_result = None
    try:
        from ldap_auth import resolve_ldap  # imported lazily — module may be optional
        ldap_result = resolve_ldap(username, password)
    except ImportError:
        ldap_result = None
    except Exception as e:
        logger.warning(f"[Login] LDAP scan error: {e}")
        ldap_result = None

    if ldap_result:
        ldap_id, user_dn, attrs = ldap_result
        attrs = attrs or {}
        cn = attrs.get("cn") or username
        mail = attrs.get("mail", "") or ""

        # Look up existing user (best-effort across helper variants)
        db_user = None
        try:
            if hasattr(database, "get_user_by_username"):
                db_user = database.get_user_by_username(username)
            else:
                all_users = database.get_all_users()
                db_user = next((u for u in all_users if u.get("username") == username), None)
        except Exception as e:
            logger.warning(f"[Login] user lookup failed: {e}")
            db_user = None

        # Auto-create if missing (respect setting if present, default True)
        auto_create = True
        try:
            if hasattr(config, "get_setting"):
                val = config.get_setting("auto_create_user_on_first_ldap_login")
                if val is not None:
                    auto_create = str(val).lower() not in ("0", "false", "no", "off")
        except Exception:
            pass

        if not db_user and auto_create:
            try:
                # Empty password → bcrypt of "" is unmatchable in normal login
                # (forces LDAP auth always for this user).
                database.create_user(
                    username=username,
                    password="",
                    display_name=cn,
                    role="user",
                )
            except Exception as e:
                logger.warning(f"[Login] auto-create user failed: {e}")

            # Re-fetch to obtain id
            try:
                if hasattr(database, "get_user_by_username"):
                    db_user = database.get_user_by_username(username)
                else:
                    all_users = database.get_all_users()
                    db_user = next((u for u in all_users if u.get("username") == username), None)
            except Exception:
                db_user = None

        # Cache default LDAP for this user
        try:
            if hasattr(database, "update_user_ldap_default"):
                database.update_user_ldap_default(username, ldap_id, user_dn)
        except Exception as e:
            logger.warning(f"[Login] update_user_ldap_default failed: {e}")

        if not db_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="LDAP authenticated but user provisioning failed",
            )

        token_data = {
            "user_id": db_user["id"],
            "username": db_user["username"],
            "role": db_user.get("role", "user"),
            "auth_source": f"ldap:{ldap_id}",
        }
        access_token = auth.create_access_token(token_data)

        try:
            database.log_activity(
                db_user["id"], db_user["username"], "LOGIN", f"LDAP:{ldap_id}"
            )
        except Exception:
            pass

        _safe_log_login(
            user=db_user["username"], status="OK",
            ip=ip, user_agent=ua,
            auth_source=f"LDAP:{ldap_id}",
            duration_ms=int((time.time() - t0) * 1000),
        )

        return TokenResponse(
            access_token=access_token,
            user=UserResponse(
                id=db_user["id"],
                username=db_user["username"],
                display_name=db_user.get("display_name", "") or cn,
                role=db_user.get("role", "user"),
                is_active=True,
            ),
        )

    # ─── Fall through to existing local auth ──────────────────────────────
    user = database.authenticate_user(username, password)
    if not user:
        _safe_log_login(
            user=username, status="FAILED",
            ip=ip, user_agent=ua, auth_source="LOCAL",
            duration_ms=int((time.time() - t0) * 1000),
            error="invalid credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token_data = {
        "user_id": user["id"],
        "username": user["username"],
        "role": user["role"],
    }
    access_token = auth.create_access_token(token_data)

    database.log_activity(user["id"], user["username"], "LOGIN", "Session started")

    _safe_log_login(
        user=user["username"], status="OK",
        ip=ip, user_agent=ua, auth_source="LOCAL",
        duration_ms=int((time.time() - t0) * 1000),
    )

    return TokenResponse(
        access_token=access_token,
        user=UserResponse(
            id=user["id"],
            username=user["username"],
            display_name=user.get("display_name", ""),
            role=user["role"],
            is_active=True,
        ),
    )


# =============================================================================
# KEYCLOAK OIDC TOKEN EXCHANGE
# =============================================================================

@router.post("/token", response_model=TokenExchangeResponse)
async def exchange_token(request: Request, payload: TokenExchangeRequest):
    """Exchange OIDC authorization code for tokens via Keycloak."""
    t0 = time.time()
    ip, ua = _req_ctx(request)
    kc = config.get_keycloak_config()
    if not kc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak not configured",
        )

    # Build token request to Keycloak
    data = {
        "grant_type": "authorization_code",
        "code": payload.code,
        "redirect_uri": payload.redirect_uri,
        "client_id": kc["client_id"],
        "code_verifier": payload.code_verifier,
    }
    if kc.get("client_secret"):
        data["client_secret"] = kc["client_secret"]

    try:
        body = urlencode(data).encode()
        req = UrlRequest(
            kc["token_url"],
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())
    except URLError as e:
        logger.error(f"Keycloak token exchange failed: {e}")
        _safe_log_login(
            user="<unknown>", status="FAILED",
            ip=ip, user_agent=ua, auth_source="KEYCLOAK",
            duration_ms=int((time.time() - t0) * 1000),
            error=f"keycloak token exchange failed: {e}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Keycloak token exchange failed: {e}",
        )

    if "error" in token_data:
        _safe_log_login(
            user="<unknown>", status="FAILED",
            ip=ip, user_agent=ua, auth_source="KEYCLOAK",
            duration_ms=int((time.time() - t0) * 1000),
            error=token_data.get("error_description", token_data["error"]),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=token_data.get("error_description", token_data["error"]),
        )

    # Auto-provision user from the access token
    kc_username = "<unknown>"
    try:
        kc_payload = auth.verify_keycloak_token(token_data["access_token"])
        user_info = auth.extract_user_info(kc_payload)
        user = database.upsert_keycloak_user(
            keycloak_id=user_info["sub"],
            username=user_info["username"],
            display_name=user_info["display_name"],
            email=user_info["email"],
            role=user_info["role"],
        )
        kc_username = user["username"]
        database.log_activity(user["id"], user["username"], "LOGIN", "Keycloak SSO")
    except Exception as e:
        logger.warning(f"User provisioning after token exchange: {e}")

    _safe_log_login(
        user=kc_username, status="OK",
        ip=ip, user_agent=ua, auth_source="KEYCLOAK",
        duration_ms=int((time.time() - t0) * 1000),
    )

    return TokenExchangeResponse(
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in", 300),
    )


# =============================================================================
# KEYCLOAK TOKEN REFRESH
# =============================================================================

@router.post("/refresh", response_model=TokenExchangeResponse)
async def refresh_token(request: Request, payload: RefreshTokenRequest):
    """Refresh Keycloak access token using refresh token."""
    t0 = time.time()
    ip, ua = _req_ctx(request)
    kc = config.get_keycloak_config()
    if not kc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak not configured",
        )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": payload.refresh_token,
        "client_id": kc["client_id"],
    }
    if kc.get("client_secret"):
        data["client_secret"] = kc["client_secret"]

    try:
        body = urlencode(data).encode()
        req = UrlRequest(
            kc["token_url"],
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode())
    except URLError as e:
        _safe_log_event(
            "TOKEN_REFRESH", status="FAILED",
            ip=ip, user_agent=ua, auth_source="KEYCLOAK",
            duration_ms=int((time.time() - t0) * 1000),
            error=f"token refresh failed: {e}",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Token refresh failed: {e}",
        )

    if "error" in token_data:
        _safe_log_event(
            "TOKEN_REFRESH", status="FAILED",
            ip=ip, user_agent=ua, auth_source="KEYCLOAK",
            duration_ms=int((time.time() - t0) * 1000),
            error=token_data.get("error_description", token_data["error"]),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=token_data.get("error_description", token_data["error"]),
        )

    _safe_log_event(
        "TOKEN_REFRESH", status="OK",
        ip=ip, user_agent=ua, auth_source="KEYCLOAK",
        duration_ms=int((time.time() - t0) * 1000),
    )

    return TokenExchangeResponse(
        access_token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in", 300),
    )


# =============================================================================
# CURRENT USER + LOGOUT
# =============================================================================

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user info with permissions."""
    permissions = database.get_user_permissions(current_user)
    group = database.get_user_group(current_user["id"])

    # Determine auth type
    all_users = database.get_all_users()
    user_row = next((u for u in all_users if u["id"] == current_user["id"]), None)
    has_keycloak = bool(user_row and user_row.get("keycloak_id")) if user_row else False

    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "display_name": current_user.get("display_name", ""),
        "role": current_user["role"],
        "email": user_row.get("email", "") if user_row else "",
        "auth_type": "keycloak" if has_keycloak else "local",
        "group": {"id": group["id"], "name": group["name"]} if group else None,
        "permissions": permissions,
    }


@router.post("/logout")
async def logout(request: Request, current_user: dict = Depends(get_current_user)):
    """Log the logout event. Client should discard the token."""
    ip, ua = _req_ctx(request)
    database.log_activity(
        current_user["id"], current_user["username"], "LOGOUT", "Session ended"
    )
    _safe_log_event(
        "LOGOUT",
        user=current_user["username"],
        status="OK",
        ip=ip, user_agent=ua,
    )
    return {"message": "Logged out"}
