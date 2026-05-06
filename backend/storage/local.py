"""Local filesystem provider (default fallback)."""
import os
from pathlib import Path
from typing import Optional


class LocalProvider:
    name = "local"

    def __init__(self, root: str = None):
        try:
            import config as _cfg
            default_root = str(_cfg.UPLOAD_FOLDER) if hasattr(_cfg, "UPLOAD_FOLDER") else "/app/data/uploads"
        except Exception:
            default_root = "/app/data/uploads"
        self.root = Path(root or os.environ.get("LOCAL_STORAGE_ROOT", default_root))
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, key: str, data: bytes, content_type: Optional[str] = None) -> dict:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"key": key, "url": f"file://{path}", "provider": "local", "size": len(data)}

    def download(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def delete(self, key: str) -> bool:
        try:
            (self.root / key).unlink()
            return True
        except Exception:
            return False

    def exists(self, key: str) -> bool:
        return (self.root / key).exists()

    def presign_url(self, key: str, expires: int = 3600) -> str:
        return f"file://{self.root / key}"

    def list_keys(self, prefix: str = "") -> list:
        base = self.root / prefix if prefix else self.root
        if not base.exists(): return []
        return [str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()]

    def test_connection(self) -> dict:
        return {"ok": True, "provider": "local", "root": str(self.root)}
