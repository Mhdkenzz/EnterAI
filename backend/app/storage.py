"""Storage boundary: local disk for a single node, S3-compatible object storage
for anything with more than one replica.

Local disk is per-container. Two API replicas each get their own `uploads/`
directory, so whichever replica did not receive the upload cannot see the file.
`STORAGE_BACKEND=s3` points every replica at one bucket instead; the same adapter
serves real S3 and any S3-compatible server (MinIO in docker-compose) via
STORAGE_ENDPOINT_URL.

`save` returns the identifier persisted on the row, and it is deliberately
self-describing -- a filesystem path for local, `s3://bucket/key` for S3 -- so a
deployment that changes backends can still tell where an existing file lives.
"""
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class Storage(Protocol):
    def save(self, folder: str, filename: str, content: bytes) -> str: ...
    def delete(self, identifier: str) -> None: ...


def _safe_name(filename: str) -> str:
    """Caller-supplied filenames never contribute a directory component: `..` and
    absolute paths would otherwise escape the upload root or the key prefix."""
    return Path(filename).name or "upload"


class LocalStorage:
    def __init__(self, root: str | Path = "uploads"):
        self.root = Path(root)

    def save(self, folder: str, filename: str, content: bytes) -> str:
        target_dir = self.root / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid4()}-{_safe_name(filename)}"
        target.write_bytes(content)
        return str(target)

    def delete(self, identifier: str) -> None:
        target = Path(identifier).resolve()
        root = self.root.resolve()
        if root not in target.parents:
            # `identifier` always comes from a `save()` return value stored on a
            # row, never straight from a request, but refusing to unlink outside
            # our own root costs nothing and rules out a whole class of mistake.
            return
        target.unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket: str, client):
        self.bucket = bucket
        self._client = client

    def save(self, folder: str, filename: str, content: bytes) -> str:
        key = f"{folder}/{uuid4()}-{_safe_name(filename)}"
        self._client.put_object(Bucket=self.bucket, Key=key, Body=content)
        return f"s3://{self.bucket}/{key}"

    def delete(self, identifier: str) -> None:
        prefix = f"s3://{self.bucket}/"
        if not identifier.startswith(prefix):
            return  # not one of ours (e.g. a row saved under a different backend)
        key = identifier[len(prefix):]
        self._client.delete_object(Bucket=self.bucket, Key=key)


def _s3_client():
    import boto3

    # Credentials come from the standard AWS environment/instance mechanisms rather
    # than bespoke settings, so a deployment can use a role instead of long-lived
    # keys. STORAGE_ENDPOINT_URL is what retargets the same code at MinIO.
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("STORAGE_ENDPOINT_URL") or None,
        region_name=os.getenv("STORAGE_REGION", "us-east-1"),
    )


def get_storage() -> Storage:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend == "local":
        return LocalStorage(os.getenv("STORAGE_ROOT", "uploads"))
    if backend == "s3":
        bucket = os.getenv("STORAGE_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError("STORAGE_BUCKET must be set when STORAGE_BACKEND=s3")
        return S3Storage(bucket, _s3_client())
    raise RuntimeError(f"Unsupported STORAGE_BACKEND={backend!r}; expected 'local' or 's3'")
