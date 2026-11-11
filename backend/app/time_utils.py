"""Date/time helpers that preserve the database's naive UTC convention."""

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Return current UTC as a naive datetime for existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
