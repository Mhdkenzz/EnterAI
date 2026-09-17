"""Exercise actual Alembic commands against a disposable SQLite file."""
import os
import subprocess
import sys
from pathlib import Path


def test_sqlite_migration_round_trip(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'chain.db'}"}
    backend = Path(__file__).resolve().parents[1]
    for direction, revision in [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]:
        result = subprocess.run([sys.executable, "-m", "alembic", direction, revision],
                                cwd=backend, env=env, capture_output=True, text=True)
        assert result.returncode == 0, f"{direction} {revision}: {result.stderr}"
