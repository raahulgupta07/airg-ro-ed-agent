#!/usr/bin/env python3
"""
JWT Authentication for RO-ED AI Agent
Dual-mode: local HS256 JWT + Keycloak RS256 JWT (OIDC)
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError
from jose import JWTError, jwt, jwk
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# =============================================================================
# LOCAL AUTH (HS256 — used when Keycloak is disabled)
# =============================================================================

_DEV_FALLBACK = "ro-ed-dev-secret-change-in-production"


def _resolve_secret() -> str:
    """JWT signing secret resolution, in priority order:
      1. JWT_SECRET_KEY env var (explicit — preferred for prod / key rotation).
      2. Auto-generated + DB-persisted key (secure-by-default, shared across all
         app/worker processes and restarts). No manual setup needed.
      3. Insecure dev fallback — only if DEV_MODE is set and the DB is unreachable.
    """
    env = os.getenv("JWT_SECRET_KEY") or ""
    if env:
        if len(env) < 32:
            raise RuntimeError(f"JWT_SECRET_KEY too short ({len(env)} chars). Use ≥32.")
        return env
    # No env var — auto-generate + persist (so it survives restarts and is shared
    # by every uvicorn worker + RQ worker container).
    try:
        import database
        secret = database.get_or_create_jwt_secret()
        if secret and len(secret) >= 32:
            logger.info("JWT_SECRET_KEY auto-resolved from DB (set the env var to override/rotate).")
            return secret
        raise RuntimeError("auto JWT secret too short")
    except Exception as e:
        if os.getenv("DEV_MODE", "").lower() in ("1", "true", "yes"):
            import warnings
            warnings.warn(f"JWT secret auto-resolve failed ({e}) — using dev fallback. Not for production.")
            return _DEV_FALLBACK
        raise RuntimeError(
            f"Could not resolve a JWT signing secret ({e}). Set JWT_SECRET_KEY "
            "(python3 -c 'import secrets; print(secrets.token_urlsafe(64))') "
            "or ensure the database is reachable, or set DEV_MODE=1 for the insecure fallback."
        )


SECRET_KEY = _resolve_secret()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7


class TokenData(BaseModel):
    user_id: int
    username: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a local HS256 JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    """Create a local HS256 JWT refresh token with longer expiry."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[TokenData]:
    """Verify and decode a local HS256 JWT token. Returns TokenData or None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        username = payload.get("username")
        role = payload.get("role")
        if user_id is None or username is None:
            return None
        return TokenData(user_id=user_id, username=username, role=role)
    except JWTError:
        return None


# =============================================================================
# KEYCLOAK AUTH (RS256 — used when Keycloak is enabled)
# =============================================================================

_jwks_cache = {"keys": None, "ts": 0, "url": ""}
_JWKS_CACHE_TTL = 86400  # 24 hours


def _fetch_jwks(jwks_url: str, force: bool = False) -> dict:
    """Fetch and cache JWKS keys from Keycloak. Auto-refreshes every 24h."""
    global _jwks_cache

    if (not force
            and _jwks_cache["keys"]
            and _jwks_cache["url"] == jwks_url
            and time.time() - _jwks_cache["ts"] < _JWKS_CACHE_TTL):
        return _jwks_cache["keys"]

    try:
        req = Request(jwks_url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            jwks_data = json.loads(resp.read().decode())
        _jwks_cache.update({"keys": jwks_data, "ts": time.time(), "url": jwks_url})
        logger.info(f"Fetched JWKS from {jwks_url}: {len(jwks_data.get('keys', []))} keys")
        return jwks_data
    except (URLError, Exception) as e:
        logger.error(f"Failed to fetch JWKS from {jwks_url}: {e}")
        if _jwks_cache["keys"] and _jwks_cache["url"] == jwks_url:
            return _jwks_cache["keys"]
        raise


def _get_signing_key(jwks_data: dict, token: str) -> str:
    """Find the correct signing key from JWKS for the given token."""
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")

    for key_data in jwks_data.get("keys", []):
        if key_data.get("kid") == kid:
            return key_data

    raise JWTError(f"Key with kid={kid} not found in JWKS")


def verify_keycloak_token(token: str) -> dict:
    """
    Verify a Keycloak RS256 JWT access token using JWKS.
    Returns the decoded payload dict.
    Raises JWTError on failure.
    """
    import config
    kc_config = config.get_keycloak_config()
    if not kc_config:
        raise JWTError("Keycloak not configured")

    jwks_url = kc_config["jwks_url"]
    issuer = kc_config["realm_url"]

    jwks_data = _fetch_jwks(jwks_url)

    try:
        key_data = _get_signing_key(jwks_data, token)
    except JWTError:
        # Key not found — maybe Keycloak rotated keys, refetch once
        jwks_data = _fetch_jwks(jwks_url, force=True)
        key_data = _get_signing_key(jwks_data, token)

    # Decode and verify
    payload = jwt.decode(
        token,
        key_data,
        algorithms=["RS256"],
        issuer=issuer,
        options={
            "verify_aud": False,  # Keycloak aud can vary (account, client_id, etc.)
            "verify_iss": True,
            "verify_exp": True,
        },
    )
    return payload


def extract_user_info(payload: dict) -> dict:
    """
    Extract user info from a decoded Keycloak JWT payload.
    Returns dict with: sub, username, display_name, email, role
    """
    import config
    kc_config = config.get_keycloak_config()
    admin_role = kc_config.get("admin_role", "admin") if kc_config else "admin"

    sub = payload.get("sub", "")
    username = payload.get("preferred_username", payload.get("username", ""))
    name = payload.get("name", "")
    if not name:
        given = payload.get("given_name", "")
        family = payload.get("family_name", "")
        name = f"{given} {family}".strip()
    email = payload.get("email", "")

    # Determine role from realm_access.roles
    realm_roles = payload.get("realm_access", {}).get("roles", [])
    role = "admin" if admin_role in realm_roles else "user"

    return {
        "sub": sub,
        "username": username,
        "display_name": name or username,
        "email": email,
        "role": role,
    }


def test_keycloak_connection(realm_url: str) -> dict:
    """Test Keycloak connection by fetching JWKS. Returns status dict."""
    jwks_url = f"{realm_url.rstrip('/')}/protocol/openid-connect/certs"
    try:
        req = Request(jwks_url, headers={"Accept": "application/json"})
        with urlopen(req, timeout=10) as resp:
            jwks_data = json.loads(resp.read().decode())
        keys_count = len(jwks_data.get("keys", []))
        return {"success": True, "message": f"JWKS reachable — {keys_count} key(s) found", "keys_found": keys_count}
    except Exception as e:
        return {"success": False, "message": f"Failed to reach JWKS endpoint: {e}", "keys_found": 0}
