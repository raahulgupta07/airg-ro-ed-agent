"""LDAP authentication core: bind, search, scan-resolver.

resolve_ldap(username, password) attempts to authenticate against multiple
LDAP servers in priority order. Returns (ldap_id, user_dn, attrs) or None.

Strategy:
1. If user has cached default_ldap_id → try that first (fast path).
2. If username has @domain → find LDAP by email_domain_hint → try.
3. Scan ALL active LDAPs by priority ascending; return first success.
"""
import ssl
from typing import Optional, Tuple, Dict, List

try:
    from ldap3 import Server, Connection, Tls, ALL, SUBTREE, AUTO_BIND_NO_TLS, AUTO_BIND_TLS_BEFORE_BIND
except ImportError:
    Server = Connection = Tls = None

import database
import ldap_crypto


def _bind_and_find_user(cfg: Dict, username: str, password: str) -> Optional[Tuple[str, Dict]]:
    """For a given LDAP config, find the user DN and attempt bind with their password.
    Returns (user_dn, user_attrs) on success, None on failure."""
    if Server is None:
        print("[LDAP] ldap3 not installed")
        return None
    host = cfg.get("host")
    port = int(cfg.get("port") or 389)
    use_tls = bool(cfg.get("use_tls"))
    validate = bool(cfg.get("validate_cert"))
    bind_dn = cfg.get("bind_dn")
    enc_pw = cfg.get("bind_password_encrypted") or ""
    bind_pw = ldap_crypto.decrypt(enc_pw) if enc_pw else ""
    search_base = cfg.get("search_base")
    search_filter = cfg.get("search_filter") or ""
    attr_username = cfg.get("attr_username") or "uid"
    attr_mail = cfg.get("attr_mail") or "mail"
    attr_groups = cfg.get("attr_groups") or "memberOf"

    # Strip @domain from username if present (LDAP usually wants just the local part)
    raw_user = username.split("@")[0] if "@" in username else username

    tls = None
    if use_tls:
        tls = Tls(validate=ssl.CERT_REQUIRED if validate else ssl.CERT_NONE,
                   version=ssl.PROTOCOL_TLS_CLIENT)
    server = Server(host=host, port=port, use_ssl=(port == 636), tls=tls, get_info=ALL)

    # Phase 1: Bind as service account, search for user DN
    try:
        conn = Connection(server, user=bind_dn, password=bind_pw,
                          auto_bind=(AUTO_BIND_TLS_BEFORE_BIND if (use_tls and port != 636) else AUTO_BIND_NO_TLS),
                          receive_timeout=10)
    except Exception as e:
        print(f"[LDAP {cfg.get('label')}] service bind failed: {e}")
        return None

    # Build search filter
    user_filter = f"({attr_username}={raw_user})"
    if search_filter:
        # Combine if user provided extra filter; AND them
        combined = f"(&{user_filter}{search_filter if search_filter.startswith('(') else f'({search_filter})'})"
    else:
        combined = user_filter

    try:
        conn.search(search_base=search_base or "",
                    search_filter=combined,
                    search_scope=SUBTREE,
                    attributes=[attr_username, attr_mail, attr_groups, "cn", "sn", "givenName"])
    except Exception as e:
        print(f"[LDAP {cfg.get('label')}] search failed: {e}")
        try: conn.unbind()
        except Exception: pass
        return None

    if not conn.entries:
        print(f"[LDAP {cfg.get('label')}] user {raw_user!r} not found")
        try: conn.unbind()
        except Exception: pass
        return None

    user_entry = conn.entries[0]
    user_dn = str(user_entry.entry_dn)
    user_attrs = {
        "username": str(user_entry[attr_username].value) if attr_username in user_entry else raw_user,
        "mail": str(user_entry[attr_mail].value) if attr_mail in user_entry else "",
        "groups": [str(g) for g in (user_entry[attr_groups].values if attr_groups in user_entry else [])],
        "cn": str(user_entry["cn"].value) if "cn" in user_entry else "",
    }
    try: conn.unbind()
    except Exception: pass

    # Phase 2: Re-bind as the user with their password
    try:
        user_conn = Connection(server, user=user_dn, password=password, receive_timeout=10)
        ok = user_conn.bind()
        try: user_conn.unbind()
        except Exception: pass
    except Exception as e:
        print(f"[LDAP {cfg.get('label')}] user bind failed: {e}")
        return None

    if not ok:
        print(f"[LDAP {cfg.get('label')}] user {user_dn} password incorrect")
        return None

    print(f"[LDAP {cfg.get('label')}] ✓ authenticated {user_dn}")
    return user_dn, user_attrs


