"""Copy verified PostgreSQL archives to a separate off-host S3-compatible bucket."""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger("enterai.backup_database")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/var/backups/enterai"))


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("DATABASE_BACKUP_ENDPOINT_URL") or None,
        region_name=os.getenv("DATABASE_BACKUP_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("DATABASE_BACKUP_ACCESS_KEY") or None,
        aws_secret_access_key=os.getenv("DATABASE_BACKUP_SECRET_KEY") or None,
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as archive:
        return hashlib.file_digest(archive, "sha256").hexdigest()


def run_once() -> int:
    bucket = os.environ["DATABASE_BACKUP_BUCKET"].strip()
    if not bucket or bucket in {
        os.getenv("BACKUP_OBJECTS_BUCKET", "").strip(),
        os.getenv("STORAGE_BUCKET", "").strip(),
    }:
        raise RuntimeError("Database backups require a dedicated bucket separate from primary and upload storage")
    client = _client()
    remote: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        remote.update(item["Key"] for item in page.get("Contents", []) if item["Key"].endswith(".dump"))
    uploaded = 0
    for archive in sorted(BACKUP_DIR.glob("enterai_*.dump")):
        stat = archive.stat()
        if archive.name in remote:
            info = client.head_object(Bucket=bucket, Key=archive.name)
            metadata = info.get("Metadata") or {}
            same_size = info.get("ContentLength") == stat.st_size
            if same_size and metadata.get("local_mtime_ns") == str(stat.st_mtime_ns):
                continue
            digest = _sha256(archive)
            if same_size and metadata.get("sha256") == digest:
                continue
        else:
            digest = _sha256(archive)
        client.upload_file(str(archive), bucket, archive.name, ExtraArgs={
            "Metadata": {"sha256": digest, "local_mtime_ns": str(stat.st_mtime_ns)},
        })
        info = client.head_object(Bucket=bucket, Key=archive.name)
        metadata = info.get("Metadata") or {}
        if (info.get("ContentLength") != stat.st_size or metadata.get("sha256") != digest
                or metadata.get("local_mtime_ns") != str(stat.st_mtime_ns)):
            raise RuntimeError(f"Offsite archive integrity check failed for {archive.name}")
        uploaded += 1
        remote.add(archive.name)
    print(f"database offsite backup: {uploaded} uploaded, {len(remote)} retained remotely", flush=True)
    return uploaded


def main(argv: list[str]) -> int:
    interval = float(os.getenv("DATABASE_BACKUP_SYNC_INTERVAL_SECONDS", "300"))
    if "--loop" not in argv:
        run_once()
        return 0
    while True:
        try:
            run_once()
        except Exception as error:
            logger.error("database_offsite_backup_failed", extra={"event": "database_offsite_backup_failed"})
            print(f"database offsite backup failed: {type(error).__name__}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
