"""Fail-closed configuration and probe helpers for hosted deployments.

Development and CI deliberately retain their small local defaults.  Staging and
production do not: a typo must stop the process before it accepts traffic.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DEPLOYMENT_ENVIRONMENTS = {"staging", "production"}


def is_deployment_environment(value: str | None = None) -> bool:
    return (value if value is not None else os.getenv("ENVIRONMENT", "development")).strip().lower() in DEPLOYMENT_ENVIRONMENTS


def _is_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}


def deployment_configuration_errors(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    environment = env.get("ENVIRONMENT", "development").strip().lower()
    if environment not in {"development", "test", "staging", "production"}:
        return ["ENVIRONMENT must be development, test, staging, or production"]
    if not is_deployment_environment(environment):
        return []
    errors: list[str] = []
    required = ("DATABASE_URL", "JWT_SECRET", "REDIS_URL", "APP_BASE_URL", "API_BASE_URL",
                "CORS_ALLOWED_ORIGINS", "TRUSTED_HOSTS", "SEED_ADMIN_EMAIL", "SEED_ADMIN_PASSWORD")
    errors.extend(name for name in required if not env.get(name, "").strip())
    database_url = env.get("DATABASE_URL", "").strip()
    if database_url:
        try:
            if make_url(database_url).get_backend_name() != "postgresql":
                errors.append("DATABASE_URL must use PostgreSQL in hosted environments")
        except (ArgumentError, ValueError):
            errors.append("DATABASE_URL must use PostgreSQL in hosted environments")
    for name in ("APP_BASE_URL", "API_BASE_URL"):
        value = env.get(name, "").strip()
        if value and not _is_public_url(value):
            errors.append(f"{name} must be an HTTPS non-localhost URL")
    origins = [value.strip() for value in env.get("CORS_ALLOWED_ORIGINS", "").split(",") if value.strip()]
    if not origins or any(not _is_public_url(value) for value in origins):
        errors.append("CORS_ALLOWED_ORIGINS must contain HTTPS non-localhost origins")
    hosts = [value.strip() for value in env.get("TRUSTED_HOSTS", "").split(",") if value.strip()]
    if not hosts or "*" in hosts or any(value in {"localhost", "127.0.0.1"} for value in hosts):
        errors.append("TRUSTED_HOSTS must name explicit non-local hosts")
    if env.get("TRUST_PROXY_HEADERS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        errors.append("TRUST_PROXY_HEADERS must be enabled behind the staging reverse proxy")
    if env.get("EMAIL_PROVIDER", "").strip().lower() != "smtp":
        errors.append("EMAIL_PROVIDER must be smtp")
    for name in ("SMTP_HOST", "EMAIL_FROM"):
        if not env.get(name, "").strip():
            errors.append(name)
    if env.get("STORAGE_BACKEND", "").strip().lower() == "s3":
        for name in ("STORAGE_BUCKET", "STORAGE_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if not env.get(name, "").strip():
                errors.append(name)
    if env.get("OIDC_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"} and not env.get("SECRETS_ENCRYPTION_KEY", "").strip():
        errors.append("SECRETS_ENCRYPTION_KEY is required when OIDC_ENABLED=true")
    gmail_client_id = env.get("GMAIL_OAUTH_CLIENT_ID", "").strip()
    gmail_client_secret = env.get("GMAIL_OAUTH_CLIENT_SECRET", "").strip()
    if bool(gmail_client_id) != bool(gmail_client_secret):
        errors.append("GMAIL_OAUTH_CLIENT_ID and GMAIL_OAUTH_CLIENT_SECRET must be set together")
    if (gmail_client_id or gmail_client_secret) and not env.get("SECRETS_ENCRYPTION_KEY", "").strip():
        errors.append("SECRETS_ENCRYPTION_KEY is required when Gmail OAuth is configured")
    slack_client_id = env.get("SLACK_CLIENT_ID", "").strip()
    slack_client_secret = env.get("SLACK_CLIENT_SECRET", "").strip()
    slack_signing_secret = env.get("SLACK_SIGNING_SECRET", "").strip()
    if bool(slack_client_id) != bool(slack_client_secret):
        errors.append("SLACK_CLIENT_ID and SLACK_CLIENT_SECRET must be set together")
    if (slack_client_id or slack_client_secret) and not slack_signing_secret:
        errors.append("SLACK_SIGNING_SECRET is required when Slack OAuth is configured")
    if (slack_client_id or slack_client_secret or slack_signing_secret) and not env.get("SECRETS_ENCRYPTION_KEY", "").strip():
        errors.append("SECRETS_ENCRYPTION_KEY is required when Slack integration is configured")
    notion_client_id = env.get("NOTION_CLIENT_ID", "").strip()
    notion_client_secret = env.get("NOTION_CLIENT_SECRET", "").strip()
    if bool(notion_client_id) != bool(notion_client_secret):
        errors.append("NOTION_CLIENT_ID and NOTION_CLIENT_SECRET must be set together")
    if (notion_client_id or notion_client_secret) and not env.get("SECRETS_ENCRYPTION_KEY", "").strip():
        errors.append("SECRETS_ENCRYPTION_KEY is required when Notion OAuth is configured")
    return errors


def validate_deployment_configuration() -> None:
    errors = deployment_configuration_errors()
    if errors:
        raise RuntimeError("Unsafe deployment configuration; set or correct: " + ", ".join(sorted(set(errors))))


def migration_revision_status(connection) -> tuple[bool, list[str], list[str]]:
    """Return whether the database exactly matches this image's Alembic heads."""
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected = sorted(ScriptDirectory.from_config(config).get_heads())
    actual = sorted(MigrationContext.configure(connection).get_current_heads())
    return actual == expected, actual, expected
