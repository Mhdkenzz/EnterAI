"""Suite-wide setup that has to happen before `app` is imported.

The auth rate limiters are per client IP, and every test in this suite shares one
client identity, so the production defaults would throttle the suite itself rather
than anything it is testing. Raising them here keeps the control switched on --
`test_auth_rate_limits.py` drives it directly with its own limits -- while letting
hundreds of logins in a run through.
"""
import os

os.environ.setdefault("AUTH_LOGIN_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_RECOVERY_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_TOKEN_RATE_LIMIT_PER_MINUTE", "100000")

import re

import pytest


@pytest.fixture
def outbox():
    """The console provider's captured messages, emptied around each test."""
    from app import email as mailer
    provider = mailer.get_email_provider()
    provider.outbox.clear()
    yield provider.outbox
    provider.outbox.clear()


def link_token(body: str, parameter: str) -> str:
    """Pull the single-use token out of an emailed link, the way a recipient would."""
    match = re.search(rf"[?&]{parameter}=([A-Za-z0-9_\-]+)", body)
    assert match, f"no {parameter} link in email body: {body!r}"
    return match.group(1)


# SQLite test DB initialization: create tables before any test that relies on them.
# Production uses PostgreSQL + Alembic; this only affects SQLite test runs.
from sqlalchemy import inspect
from app.database import DATABASE_URL, Base, engine


def pytest_configure(config):
    # Run Alembic migrations for SQLite test DB rather than relying on
    # Base.metadata.create_all(), which hides missing migrations.
    import app.models  # noqa: F401
    if "sqlite" in DATABASE_URL:
        from alembic import command
        from alembic.config import Config
        alembic_cfg = Config()
        # Derive absolute script_location from this file's location (backend/tests/conftest.py -> backend/alembic)
        import pathlib
        script_location = str(pathlib.Path(__file__).parent.parent / "alembic")
        alembic_cfg.set_main_option("script_location", script_location)
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
        command.upgrade(alembic_cfg, "head")
