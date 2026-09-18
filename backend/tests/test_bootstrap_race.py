"""Replicas all boot against the same database and all run seed().

On an empty database every replica sees no organization and tries to create the
same seed rows. The unique constraints decide the race; the replicas that lose it
must carry on serving rather than dying during startup.
"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import Organization
from app.services import seed


class _EmptyDatabase:
    """Stands in for what every replica sees when they boot together against a
    fresh deployment: no organization yet, so all of them try to create one."""

    def __init__(self):
        self.rolled_back = False

    def scalar(self, _statement):
        return None

    def rollback(self):
        self.rolled_back = True


def test_losing_the_bootstrap_race_is_not_a_startup_failure(monkeypatch):
    def already_taken(*_args, **_kwargs):
        raise IntegrityError("duplicate key", None, Exception("organizations_slug_key"))

    monkeypatch.setattr("app.services._bootstrap", already_taken)
    db = _EmptyDatabase()

    seed(db)  # the replica that lost the race must still finish starting up

    assert db.rolled_back, "the failed transaction must be rolled back, not left open"


def test_a_genuine_bootstrap_failure_is_not_swallowed(monkeypatch):
    """Only the race is tolerated. Anything else (a bad password policy, a broken
    schema) must still stop the process rather than boot a half-seeded workspace."""
    def broken(*_args, **_kwargs):
        raise RuntimeError("SEED_ADMIN_PASSWORD must be set")

    monkeypatch.setattr("app.services._bootstrap", broken)
    try:
        seed(_EmptyDatabase())
    except RuntimeError as error:
        assert "SEED_ADMIN_PASSWORD" in str(error)
    else:
        raise AssertionError("a non-race bootstrap failure must propagate")


def test_an_already_seeded_database_is_left_alone():
    with SessionLocal() as db:
        before = {org.id: (org.name, org.slug) for org in db.scalars(select(Organization)).all()}
        seed(db)
        after = {org.id: (org.name, org.slug) for org in db.scalars(select(Organization)).all()}
        assert after == before
