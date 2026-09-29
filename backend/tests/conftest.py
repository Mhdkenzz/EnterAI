"""Suite-wide setup that has to happen before `app` is imported.

The auth rate limiters are per client IP, and every test in this suite shares one
client identity, so the production defaults would throttle the suite itself rather
than anything it is testing. Raising them here keeps the control switched on --
`test_auth_rate_limits.py` drives it directly with its own limits -- while letting
hundreds of logins in a run through.
"""
import os
import tempfile

# Without an explicit DATABASE_URL the app falls back to ./enterai.db, the local
# development database; a test run would fill it with hundreds of throwaway orgs.
# Default to a fresh file per run instead. CI and Postgres runs set their own.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="enterai-tests-"), "test.db"))
os.environ.setdefault("AUTH_LOGIN_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_RECOVERY_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_TOKEN_RATE_LIMIT_PER_MINUTE", "100000")
os.environ.setdefault("AUTH_ACCOUNT_FAILURES_PER_15_MIN", "100000")

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


def make_verified_provider(client, admin_headers, name, domain=None, role_map=None):
    """Create an identity provider whose email domain is genuinely verified.

    Returns (provider_id, scim_headers, domain).

    Existing SCIM/SSO tests provision users through a provider, and since
    domain ownership landed, a provider that has not proven its domain may not
    provision anything at all. The correct fix is for those tests to use a
    provider that has actually completed verification -- NOT to relax the
    production gate. This helper is that legitimate setup, in one place.

    `domain` must be unique per provider: exclusivity is global, so two
    providers cannot bind the same domain. When omitted a random one is
    generated, which is the normal case for a test that only needs a working
    provider.

    DNS is mocked here, exactly as the application would require in reality.
    A real deployment publishes the TXT record; a test asserts the provider
    would accept it. No test performs a real public DNS lookup.
    """
    import uuid as _uuid

    from unittest import mock

    from app import domain_verification

    if domain is None:
        safe = "".join(ch for ch in name.lower() if ch.isalnum()) or "idp"
        domain = f"{safe}-{_uuid.uuid4().hex[:10]}.example"

    payload = {"name": name, "protocol": "oidc", "domain_binding": domain}
    if role_map:
        payload["role_mapping_json"] = role_map
    created = client.post("/api/admin/sso/providers", headers=admin_headers,
                          json=payload)
    assert created.status_code == 200, created.text
    provider_id = created.json()["id"]

    txt_value = domain_verification.expected_txt_value(domain)
    with mock.patch.object(domain_verification, "lookup_txt",
                           return_value=[txt_value]):
        verified = client.post(
            f"/api/admin/sso/providers/{provider_id}/domain-verification",
            headers=admin_headers, json={"domain": domain})
    assert verified.status_code == 200, (
        f"domain verification failed for {domain}: {verified.text}")

    rotated = client.post(f"/api/admin/sso/providers/{provider_id}/scim-token",
                          headers=admin_headers)
    assert rotated.status_code == 200, rotated.text
    return (provider_id,
            {"Authorization": f'Bearer {rotated.json()["scim_token"]}'},
            domain)


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
