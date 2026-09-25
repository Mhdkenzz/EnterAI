"""Create the upload bucket if it does not exist, then exit.

Run once before the API starts (`python -m app.ensure_bucket`), from the backend
image the API already uses, so bringing up object storage needs no separate client
image -- the stack previously depended on MinIO's `mc` image, which stopped being
publicly pullable. Waits for the storage server to accept connections first.
"""
import os
import sys
import time

from botocore.exceptions import ClientError, EndpointConnectionError

from .storage import _s3_client


def ensure_bucket(attempts: int = 60, delay: float = 2.0) -> None:
    bucket = os.environ["STORAGE_BUCKET"]
    client = _s3_client()
    region = os.getenv("STORAGE_REGION", "us-east-1")
    # AWS rejects an explicit us-east-1 constraint and requires one everywhere else.
    location = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
    for attempt in range(attempts):
        try:
            client.create_bucket(Bucket=bucket, **location)
            print(f"created bucket {bucket}")
            return
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                print(f"bucket {bucket} already exists")
                return
            raise
        except EndpointConnectionError:
            print(f"waiting for object storage ({attempt + 1}/{attempts})", file=sys.stderr)
            time.sleep(delay)
    raise SystemExit("object storage never became reachable")


if __name__ == "__main__":
    ensure_bucket()
