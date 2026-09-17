"""Human approval boundaries; isolated identities and no live database."""
from typing import cast
import pytest
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.auth import current_user
from app.copilot import create_agent_confirmation, read_agent_confirmation
from app.database import get_db
from app.main import app
from app.models import User


class NoDatabaseAccess:
    def __getattr__(self, name):
        raise AssertionError("Reject nonhuman approval before database access")


@pytest.mark.parametrize("kind,active", [("agent", True), ("human", False)])
def test_confirmation_route_requires_active_human(kind, active):
    user = User(id="local-confirmer", organization_id="local-org", kind=kind,
                role="admin", active=active)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: NoDatabaseAccess()
    try:
        # Even an invalid proposal must not reach parsing/consumption for this user.
        response = TestClient(app).post("/api/ai/confirm", json={"confirmation_token": "invalid-local-proposal"})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("kind,active", [("agent", True), ("human", False)])
def test_agent_proposal_reader_requires_active_human(kind, active):
    proposer = User(id="local-proposer", organization_id="local-org", kind="agent", active=True)
    confirmer = User(id="local-confirmer", organization_id="local-org", kind=kind,
                     role="admin", active=active)
    token = create_agent_confirmation(proposer, "create_team", {"name": "Local proposal"})
    with pytest.raises(ValueError, match="active human"):
        read_agent_confirmation(token, confirmer, cast(Session, NoDatabaseAccess()))
