"""Uploads must land somewhere every replica can reach.

The S3 cases run against moto, which serves the real botocore wire protocol in
process, so these exercise the same code path that talks to MinIO and to AWS.
"""
import os
from pathlib import Path

import pytest

from app.storage import LocalStorage, S3Storage, get_storage

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

BUCKET = "enterai-test-bucket"


@pytest.fixture
def s3():
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture(autouse=True)
def _restore_environment():
    keys = ("STORAGE_BACKEND", "STORAGE_BUCKET", "STORAGE_ENDPOINT_URL", "STORAGE_ROOT",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION")
    before = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in before.items():
        os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)


def test_s3_save_stores_the_bytes_and_returns_a_locatable_identifier(s3):
    storage = S3Storage(BUCKET, s3)
    identifier = storage.save("task-attachments", "report.pdf", b"file-bytes")

    assert identifier.startswith(f"s3://{BUCKET}/task-attachments/")
    assert identifier.endswith("-report.pdf")
    key = identifier.split(f"s3://{BUCKET}/", 1)[1]
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"file-bytes"


def test_every_replica_reads_what_any_replica_wrote(s3):
    """The whole point of object storage here: a second replica, holding its own
    client, must see the upload the first one took."""
    first = S3Storage(BUCKET, s3)
    second = S3Storage(BUCKET, boto3.client("s3", region_name="us-east-1"))
    identifier = first.save("project-documents", "brief.txt", b"shared-bytes")
    key = identifier.split(f"s3://{BUCKET}/", 1)[1]
    assert second._client.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"shared-bytes"


def test_saved_keys_are_unique_per_upload(s3):
    storage = S3Storage(BUCKET, s3)
    first = storage.save("task-attachments", "same.txt", b"one")
    second = storage.save("task-attachments", "same.txt", b"two")
    assert first != second
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix="task-attachments/")["Contents"]
    assert len(listed) == 2


@pytest.mark.parametrize("filename,expected_suffix", [
    ("../../etc/passwd", "-passwd"),
    ("/absolute/path/report.pdf", "-report.pdf"),
    ("", "-upload"),
])
def test_hostile_filenames_cannot_escape_the_key_prefix(s3, filename, expected_suffix):
    identifier = S3Storage(BUCKET, s3).save("task-attachments", filename, b"x")
    key = identifier.split(f"s3://{BUCKET}/", 1)[1]
    assert key.startswith("task-attachments/")
    assert ".." not in key
    assert key.endswith(expected_suffix)


def test_local_storage_also_refuses_hostile_filenames(tmp_path):
    stored = Path(LocalStorage(tmp_path).save("task-attachments", "../../escape.txt", b"x"))
    assert ".." not in str(stored)
    assert stored.parent == tmp_path / "task-attachments"
    assert stored.name.endswith("-escape.txt")


def test_get_storage_selects_the_configured_backend(tmp_path):
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["STORAGE_ROOT"] = str(tmp_path)
    assert isinstance(get_storage(), LocalStorage)

    os.environ["STORAGE_BACKEND"] = "s3"
    os.environ["STORAGE_BUCKET"] = BUCKET
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    assert isinstance(get_storage(), S3Storage)


def test_s3_backend_without_a_bucket_fails_closed():
    os.environ["STORAGE_BACKEND"] = "s3"
    os.environ.pop("STORAGE_BUCKET", None)
    with pytest.raises(RuntimeError) as error:
        get_storage()
    assert "STORAGE_BUCKET" in str(error.value)


def test_an_unknown_backend_is_refused_rather_than_silently_local():
    os.environ["STORAGE_BACKEND"] = "gcs"
    with pytest.raises(RuntimeError) as error:
        get_storage()
    assert "gcs" in str(error.value)
