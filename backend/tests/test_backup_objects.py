"""Uploaded files are backed up alongside the database, and deletions reach the backup."""
import pytest

from app.backup_objects import mirror, run_once
from app.storage import S3Storage

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")


@pytest.fixture
def buckets():
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="uploads")
        client.create_bucket(Bucket="uploads-backup")
        yield client


def test_the_mirror_copies_new_files_and_drops_deleted_ones(buckets):
    storage = S3Storage("uploads", buckets)
    kept = storage.save("project-documents", "brief.pdf", b"brief")
    gone = storage.save("task-attachments", "old.txt", b"old")
    assert mirror(buckets, "uploads", buckets, "uploads-backup") == {"copied": 2, "removed": 0, "total": 2}

    storage.delete(gone)  # e.g. an account deletion
    storage.save("task-attachments", "new.txt", b"new")
    assert mirror(buckets, "uploads", buckets, "uploads-backup") == {"copied": 1, "removed": 1, "total": 2}

    backed_up = {o["Key"] for o in buckets.list_objects_v2(Bucket="uploads-backup")["Contents"]}
    assert kept.split("/", 3)[3] in backed_up and gone.split("/", 3)[3] not in backed_up
    body = buckets.get_object(Bucket="uploads-backup", Key=kept.split("/", 3)[3])["Body"].read()
    assert body == b"brief"
    # Nothing changed: a second run is a no-op.
    assert mirror(buckets, "uploads", buckets, "uploads-backup")["copied"] == 0


def test_an_unconfigured_backup_says_so_instead_of_pretending(monkeypatch, caplog):
    monkeypatch.delenv("BACKUP_OBJECTS_BUCKET", raising=False)
    assert run_once() is None
    assert "not being backed up" in caplog.text
