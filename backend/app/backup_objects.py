"""Mirror the upload bucket to a backup bucket (`python -m app.backup_objects`).

`scripts/backup.sh` covers PostgreSQL only; without this, a lost volume or bucket
would leave every document row pointing at a file that no longer exists.

Uploaded objects are immutable -- `storage.S3Storage.save` writes each one under a
fresh UUID key and never overwrites -- so a mirror only has to copy keys the backup
lacks and remove keys the source no longer has. Removal is what keeps an account or
organization deletion from living on in backups forever; recoverability comes from
the destination bucket itself: enable versioning there with a noncurrent-version
expiry of BACKUP_RETENTION_DAYS, so a deleted or overwritten object stays
restorable for that window and is then purged.

Configuration (destination may be a different provider, e.g. off-site R2 or S3):
  BACKUP_OBJECTS_BUCKET          required; unset means "not configured" and exits 0
  BACKUP_OBJECTS_ENDPOINT_URL    optional (empty for AWS S3)
  BACKUP_OBJECTS_REGION          optional, default us-east-1
  BACKUP_OBJECTS_ACCESS_KEY / BACKUP_OBJECTS_SECRET_KEY   optional; falls back to the
                                 standard AWS credential chain
  BACKUP_INTERVAL_SECONDS        with --loop, seconds between runs (default 86400)
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

from .storage import _s3_client

logger = logging.getLogger("enterai.backup_objects")


def _destination():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("BACKUP_OBJECTS_ENDPOINT_URL") or None,
        region_name=os.getenv("BACKUP_OBJECTS_REGION", "us-east-1"),
        aws_access_key_id=os.getenv("BACKUP_OBJECTS_ACCESS_KEY") or None,
        aws_secret_access_key=os.getenv("BACKUP_OBJECTS_SECRET_KEY") or None,
    )


def _keys(client, bucket: str) -> set[str]:
    keys: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys.update(item["Key"] for item in page.get("Contents", []))
    return keys


def mirror(source, source_bucket: str, destination, destination_bucket: str) -> dict:
    """Copy missing objects, then delete ones the source no longer has. Returns counts."""
    source_keys = _keys(source, source_bucket)
    backup_keys = _keys(destination, destination_bucket)
    copied = 0
    for key in sorted(source_keys - backup_keys):
        # Spooled through a temporary file: source and destination may be different
        # providers, so a server-side copy is not available, and a large document
        # should not have to fit in memory.
        with tempfile.TemporaryFile() as buffer:
            source.download_fileobj(source_bucket, key, buffer)
            buffer.seek(0)
            destination.upload_fileobj(buffer, destination_bucket, key)
        copied += 1
    removed = 0
    for key in sorted(backup_keys - source_keys):
        destination.delete_object(Bucket=destination_bucket, Key=key)
        removed += 1
    return {"copied": copied, "removed": removed, "total": len(source_keys)}


def run_once() -> dict | None:
    destination_bucket = os.getenv("BACKUP_OBJECTS_BUCKET", "").strip()
    if not destination_bucket:
        logger.warning("BACKUP_OBJECTS_BUCKET is not set; uploaded files are not being backed up")
        return None
    result = mirror(_s3_client(), os.environ["STORAGE_BUCKET"], _destination(), destination_bucket)
    print(f"object backup: {result['copied']} copied, {result['removed']} removed, {result['total']} in source", flush=True)
    return result


def main(argv: list[str]) -> int:
    if "--loop" not in argv:
        run_once()
        return 0
    interval = float(os.getenv("BACKUP_INTERVAL_SECONDS", "86400"))
    while True:
        try:
            run_once()
        except Exception as error:  # retried next interval; the failure is what alerting should watch
            print(f"object backup failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