def test_connection(cfg: Dict) -> Dict:
    """Test that the bind_dn / bind_password works. Used by admin UI's 'Test' button."""
    if Server is None:
        return {"ok": False, "error": "ldap3 not installed"}
    host = cfg.get("host")
    port = int(cfg.get("port") or 389)
    use_tls = bool(cfg.get("use_tls"))
    validate = bool(cfg.get("validate_cert"))
    bind_dn = cfg.get("bind_dn")
    enc_pw = cfg.get("bind_password_encrypted") or ""
    if not enc_pw and cfg.get("bind_password"):
        # Convenience: caller passed plaintext for test before saving
        bind_pw = cfg.get("bind_password")
    else:
        bind_pw = ldap_crypto.decrypt(enc_pw) if enc_pw else ""

    tls = None
    if use_tls:
        tls = Tls(validate=ssl.CERT_REQUIRED if validate else ssl.CERT_NONE,
                   version=ssl.PROTOCOL_TLS_CLIENT)
    try:
        server = Server(host=host, port=port, use_ssl=(port == 636), tls=tls, get_info=ALL)
        conn = Connection(server, user=bind_dn, password=bind_pw,
                          auto_bind=(AUTO_BIND_TLS_BEFORE_BIND if (use_tls and port != 636) else AUTO_BIND_NO_TLS),
                          receive_timeout=10)
        info = {"ok": True, "server_info": str(server.info)[:300] if server.info else None}
        try: conn.unbind()
        except Exception: pass
        return info
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def resolve_ldap(username: str, password: str) -> Optional[Tuple[int, str, Dict]]:
    """Authenticate user across multiple LDAP servers. Returns (ldap_id, user_dn, attrs) or None.

    Strategy:
      1. Cached default_ldap_id (if user exists in DB).
      2. Email-domain hint match.
      3. Scan all active LDAPs by priority.
    """
    # 1. Cached default
    user = None
    try:
        users = database.list_users() if hasattr(database, "list_users") else []
        user = next((u for u in users if (u.get("username") or "") == username), None)
    except Exception:
        user = None
    if user and user.get("default_ldap_id"):
        cfg = database.get_ldap_config(user["default_ldap_id"], include_password=True)
        if cfg and cfg.get("active", 1):
            res = _bind_and_find_user(cfg, username, password)
            if res:
                dn, attrs = res
                return cfg["id"], dn, attrs

    # 2. Email-domain hint
    if "@" in username:
        domain = username.split("@", 1)[1].lower()
        hint_id = database.find_ldap_by_email_domain(domain)
        if hint_id:
            cfg = database.get_ldap_config(hint_id, include_password=True)
            if cfg and cfg.get("active", 1):
                res = _bind_and_find_user(cfg, username, password)
                if res:
                    dn, attrs = res
                    database.update_user_ldap_default(username, cfg["id"], dn)
                    return cfg["id"], dn, attrs

    # 3. Scan all by priority
    for cfg_lite in database.list_ldap_configs(only_active=True):
        cfg = database.get_ldap_config(cfg_lite["id"], include_password=True)
        if not cfg: continue
        res = _bind_and_find_user(cfg, username, password)
        if res:
            dn, attrs = res
            database.update_user_ldap_default(username, cfg["id"], dn)
            return cfg["id"], dn, attrs

    return None
