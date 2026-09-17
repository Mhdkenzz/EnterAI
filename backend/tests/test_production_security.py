"""Regression tests for production-only fail-closed behavior found during audit.

These changes only take effect when ENVIRONMENT=production; every existing test
suite runs without that variable set, so none of this can regress local/CI defaults.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _run_startup(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    # Startup no longer creates schema (Alembic is the sole schema path), so the
    # boot harness migrates first -- on a file-backed database, because in-memory
    # SQLite is per-thread and would not share the migrated schema with startup.
    boot_db = Path(tempfile.mkdtemp()) / "boot.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{boot_db}", "JWT_SECRET": "a" * 32, **env_overrides}
    script = (
        "import alembic.config, alembic.command\n"
        "alembic.command.upgrade(alembic.config.Config('alembic.ini'), 'head')\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app):\n"
        "    pass\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("password", [None, "", "enterai-demo", "short"])
def test_production_boot_requires_a_real_seed_admin_password(password):
    """A fresh production database with no organization yet must refuse to boot
    without an operator-chosen admin password -- the fixed demo login is published
    in this repo's README and must never be reachable in production."""
    env = {"ENVIRONMENT": "production"}
    env.pop("SEED_ADMIN_PASSWORD", None)
    if password is not None:
        env["SEED_ADMIN_PASSWORD"] = password
    result = _run_startup(env)
    assert result.returncode != 0
    assert "SEED_ADMIN_PASSWORD must be set" in result.stderr


def test_production_boot_succeeds_with_a_real_seed_admin_password():
    result = _run_startup({"ENVIRONMENT": "production", "SEED_ADMIN_PASSWORD": "a-genuinely-unique-password-123"})
    assert result.returncode == 0, result.stderr


def test_development_boot_keeps_the_fixed_demo_login_without_seed_admin_password():
    env = {"ENVIRONMENT": "development"}
    env.pop("SEED_ADMIN_PASSWORD", None)
    result = _run_startup(env)
    assert result.returncode == 0, result.stderr


def test_cors_allowed_origins_env_var_overrides_the_dev_default():
    script = (
        "import os\n"
        "os.environ['CORS_ALLOWED_ORIGINS'] = 'https://app.example.com, https://admin.example.com'\n"
        "from app.main import allowed_origins\n"
        "assert allowed_origins == ['https://app.example.com', 'https://admin.example.com'], allowed_origins\n"
    )
    env = {**os.environ, "DATABASE_URL": "sqlite://", "JWT_SECRET": "a" * 32}
    env.pop("CORS_ALLOWED_ORIGINS", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_cors_allowed_origins_falls_back_to_dev_defaults_when_unset():
    script = "from app.main import allowed_origins\nassert 'http://localhost:3000' in allowed_origins, allowed_origins\n"
    env = {**os.environ, "DATABASE_URL": "sqlite://", "JWT_SECRET": "a" * 32}
    env.pop("CORS_ALLOWED_ORIGINS", None)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
