from app.deployment_config import deployment_configuration_errors, migration_revision_status


def _hosted_env(**updates):
    env = {
        "ENVIRONMENT": "staging", "DATABASE_URL": "postgresql+psycopg://db/enterai",
        "JWT_SECRET": "x" * 32, "REDIS_URL": "redis://redis:6379/0",
        "APP_BASE_URL": "https://staging.example.test", "API_BASE_URL": "https://api.staging.example.test",
        "CORS_ALLOWED_ORIGINS": "https://staging.example.test", "TRUSTED_HOSTS": "staging.example.test,api.staging.example.test",
        "TRUST_PROXY_HEADERS": "true", "SEED_ADMIN_EMAIL": "admin@example.test", "SEED_ADMIN_PASSWORD": "x" * 24,
        "EMAIL_PROVIDER": "smtp", "SMTP_HOST": "smtp.example.test", "EMAIL_FROM": "no-reply@example.test",
    }
    env.update(updates)
    return env


def test_staging_configuration_accepts_explicit_safe_values():
    assert deployment_configuration_errors(_hosted_env()) == []


def test_staging_configuration_rejects_local_fallbacks_and_wildcard_hosts():
    errors = deployment_configuration_errors(_hosted_env(API_BASE_URL="http://localhost:8000", TRUSTED_HOSTS="*"))
    assert "API_BASE_URL must be an HTTPS non-localhost URL" in errors
    assert "TRUSTED_HOSTS must name explicit non-local hosts" in errors


def test_development_does_not_require_hosted_configuration():
    assert deployment_configuration_errors({"ENVIRONMENT": "development"}) == []
