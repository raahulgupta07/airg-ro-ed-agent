"""LDAP config admin API.

Endpoints (admin-only):
  GET    /api/ldap/configs            list
  GET    /api/ldap/configs/{id}       get one (no password)
  POST   /api/ldap/configs            create
  PUT    /api/ldap/configs/{id}       update
  DELETE /api/ldap/configs/{id}       delete
  POST   /api/ldap/configs/{id}/test  test bind credentials of saved config
  POST   /api/ldap/configs/test       test ad-hoc credentials (before saving)
"""
import time
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

import database
import ldap_crypto
import ldap_auth
import event_logger
from middleware import require_admin


router = APIRouter()


def _user_name(u):
    try:
        return u.get("username") if u else None
    except Exception:
        return None


def _client_ip(request: Request):
    try:
        return request.client.host if request and request.client else None
    except Exception:
        return None


class LdapConfigIn(BaseModel):
    label: str
    host: str
    port: int = 389
    use_tls: bool = False
    validate_cert: bool = False
    bind_dn: Optional[str] = None
    bind_password: Optional[str] = None  # plaintext from UI; encrypted before save
    search_base: Optional[str] = None
    search_filter: Optional[str] = None
    attr_username: str = "uid"
    attr_mail: str = "mail"
    attr_groups: str = "memberOf"
    email_domain_hint: Optional[str] = None
    priority: int = 50
    active: bool = True


class LdapConfigUpdate(BaseModel):
    label: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    use_tls: Optional[bool] = None
    validate_cert: Optional[bool] = None
    bind_dn: Optional[str] = None
    bind_password: Optional[str] = None  # if non-empty, replace
    search_base: Optional[str] = None
    search_filter: Optional[str] = None
    attr_username: Optional[str] = None
    attr_mail: Optional[str] = None
    attr_groups: Optional[str] = None
    email_domain_hint: Optional[str] = None
    priority: Optional[int] = None
    active: Optional[bool] = None


class TestRequest(BaseModel):
    """Ad-hoc test of unsaved config."""
    host: str
    port: int = 389
    use_tls: bool = False
    validate_cert: bool = False
    bind_dn: Optional[str] = None
    bind_password: Optional[str] = None


@router.get("/configs")
async def list_configs(_=Depends(require_admin)):
    return {"configs": database.list_ldap_configs(only_active=False)}


@router.get("/configs/{ldap_id}")
async def get_config(ldap_id: int, _=Depends(require_admin)):
    cfg = database.get_ldap_config(ldap_id, include_password=False)
    if not cfg:
        raise HTTPException(404, "not found")
    return cfg


@router.post("/configs")
async def create_config(body: LdapConfigIn, request: Request, user=Depends(require_admin)):
    t0 = time.time()
    enc = ldap_crypto.encrypt(body.bind_password) if body.bind_password else ""
    new_id = database.create_ldap_config(
        label=body.label, host=body.host, port=body.port,
        use_tls=body.use_tls, validate_cert=body.validate_cert,
        bind_dn=body.bind_dn, bind_password_encrypted=enc,
        search_base=body.search_base, search_filter=body.search_filter,
        attr_username=body.attr_username, attr_mail=body.attr_mail,
        attr_groups=body.attr_groups, email_domain_hint=body.email_domain_hint,
        priority=body.priority, active=body.active,
    )
    try:
        event_logger.log_ldap(
            action="LDAP_CREATE",
            user=_user_name(user),
            ldap_id=new_id,
            status="OK",
            details=f"created LDAP {body.label}",
            ip=_client_ip(request),
            duration_ms=int((time.time() - t0) * 1000),
            payload={"label": body.label, "host": body.host, "port": body.port},
        )
    except Exception:
        pass
    return {"id": new_id, "ok": True}


@router.put("/configs/{ldap_id}")
async def update_config(ldap_id: int, body: LdapConfigUpdate, request: Request, user=Depends(require_admin)):
    t0 = time.time()
    kwargs = {k: v for k, v in body.dict().items() if v is not None}
    if "bind_password" in kwargs:
        # Treat empty string as "don't change". Non-empty re-encrypt.
        pw = kwargs.pop("bind_password")
        if pw:
            kwargs["bind_password_encrypted"] = ldap_crypto.encrypt(pw)
    ok = database.update_ldap_config(ldap_id, **kwargs)
    if not ok:
        raise HTTPException(404, "not found or no changes")
    try:
        event_logger.log_ldap(
            action="LDAP_UPDATE",
            user=_user_name(user),
            ldap_id=ldap_id,
            status="OK",
            details=f"updated LDAP {ldap_id}",
            ip=_client_ip(request),
            duration_ms=int((time.time() - t0) * 1000),
            payload={k: v for k, v in kwargs.items() if k != "bind_password_encrypted"},
        )
    except Exception:
        pass
    return {"ok": True}


@router.delete("/configs/{ldap_id}")
async def delete_config(ldap_id: int, request: Request, user=Depends(require_admin)):
    t0 = time.time()
    if not database.delete_ldap_config(ldap_id):
        raise HTTPException(404, "not found")
    try:
        event_logger.log_ldap(
            action="LDAP_DELETE",
            user=_user_name(user),
            ldap_id=ldap_id,
            status="OK",
            details=f"deleted LDAP {ldap_id}",
            ip=_client_ip(request),
            duration_ms=int((time.time() - t0) * 1000),
        )
    except Exception:
        pass
    return {"ok": True}


@router.post("/configs/{ldap_id}/test")
async def test_saved(ldap_id: int, request: Request, user=Depends(require_admin)):
    t0 = time.time()
    cfg = database.get_ldap_config(ldap_id, include_password=True)
    if not cfg:
        raise HTTPException(404, "not found")
    result = ldap_auth.test_connection(cfg)
    try:
        event_logger.log_ldap(
            action="LDAP_TEST",
            user=_user_name(user),
            ldap_id=ldap_id,
            status="OK" if result.get("ok") else "FAILED",
            error=result.get("error"),
            details=f"tested {cfg.get('label') or cfg.get('host')}",
            ip=_client_ip(request),
            duration_ms=int((time.time() - t0) * 1000),
        )
    except Exception:
        pass
    return result


@router.post("/configs/test")
async def test_adhoc(body: TestRequest, request: Request, user=Depends(require_admin)):
    t0 = time.time()
    cfg = body.dict()
    result = ldap_auth.test_connection(cfg)
    try:
        event_logger.log_ldap(
            action="LDAP_TEST",
            user=_user_name(user),
            ldap_id=None,
            status="OK" if result.get("ok") else "FAILED",
            error=result.get("error"),
            details=f"tested ad-hoc {cfg.get('host')}",
            ip=_client_ip(request),
            duration_ms=int((time.time() - t0) * 1000),
        )
    except Exception:
        pass
    return result
