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

    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri=_redis_url,
        default_limits=["1000/hour"],   # global per-IP fallback
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
