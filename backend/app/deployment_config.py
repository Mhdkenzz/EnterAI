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

DEPLOYMENT_ENVIRONMENTS = {"staging", "production"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def is_deployment_environment(value: str | None = None) -> bool:
    return (value if value is not None else os.getenv("ENVIRONMENT", "development")).strip().lower() in DEPLOYMENT_ENVIRONMENTS


def registration_requires_invite(environ: dict[str, str] | None = None) -> bool:
    """Hosted environments default to invite-only; local/CI stays self-serve."""
    env = os.environ if environ is None else environ
    raw = env.get("INVITE_ONLY_REGISTRATION", "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    mode = env.get("REGISTRATION_MODE", "").strip().lower()
    if mode in {"invite_only", "invite-only", "pilot"}:
        return True
    if mode in {"open", "self_serve", "self-serve"}:
        return False
    return is_deployment_environment(env.get("ENVIRONMENT", "development"))


def _is_public_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}


def deployment_configuration_errors(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    if not is_deployment_environment(env.get("ENVIRONMENT", "development")):
        return []
    errors: list[str] = []
    required = ("DATABASE_URL", "JWT_SECRET", "REDIS_URL", "APP_BASE_URL", "API_BASE_URL",
                "CORS_ALLOWED_ORIGINS", "TRUSTED_HOSTS", "SEED_ADMIN_EMAIL", "SEED_ADMIN_PASSWORD",
                "TERMS_URL", "PRIVACY_URL")
    errors.extend(name for name in required if not env.get(name, "").strip())
    for name in ("APP_BASE_URL", "API_BASE_URL", "TERMS_URL", "PRIVACY_URL"):
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
    if not env.get("SECRETS_ENCRYPTION_KEY", "").strip():
        errors.append("SECRETS_ENCRYPTION_KEY is required in hosted environments")
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
