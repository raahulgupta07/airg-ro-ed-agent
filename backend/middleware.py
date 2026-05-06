#!/usr/bin/env python3
"""
FastAPI middleware and dependencies for RO-ED AI Agent
Dual-mode: local JWT or Keycloak OIDC (with fallback)
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware

import auth
import config
import database


class EventContextMiddleware(BaseHTTPMiddleware):
    """Capture client IP + User-Agent into request.state for handlers/event_logger.

    Phase 7: every request gets request.state.client_ip and request.state.user_agent
    populated. Failures here MUST NOT break the request — they are silently ignored.
    """

    async def dispatch(self, request, call_next):
        try:
            request.state.client_ip = (
                request.client.host if request.client else None
            )
        except Exception:
            try:
                request.state.client_ip = None
            except Exception:
                pass
        try:
            request.state.user_agent = (
                request.headers.get("user-agent") or ""
            )[:200]
        except Exception:
            try:
                request.state.user_agent = ""
            except Exception:
                pass
        return await call_next(request)


security = HTTPBearer()


def _try_keycloak(token: str):
    """Try to verify token as Keycloak JWT. Returns user dict or None."""
    kc_config = config.get_keycloak_config()
    if not kc_config or not kc_config.get("realm_url"):
        return None
    try:
        payload = auth.verify_keycloak_token(token)
        user_info = auth.extract_user_info(payload)
        user = database.upsert_keycloak_user(
            keycloak_id=user_info["sub"],
            username=user_info["username"],
            display_name=user_info["display_name"],
            email=user_info["email"],
            role=user_info["role"],
        )
        return {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name", ""),
        }
    except Exception:
        return None


def _try_local(token: str):
    """Try to verify token as local HS256 JWT. Returns user dict or None."""
    token_data = auth.verify_token(token)
    if token_data is None:
        return None
    all_users = database.get_all_users()
    user = next(
        (u for u in all_users if u["id"] == token_data.user_id and u["is_active"]),
        None,
    )
    if user is None:
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "display_name": user.get("display_name", ""),
    }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    FastAPI dependency: extract and verify JWT from Authorization header.
    Tries Keycloak first (if enabled), falls back to local JWT.
    Returns user dict with id, username, role, display_name.
    """
    token = credentials.credentials

    # Try Keycloak first if enabled
    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
        if user:
            return user

    # Fall back to local JWT
    user = _try_local(token)
    if user:
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    FastAPI dependency: requires the current user to have admin role.
    """
    if current_user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


def check_permission(user: dict, action: str):
    """Check if user has a specific action permission. Raises 403 if not."""
    from fastapi import HTTPException
    perms = database.get_user_permissions(user)
    if not perms.get("actions", {}).get(action, False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {action}",
        )


def get_data_scope(user: dict) -> str:
    """Get data scope for a user: 'own', 'all_readonly', 'all_full'."""
    perms = database.get_user_permissions(user)
    return perms.get("data_scope", "own")


async def optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
) -> Optional[dict]:
    """
    FastAPI dependency: optionally extract user from JWT.
    Returns user dict if valid token present, None otherwise.
    """
    if credentials is None:
        return None

    token = credentials.credentials

    kc_config = config.get_keycloak_config()
    if kc_config:
        user = _try_keycloak(token)
        if user:
            return user

    return _try_local(token)
