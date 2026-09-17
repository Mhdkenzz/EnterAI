"""Pool configuration is inspected without contacting a database."""
import os
import subprocess
import sys


def test_postgres_pool_is_bounded_and_checks_stale_connections():
    script = '''
from app.database import engine
assert engine.pool._pre_ping is True
assert engine.pool.size() == 5
assert engine.pool._max_overflow == 10
assert engine.pool.timeout() == 30
assert engine.pool._recycle == 1800
engine.dispose()
'''
    env = {**os.environ, "DATABASE_URL": "postgresql+psycopg://localhost/unused"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_sqlite_memory_database_remains_supported():
    script = '''
from app.database import engine
from sqlalchemy import text
with engine.connect() as connection:
    assert connection.scalar(text("select 1")) == 1
'''
    result = subprocess.run([sys.executable, "-c", script],
                            env={**os.environ, "DATABASE_URL": "sqlite://"},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
