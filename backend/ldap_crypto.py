"""Fernet encryption utility for LDAP bind passwords."""
import base64
import os
from cryptography.fernet import Fernet

_KEY_ENV = "LDAP_ENCRYPTION_KEY"


def _key() -> bytes:
    raw = os.getenv(_KEY_ENV)
    if not raw:
        # Auto-generate ephemeral key (warn — passwords lost on container restart)
        # In production, set LDAP_ENCRYPTION_KEY in .env to a stable Fernet key
        from pathlib import Path
        cache = Path("/tmp/.ldap_fernet_key")
        if cache.exists():
            raw = cache.read_text().strip()
        else:
            raw = Fernet.generate_key().decode()
            cache.write_text(raw)
            print(f"⚠️  LDAP_ENCRYPTION_KEY not set; generated ephemeral key at {cache}")
    if isinstance(raw, str):
        raw = raw.encode()
    # Ensure valid Fernet key
    try:
        Fernet(raw)
        return raw
    except Exception:
        # Derive from arbitrary string via SHA256 base64
        import hashlib
        h = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(h)


_fernet = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_key())
    return _fernet


def encrypt(plaintext: str) -> str:
    if plaintext is None or plaintext == "":
        return ""
    return _f().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _f().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""
