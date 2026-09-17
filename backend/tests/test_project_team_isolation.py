"""Defensive tenant checks using only disposable local fixtures."""
import os
from contextlib import ExitStack
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.main import ProjectIn, ProjectUpdate, create_project, update_project, team_projects, teams
from app.models import Organization, Project, Team, User


@pytest.fixture
def workspace(tmp_path):
    # Explicit opt-in uses a pre-migrated disposable PostgreSQL database.
    # Bind commits to a savepoint so fixture writes roll back after each test.
    pg_url = os.getenv("ENTERAI_TEST_POSTGRES_URL")
    engine = create_engine(pg_url or f"sqlite:///{tmp_path / 'workspace.db'}")
    if not pg_url:
        Base.metadata.create_all(engine)
    with ExitStack() as stack:
        stack.callback(engine.dispose)
        connection = stack.enter_context(engine.connect())
        transaction = connection.begin()
        stack.callback(transaction.rollback)
        db = stack.enter_context(Session(bind=connection, join_transaction_mode="create_savepoint"))
        own = Organization(name="Local", slug="local")
        other = Organization(name="Other", slug="other")
        db.add_all([own, other]); db.flush()
        user = User(organization_id=own.id, name="Local admin", email="local@example.test",
                    password_hash="unused", role="admin", kind="human", active=True)
        local_team = Team(organization_id=own.id, name="Local team")
        foreign_team = Team(organization_id=other.id, name="Other team")
        db.add_all([user, local_team, foreign_team]); db.commit()
        yield db, user, local_team, foreign_team


def test_project_create_rejects_foreign_team_without_writes(workspace):
    db, user, _, foreign = workspace
    with pytest.raises(HTTPException) as exc:
        create_project(ProjectIn(name="Rejected", code="REJ", team_id=foreign.id), user, db)
    assert exc.value.status_code == 422
    assert db.scalar(select(Project.id)) is None


def test_team_reads_exclude_inconsistent_foreign_project(workspace):
    db, user, local, foreign = workspace
    # A pre-existing inconsistent association must not cross the read boundary.
    own = Project(organization_id=user.organization_id, team_id=local.id, name="Own", code="OWN")
    inconsistent = Project(organization_id=foreign.organization_id, team_id=local.id,
                           name="Other", code="OTHER")
    db.add_all([own, inconsistent]); db.commit()
    assert [p["id"] for p in team_projects(local.id, user, db)] == [own.id]
    assert teams(user, db)[0]["projects"] == 1


def test_project_create_accepts_local_and_unassigned_team(workspace):
    db, user, local, _ = workspace
    assert create_project(ProjectIn(name="Allowed", code="OK", team_id=local.id), user, db)["team"] == local.name
    assert create_project(ProjectIn(name="No team", code="NONE"), user, db)["team"] is None


@pytest.mark.parametrize("invalid_team", ["foreign", "missing", ""])
def test_update_rejects_invalid_team_without_mutation(workspace, invalid_team):
    db, user, local, foreign = workspace
    created = create_project(ProjectIn(name="Original", code="ORIG", team_id=local.id), user, db)
    team_id = foreign.id if invalid_team == "foreign" else invalid_team
    with pytest.raises(HTTPException) as exc:
        update_project(created["id"], ProjectUpdate(team_id=team_id, name="Changed"), user, db)
    assert exc.value.status_code == 422
    db.expire_all()
    row = db.get(Project, created["id"])
    assert row is not None
    assert (row.team_id, row.name) == (local.id, "Original")


@pytest.mark.parametrize("mode", ["assign", "clear", "omit"])
def test_update_preserves_supported_team_semantics(workspace, mode):
    db, user, local, _ = workspace
    initial = None if mode == "assign" else local.id
    created = create_project(ProjectIn(name="Original", code="ORIG", team_id=initial), user, db)
    update = ProjectUpdate(name="Changed") if mode == "omit" else ProjectUpdate(
        name="Changed", team_id=local.id if mode == "assign" else None)
    result = update_project(created["id"], update, user, db)
    db.expire_all()
    row = db.get(Project, created["id"])
    assert row is not None
    expected = None if mode == "clear" else local.id
    assert (row.team_id, row.name) == (expected, "Changed")
    assert result["team"] == (None if mode == "clear" else local.name)
