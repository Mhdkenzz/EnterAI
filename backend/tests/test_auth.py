import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("secret", [None, "", "   "])
def test_production_startup_requires_jwt_secret(secret):
    env = {**os.environ, "ENVIRONMENT": " production ", "DATABASE_URL": "sqlite://"}
    env.pop("JWT_SECRET", None)
    if secret is not None:
        env["JWT_SECRET"] = secret
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "JWT_SECRET must be set" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("secret", [
    "dev-secret-change-me",
    "change-me-in-production",
    "replace-this-with-a-long-random-production-secret",
    "short",
])
def test_production_startup_rejects_insecure_jwt_secret(secret):
    """A production deployment that copies the placeholder from .env.example or
    docker-compose.yml verbatim, or sets a too-short secret, must fail closed --
    otherwise anyone can forge auth tokens using the publicly known placeholder."""
    env = {**os.environ, "ENVIRONMENT": "production", "DATABASE_URL": "sqlite://", "JWT_SECRET": secret}
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "JWT_SECRET must be a unique, random value" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("environment,configured", [
    ("production", True), ("development", False), ("development", True),
    ("test", False), ("test", True),
])
def test_auth_and_copilot_share_startup_secret(environment, configured):
    import secrets

    # Production also refuses to boot without REDIS_URL (see ratelimit.py). The
    # client connects lazily, so naming an address is enough to get past that gate
    # and test the one thing this case is about: the signing secrets.
    env = {**os.environ, "ENVIRONMENT": environment, "DATABASE_URL": "sqlite://",
           "REDIS_URL": "redis://127.0.0.1:6379/0"}
    env.pop("JWT_SECRET", None)
    if configured:
        env["JWT_SECRET"] = secrets.token_hex(32)
    script = '''
import os
import secrets
import jwt
from app import auth, copilot, main
from app.models import User
from fastapi.security import HTTPAuthorizationCredentials

expected = os.environ.get("JWT_SECRET") or "dev-secret-change-me"
user = User(id="test-user", organization_id="test-org", active=True)
class Database:
    def scalar(self, statement):
        return user

session = auth.create_token(user)
assert jwt.decode(session, expected, algorithms=["HS256"])["sub"] == user.id
assert auth.current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=session), Database()) is user
args = {"title": "Review"}
confirmation = copilot.create_confirmation(user, "create_task", args)
# The confirmation family is signed with a key derived from (but distinct to) the
# session secret; the raw session secret must never verify a confirmation token.
try:
    jwt.decode(confirmation, expected, algorithms=["HS256"])
except jwt.InvalidSignatureError:
    pass
else:
    raise AssertionError("confirmation token verified with the raw session secret")
assert jwt.decode(confirmation, auth.CONFIRMATION_SECRET, algorithms=["HS256"])["kind"] == "copilot_confirmation"
# Runtime environment changes must not rotate only one token family.
os.environ["JWT_SECRET"] = secrets.token_hex(32)
assert copilot.read_confirmation(confirmation, user) == ("create_task", args)
assert jwt.decode(copilot.create_confirmation(user, "create_task", args), auth.CONFIRMATION_SECRET, algorithms=["HS256"])["sub"] == user.id
assert auth.current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=session), Database()) is user
'''
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    if configured:
        assert env["JWT_SECRET"] not in result.stderr


@pytest.mark.parametrize("user_id,organization_id", [
    ("other-user", "test-org"),
    ("test-user", "other-org"),
    ("other-user", "other-org"),
])
def test_confirmation_rejects_cross_user_or_organization(user_id, organization_id):
    from app.copilot import create_confirmation, read_confirmation
    from app.models import User

    owner = User(id="test-user", organization_id="test-org")
    token = create_confirmation(owner, "create_task", {"title": "Review"})
    recipient = User(id=user_id, organization_id=organization_id)
    with pytest.raises(ValueError, match="does not belong to your workspace"):
        read_confirmation(token, recipient)
