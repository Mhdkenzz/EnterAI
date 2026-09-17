"""Startup must consume a migrated schema, never create one."""
import os
import subprocess
import sys


def test_unmigrated_startup_does_not_create_schema(tmp_path):
    script = '''
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from app.main import app
from app.database import engine
try:
    with TestClient(app):
        pass
except OperationalError:
    pass
else:
    raise AssertionError("Startup accepted an unmigrated database")
assert inspect(engine).get_table_names() == []
'''
    env = {**os.environ, "ENVIRONMENT": "test", "DATABASE_URL": f"sqlite:///{tmp_path / 'fresh.db'}"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
