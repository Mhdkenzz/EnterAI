"""Audit regressions on an isolated database; never touch live tenant data."""
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session
from app.database import Base
from app.models import Organization, Project, Task, Team, User
from app.services import seed


def test_no_demo_content_bootstraps_only_admin(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed(db, demo_content=False)
        assert db.scalar(select(func.count()).select_from(Organization)) == 1
        assert db.scalar(select(func.count()).select_from(User)) == 1
        for model in (Project, Task, Team):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_seed_preserves_existing_tenant(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = Organization(name="Customer", slug="customer")
        db.add(org)
        db.commit()
        seed(db, demo_content=False)
        db.refresh(org)
        assert (org.name, org.slug) == ("Customer", "customer")
