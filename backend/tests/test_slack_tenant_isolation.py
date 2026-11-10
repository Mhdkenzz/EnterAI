"""A Slack bot workspace must not fan requests into multiple tenants."""
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.integrations import slack_api
from app.main import app
from app.models import IntegrationConnection, IntegrationOAuthState
from test_observability import workspace


def test_slack_workspace_bot_connection_is_exclusive_across_organizations(monkeypatch):
    with TestClient(app) as client:
        _, first = workspace(client)
        _, second = workspace(client)
    team_id = f"T{uuid4().hex[:10]}"
    state = uuid4().hex
    with SessionLocal() as db:
        first_user_id = first["user"]["id"]
        db.add(IntegrationConnection(
            organization_id=first["organization"]["id"], provider="slack",
            external_account_id=team_id, display_name="Shared Slack", status="connected",
            granted_scopes=slack_api.SLACK_BOT_SCOPES.split(","),
            access_token_ciphertext="encrypted", connected_by=first_user_id,
        ))
        db.add(IntegrationOAuthState(
            state_hash=sha256(state.encode()).hexdigest(), provider="slack",
            organization_id=second["organization"]["id"], user_id=second["user"]["id"],
            code_verifier_ciphertext="unused", expires_at=datetime.utcnow() + timedelta(minutes=5),
        ))
        db.commit()

    monkeypatch.setattr(slack_api, "exchange_code", lambda _code: {
        "access_token": "new-token", "team": {"id": team_id, "name": "Shared Slack"},
        "scope": slack_api.SLACK_BOT_SCOPES,
    })
    with TestClient(app) as client:
        response = client.get(
            "/api/integrations/slack/callback", params={"state": state, "code": "oauth-code"},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert "slack=workspace-in-use" in response.headers["location"]
    with SessionLocal() as db:
        rows = db.scalars(select(IntegrationConnection).where(
            IntegrationConnection.provider == "slack", IntegrationConnection.external_account_id == team_id,
        )).all()
        assert len(rows) == 1
        assert rows[0].organization_id == first["organization"]["id"]
