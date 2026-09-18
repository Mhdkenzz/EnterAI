"""The email provider abstraction, its production gate, and first-admin onboarding."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app import email as mailer
from app.main import app

from test_observability import identity, workspace


@pytest.fixture(autouse=True)
def _restore_email_environment():
    keys = ("EMAIL_PROVIDER", "EMAIL_FROM", "SMTP_HOST", "SMTP_PORT", "APP_BASE_URL", "ENVIRONMENT")
    before = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in before.items():
        os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
    mailer.reset_email_provider()


def test_the_default_provider_keeps_mail_on_the_machine():
    mailer.reset_email_provider()
    os.environ.pop("EMAIL_PROVIDER", None)
    assert isinstance(mailer.get_email_provider(), mailer.ConsoleEmailProvider)


def test_smtp_is_selected_and_configured_from_the_environment():
    mailer.reset_email_provider()
    os.environ.update({"EMAIL_PROVIDER": "smtp", "SMTP_HOST": "smtp.example.com",
                       "SMTP_PORT": "2525", "EMAIL_FROM": "bot@example.com"})
    provider = mailer.get_email_provider()
    assert isinstance(provider, mailer.SMTPEmailProvider)
    assert (provider.host, provider.port, provider.sender) == ("smtp.example.com", 2525, "bot@example.com")


def test_an_unknown_provider_is_refused_rather_than_silently_dropped():
    mailer.reset_email_provider()
    os.environ["EMAIL_PROVIDER"] = "carrier-pigeon"
    with pytest.raises(RuntimeError) as error:
        mailer.get_email_provider()
    assert "carrier-pigeon" in str(error.value)


def test_a_failing_provider_never_breaks_the_endpoint_that_sent_it(monkeypatch):
    """A send that raised would turn a generic 202 into a 500 for real addresses
    only -- an enumeration oracle built out of error handling."""
    class Exploding:
        def send(self, _message):
            raise RuntimeError("relay refused")

    monkeypatch.setattr(mailer, "_provider", Exploding())
    mailer.send(mailer.EmailMessage("someone@example.com", "Subject", "Body"))  # must not raise


def _boot(env_overrides):
    boot_db = Path(tempfile.mkdtemp()) / "boot.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{boot_db}", "JWT_SECRET": "a" * 32,
           "REDIS_URL": "redis://127.0.0.1:6379/0", "SEED_ADMIN_PASSWORD": "a-genuinely-unique-password-123",
           **env_overrides}
    script = ("import alembic.config, alembic.command\n"
              "alembic.command.upgrade(alembic.config.Config('alembic.ini'), 'head')\n"
              "from fastapi.testclient import TestClient\nfrom app.main import app\n"
              "with TestClient(app):\n    pass\n")
    return subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)


def test_production_refuses_to_boot_without_a_real_email_provider():
    result = _boot({"ENVIRONMENT": "production", "EMAIL_PROVIDER": "console"})
    assert result.returncode != 0
    assert "EMAIL_PROVIDER must be set to 'smtp' in production" in result.stderr


def test_production_refuses_to_boot_with_smtp_but_no_delivery_configuration():
    result = _boot({"ENVIRONMENT": "production", "EMAIL_PROVIDER": "smtp",
                    "SMTP_HOST": "", "EMAIL_FROM": "", "APP_BASE_URL": ""})
    assert result.returncode != 0
    assert "must be set in production for transactional email" in result.stderr


def test_production_boots_once_email_is_configured():
    result = _boot({"ENVIRONMENT": "production", "EMAIL_PROVIDER": "smtp",
                    "SMTP_HOST": "smtp.example.com", "EMAIL_FROM": "bot@example.com",
                    "APP_BASE_URL": "https://app.example.com"})
    assert result.returncode == 0, result.stderr


def test_onboarding_reports_real_workspace_state_and_can_be_completed(outbox):
    with TestClient(app) as c:
        headers, account = workspace(c)
        state = c.get("/api/onboarding", headers=headers).json()
        steps = {step["key"]: step["done"] for step in state["steps"]}
        assert steps == {"verify_email": False, "create_project": False, "invite_team": False}
        assert state["complete"] is False and state["can_invite"] is True

        c.post("/api/projects", headers=headers, json={"name": "First", "code": "FIRST"})
        c.post("/api/auth/invites", headers=headers, json={"email": "colleague@example.com"})
        verification = next(m for m in outbox if "Confirm" in m.subject and m.to == account["user"]["email"])
        from conftest import link_token
        c.post("/api/auth/verify-email", json={"token": link_token(verification.body, "verify")})

        state = c.get("/api/onboarding", headers=headers).json()
        assert all(step["done"] for step in state["steps"]), state["steps"]

        assert c.post("/api/onboarding/complete", headers=headers).status_code == 200
        assert c.get("/api/onboarding", headers=headers).json()["complete"] is True


def test_only_a_human_admin_can_complete_onboarding():
    with TestClient(app) as c:
        _headers, account = workspace(c)
        _member_id, member_headers = identity(account["organization"]["id"], "member")
        assert c.get("/api/onboarding", headers=member_headers).json()["can_invite"] is False
        assert c.post("/api/onboarding/complete", headers=member_headers).status_code == 403


def test_onboarding_state_does_not_leak_across_organizations():
    with TestClient(app) as c:
        headers_a, _account_a = workspace(c)
        headers_b, account_b = workspace(c)
        c.post("/api/projects", headers=headers_a, json={"name": "Only A", "code": "ONLYA"})
        state_b = {step["key"]: step["done"] for step in c.get("/api/onboarding", headers=headers_b).json()["steps"]}
        assert state_b["create_project"] is False
        assert c.get("/api/onboarding", headers=headers_b).json()["organization"] == account_b["organization"]["name"]
