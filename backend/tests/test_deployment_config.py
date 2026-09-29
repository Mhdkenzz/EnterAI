from app.deployment_config import deployment_configuration_errors, migration_revision_status, registration_requires_invite


def _hosted_env(**updates):
    env = {
        "ENVIRONMENT": "staging", "DATABASE_URL": "postgresql+psycopg://db/enterai",
        "JWT_SECRET": "x" * 32, "REDIS_URL": "redis://redis:6379/0",
        "APP_BASE_URL": "https://staging.example.test", "API_BASE_URL": "https://api.staging.example.test",
        "CORS_ALLOWED_ORIGINS": "https://staging.example.test", "TRUSTED_HOSTS": "staging.example.test,api.staging.example.test",
        "TRUST_PROXY_HEADERS": "true", "SEED_ADMIN_EMAIL": "admin@example.test", "SEED_ADMIN_PASSWORD": "x" * 24,
        "EMAIL_PROVIDER": "smtp", "SMTP_HOST": "smtp.example.test", "EMAIL_FROM": "no-reply@example.test",
        "SECRETS_ENCRYPTION_KEY": "hosted-test-key",
        "TERMS_URL": "https://staging.example.test/terms", "PRIVACY_URL": "https://staging.example.test/privacy",
    }
    env.update(updates)
    return env


def test_staging_configuration_accepts_explicit_safe_values():
    assert deployment_configuration_errors(_hosted_env()) == []


def test_staging_configuration_rejects_local_fallbacks_and_wildcard_hosts():
    errors = deployment_configuration_errors(_hosted_env(API_BASE_URL="http://localhost:8000", TRUSTED_HOSTS="*"))
    assert "API_BASE_URL must be an HTTPS non-localhost URL" in errors
    assert "TRUSTED_HOSTS must name explicit non-local hosts" in errors


def test_hosted_configuration_requires_encryption_key_even_without_oidc():
    errors = deployment_configuration_errors(_hosted_env(SECRETS_ENCRYPTION_KEY=""))
    assert "SECRETS_ENCRYPTION_KEY is required in hosted environments" in errors


def test_hosted_legal_urls_must_be_public_https_urls():
    errors = deployment_configuration_errors(_hosted_env(
        TERMS_URL="http://staging.example.test/terms",
        PRIVACY_URL="https://localhost/privacy",
    ))
    assert "TERMS_URL must be an HTTPS non-localhost URL" in errors
    assert "PRIVACY_URL must be an HTTPS non-localhost URL" in errors


def test_development_does_not_require_hosted_configuration():
    assert deployment_configuration_errors({"ENVIRONMENT": "development"}) == []


def test_registration_is_closed_by_default_for_hosted_environments():
    assert registration_requires_invite({"ENVIRONMENT": "production"}) is True
    assert registration_requires_invite({"ENVIRONMENT": "staging"}) is True
    assert registration_requires_invite({"ENVIRONMENT": "test"}) is False


def test_registration_mode_requires_an_explicit_local_or_pilot_choice():
    assert registration_requires_invite({"ENVIRONMENT": "production", "REGISTRATION_MODE": "open"}) is False
    assert registration_requires_invite({"ENVIRONMENT": "production", "REGISTRATION_MODE": "pilot"}) is True
    # An invalid value fails closed instead of opening a public registration path.
    assert registration_requires_invite({"ENVIRONMENT": "production", "REGISTRATION_MODE": "unexpected"}) is True
