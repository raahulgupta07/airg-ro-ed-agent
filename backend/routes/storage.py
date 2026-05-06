"""Storage config admin API.

Routes (admin-only):
  GET    /api/storage/configs               list all
  GET    /api/storage/configs/{id}          one (no secret)
  POST   /api/storage/configs               create
  PUT    /api/storage/configs/{id}          update
  DELETE /api/storage/configs/{id}          delete
  POST   /api/storage/configs/{id}/activate set active (deactivates others)
  POST   /api/storage/configs/{id}/test     test saved config
  POST   /api/storage/configs/test          test ad-hoc (before save)
  GET    /api/storage/active                returns active provider info
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import database
import ldap_crypto

try:
    from middleware import require_admin
except ImportError:
    from auth import get_current_user
    async def require_admin(user=Depends(get_current_user)):
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "admin only")
        return user


router = APIRouter()


class StorageConfigIn(BaseModel):
    label: str
    provider: str = "s3"  # local | s3 | gcs | azure
    endpoint_url: Optional[str] = None
    region_name: Optional[str] = None
    bucket_name: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None  # plaintext from UI; encrypted before save
    key_prefix: Optional[str] = ""
    use_ssl: bool = True
    addressing_style: str = "auto"
    signature_version: str = "s3v4"
    use_for_uploads: bool = True
    use_for_exports: bool = True
    use_for_cache: bool = False
    use_for_archive: bool = False
    active: bool = False


class StorageConfigUpdate(BaseModel):
    label: Optional[str] = None
    provider: Optional[str] = None
    endpoint_url: Optional[str] = None
    region_name: Optional[str] = None
    bucket_name: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None  # if non-empty -> re-encrypt
    key_prefix: Optional[str] = None
    use_ssl: Optional[bool] = None
    addressing_style: Optional[str] = None
    signature_version: Optional[str] = None
    use_for_uploads: Optional[bool] = None
    use_for_exports: Optional[bool] = None
    use_for_cache: Optional[bool] = None
    use_for_archive: Optional[bool] = None


class TestRequest(BaseModel):
    provider: str
    endpoint_url: Optional[str] = None
    region_name: Optional[str] = None
    bucket_name: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    key_prefix: Optional[str] = ""
    use_ssl: bool = True
    addressing_style: str = "auto"
    signature_version: str = "s3v4"


@router.get("/configs")
async def list_configs(_=Depends(require_admin)):
    return {"configs": database.list_storage_configs()}


@router.get("/configs/{cfg_id}")
async def get_config(cfg_id: int, _=Depends(require_admin)):
    c = database.get_storage_config(cfg_id, include_secret=False)
    if not c:
        raise HTTPException(404, "not found")
    return c


@router.post("/configs")
async def create_config(body: StorageConfigIn, _=Depends(require_admin)):
    enc = ldap_crypto.encrypt(body.secret_access_key) if body.secret_access_key else ""
    new_id = database.create_storage_config(
        label=body.label, provider=body.provider,
        endpoint_url=body.endpoint_url, region_name=body.region_name,
        bucket_name=body.bucket_name, access_key_id=body.access_key_id,
        secret_access_key_encrypted=enc, key_prefix=body.key_prefix,
        use_ssl=body.use_ssl, addressing_style=body.addressing_style,
        signature_version=body.signature_version,
        use_for_uploads=body.use_for_uploads,
        use_for_exports=body.use_for_exports,
        use_for_cache=body.use_for_cache,
        use_for_archive=body.use_for_archive,
        active=body.active,
    )
    if body.active:
        database.activate_storage_config(new_id)
    try:
        from storage import reset_cache
        reset_cache()
    except Exception:
        pass
    return {"id": new_id, "ok": True}


@router.put("/configs/{cfg_id}")
async def update_config(cfg_id: int, body: StorageConfigUpdate, _=Depends(require_admin)):
    kwargs = {k: v for k, v in body.dict().items() if v is not None}
    if "secret_access_key" in kwargs:
        pw = kwargs.pop("secret_access_key")
        if pw:
            kwargs["secret_access_key_encrypted"] = ldap_crypto.encrypt(pw)
    ok = database.update_storage_config(cfg_id, **kwargs)
    if not ok:
        raise HTTPException(404, "not found or no changes")
    try:
        from storage import reset_cache
        reset_cache()
    except Exception:
        pass
    return {"ok": True}


@router.delete("/configs/{cfg_id}")
async def delete_config(cfg_id: int, _=Depends(require_admin)):
    if not database.delete_storage_config(cfg_id):
        raise HTTPException(404, "not found")
    try:
        from storage import reset_cache
        reset_cache()
    except Exception:
        pass
    return {"ok": True}


@router.post("/configs/{cfg_id}/activate")
async def activate(cfg_id: int, _=Depends(require_admin)):
    if not database.activate_storage_config(cfg_id):
        raise HTTPException(404, "not found")
    try:
        from storage import reset_cache
        reset_cache()
    except Exception:
        pass
    return {"ok": True, "active_id": cfg_id}


@router.post("/configs/{cfg_id}/test")
async def test_saved(cfg_id: int, _=Depends(require_admin)):
    cfg = database.get_storage_config(cfg_id, include_secret=True)
    if not cfg:
        raise HTTPException(404, "not found")
    try:
        from storage import test_provider
        return test_provider(cfg)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/configs/test")
async def test_adhoc(body: TestRequest, _=Depends(require_admin)):
    try:
        from storage import test_provider
        return test_provider(body.dict())
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/active")
async def active_info(_=Depends(require_admin)):
    cfg = database.get_active_storage_config(include_secret=False) if hasattr(database, "get_active_storage_config") else None
    return {"active": cfg or {"provider": "local"}}
