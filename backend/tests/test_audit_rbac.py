"""Harmless permission tests using local dependency overrides."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.auth import current_user
from app.database import get_db
from app.models import User


@pytest.mark.parametrize("role", ["member", "manager", "viewer"])
@pytest.mark.parametrize("path,payload", [
    ("/api/teams", {"name": "Denied"}),
    ("/api/tasks/nonexistent/assign-agent", {"agent_id": "nonexistent"}),
])
def test_admin_mutation_routes_reject_nonadmins(role, path, payload):
    user = User(id="permission-test", organization_id="permission-org", role=role,
                kind="human", active=True)
    class NoDatabaseAccess:
        def __getattr__(self, name):
            raise AssertionError("Authorization must run before database operations")
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: NoDatabaseAccess()
    try:
        response = TestClient(app).post(path, json=payload)
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
