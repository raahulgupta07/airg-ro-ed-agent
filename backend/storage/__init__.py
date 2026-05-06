"""Storage abstraction. get_provider() returns active provider per DB config or env fallback.

Supports: local (filesystem), s3 (any S3-compatible)."""
import os
from typing import Optional

from .local import LocalProvider
from .s3 import S3Provider


_cached_provider = None


def _build_from_db_config(cfg: dict):
    """Build provider from DB config dict (already decrypted secret)."""
    p = (cfg.get("provider") or "local").lower()
    if p == "s3":
        try:
            import ldap_crypto
            secret = ldap_crypto.decrypt(cfg.get("secret_access_key_encrypted") or "")
        except Exception:
            secret = ""
        return S3Provider(
            endpoint_url=cfg.get("endpoint_url"),
            region_name=cfg.get("region_name"),
            bucket=cfg.get("bucket_name"),
            access_key_id=cfg.get("access_key_id"),
            secret_access_key=secret,
            key_prefix=cfg.get("key_prefix") or "",
            use_ssl=bool(cfg.get("use_ssl", 1)),
            addressing_style=cfg.get("addressing_style") or "auto",
            signature_version=cfg.get("signature_version") or "s3v4",
        )
    return LocalProvider()


def _build_from_env():
    """Open WebUI–compatible env vars."""
    p = os.getenv("STORAGE_PROVIDER", "local").lower()
    if p == "s3":
        return S3Provider(
            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
            region_name=os.getenv("S3_REGION_NAME"),
            bucket=os.getenv("S3_BUCKET_NAME"),
            access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
            secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
            key_prefix=os.getenv("S3_KEY_PREFIX", ""),
        )
    return LocalProvider()


def get_provider(force_reload: bool = False):
    """Return active provider.
    Priority: DB-stored active config > env vars > local fallback."""
    global _cached_provider
    if _cached_provider is not None and not force_reload:
        return _cached_provider
    try:
        import database
        cfg = database.get_active_storage_config(include_secret=True)
        if cfg and cfg.get("provider") and cfg.get("provider") != "local":
            _cached_provider = _build_from_db_config(cfg)
            return _cached_provider
    except Exception as e:
        print(f"[storage] DB config load failed: {e}")
    _cached_provider = _build_from_env()
    return _cached_provider


def reset_cache():
    """Call after admin changes config so next get_provider() rebuilds."""
    global _cached_provider
    _cached_provider = None


def test_provider(cfg: dict) -> dict:
    """Test ad-hoc config (used by admin's TEST button before saving).
    cfg may have plain `secret_access_key` or encrypted variant."""
    try:
        from .s3 import S3Provider
        if (cfg.get("provider") or "").lower() == "s3":
            secret = cfg.get("secret_access_key")
            if not secret and cfg.get("secret_access_key_encrypted"):
                try:
                    import ldap_crypto
                    secret = ldap_crypto.decrypt(cfg["secret_access_key_encrypted"])
                except Exception: secret = ""
            p = S3Provider(
                endpoint_url=cfg.get("endpoint_url"),
                region_name=cfg.get("region_name"),
                bucket=cfg.get("bucket_name"),
                access_key_id=cfg.get("access_key_id"),
                secret_access_key=secret,
                key_prefix=cfg.get("key_prefix") or "",
                use_ssl=bool(cfg.get("use_ssl", True)),
                addressing_style=cfg.get("addressing_style") or "auto",
                signature_version=cfg.get("signature_version") or "s3v4",
            )
            return p.test_connection()
        return {"ok": True, "note": "local provider — always works"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
