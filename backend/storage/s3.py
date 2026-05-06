"""S3-compatible provider via boto3.

Works with: AWS S3, MinIO, Wasabi, Cloudflare R2, Backblaze B2, custom."""
from typing import Optional


class S3Provider:
    name = "s3"

    def __init__(self, endpoint_url=None, region_name=None, bucket=None,
                 access_key_id=None, secret_access_key=None, key_prefix="",
                 use_ssl=True, addressing_style="auto", signature_version="s3v4"):
        self.endpoint_url = endpoint_url
        self.region_name = region_name or "us-east-1"
        self.bucket = bucket
        self.key_prefix = key_prefix or ""
        self.use_ssl = use_ssl
        self.addressing_style = addressing_style
        self.signature_version = signature_version
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = None

    def _get_client(self):
        if self._client is not None: return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise RuntimeError(f"boto3 not installed: {e}")
        cfg = Config(
            region_name=self.region_name,
            signature_version=self.signature_version,
            s3={"addressing_style": self.addressing_style},
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            region_name=self.region_name,
            use_ssl=self.use_ssl,
            config=cfg,
        )
        return self._client

    def _full_key(self, key: str) -> str:
        if self.key_prefix:
            return f"{self.key_prefix.rstrip('/')}/{key.lstrip('/')}"
        return key

    def upload(self, key: str, data: bytes, content_type: Optional[str] = None) -> dict:
        c = self._get_client()
        full = self._full_key(key)
        extra = {}
        if content_type: extra["ContentType"] = content_type
        c.put_object(Bucket=self.bucket, Key=full, Body=data, **extra)
        return {"key": full, "bucket": self.bucket, "provider": "s3", "size": len(data)}

    def download(self, key: str) -> bytes:
        c = self._get_client()
        full = self._full_key(key)
        obj = c.get_object(Bucket=self.bucket, Key=full)
        return obj["Body"].read()

    def delete(self, key: str) -> bool:
        try:
            c = self._get_client()
            c.delete_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception as e:
            print(f"[s3] delete error: {e}")
            return False

    def exists(self, key: str) -> bool:
        try:
            c = self._get_client()
            c.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def presign_url(self, key: str, expires: int = 3600) -> str:
        c = self._get_client()
        return c.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=expires,
        )

    def list_keys(self, prefix: str = "") -> list:
        c = self._get_client()
        full_prefix = self._full_key(prefix) if prefix else self.key_prefix
        paginator = c.get_paginator("list_objects_v2")
        out = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                out.append(obj["Key"])
        return out

    def test_connection(self) -> dict:
        try:
            c = self._get_client()
            c.head_bucket(Bucket=self.bucket)
            return {"ok": True, "provider": "s3", "bucket": self.bucket,
                    "endpoint": self.endpoint_url}
        except Exception as e:
            return {"ok": False, "error": str(e)[:300], "provider": "s3"}
