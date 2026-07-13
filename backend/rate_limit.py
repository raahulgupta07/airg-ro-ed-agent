"""Shared slowapi limiter — Redis-backed so limits are consistent across
uvicorn workers and RQ workers.

Imported by main.py (registers middleware + handler) and route modules
(uses @limiter.limit decorators). Centralized here to avoid circular imports.
"""
import os

LIMIT_LOGIN = os.environ.get("RATE_LIMIT_LOGIN", "5/minute")
LIMIT_EXTRACT = os.environ.get("RATE_LIMIT_EXTRACT", "10/minute")

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler  # noqa: F401
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded  # noqa: F401
    from slowapi.middleware import SlowAPIMiddleware  # noqa: F401

    _redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")

    def _client_key(request):
        """Rate-limit key: authenticated user when present, else client IP.

        Without this, 10 users behind one office NAT IP share a single per-IP
        budget (the 6th extract gets 429). Keying by the JWT subject gives each
        user their own budget. Unauthenticated requests (login) have no token →
        fall back to IP, preserving brute-force protection.

        The signature IS verified. It previously wasn't ("this only picks a
        bucket") — but an attacker could then forge an arbitrary user_id per
        request and get a fresh budget every time, which defeats the per-user
        limit AND the 5/min brute-force limit on login. An unverifiable token
        falls back to the IP bucket.
        """
        try:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                token = header.split(" ", 1)[1]
                import auth as _auth   # lazy — avoids a circular import at module load

                td = _auth.verify_token(token)          # local HS256 → TokenData | None
                if td is not None:
                    return f"user:{td.user_id}"

                try:
                    payload = _auth.verify_keycloak_token(token)   # OIDC RS256, JWKS cached
                except Exception:
                    payload = None
                if payload:
                    uid = payload.get("sub") or payload.get("preferred_username")
                    if uid:
                        return f"user:{uid}"
        except Exception:
            pass
        return get_remote_address(request)

    limiter = Limiter(
        key_func=_client_key,
        storage_uri=_redis_url,
        default_limits=["1000/hour"],   # global fallback (per-user, or per-IP if unauthenticated)
        headers_enabled=False,  # avoid Response-param injection requirement on async endpoints
    )
    RATE_LIMIT_OK = True
except Exception as _e:  # slowapi missing or redis unreachable at import time
    print(f"[rate-limit] disabled: {_e}")
    limiter = None
    RATE_LIMIT_OK = False


def maybe_limit(spec):
    """Apply slowapi limit if available, else no-op decorator."""
    if limiter is not None:
        return limiter.limit(spec)
    return lambda f: f
