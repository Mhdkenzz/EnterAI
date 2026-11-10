"""Notion intake must process pages tied at the durable timestamp watermark."""
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.integrations import IntegrationConnection
from app.main import app
from app.models import InboundMessage
from app.notion_sync import _sync_one
from test_observability import workspace


def test_new_page_at_exact_timestamp_watermark_is_not_skipped(monkeypatch):
    watermark = "2026-11-01T12:00:00.000Z"
    new_page_id, old_page_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        headers, account = workspace(client)
    organization_id = account["organization"]["id"]
    with SessionLocal() as db:
        connection = IntegrationConnection(
            organization_id=organization_id, provider="notion", external_account_id=str(uuid4()),
            display_name="Workspace", status="connected", granted_scopes=[],
            access_token_ciphertext="unused", connected_by=account["user"]["id"],
            history_cursor=watermark,
        )
        db.add(connection)
        db.commit()

        monkeypatch.setattr("app.notion_sync.notion_api.get_access_token", lambda _connection: "token")
        monkeypatch.setattr("app.notion_sync.notion_api.search_pages", lambda _token, _cursor: {
            "results": [
                {"id": new_page_id, "last_edited_time": watermark, "url": "https://notion.example/new"},
                {"id": old_page_id, "last_edited_time": "2026-10-31T23:59:59.000Z", "url": "https://notion.example/old"},
            ],
            "has_more": False,
        })
        monkeypatch.setattr("app.notion_sync.notion_api.retrieve_page", lambda _token, page_id: {
            "id": page_id,
            "last_edited_time": watermark,
            "url": f"https://notion.example/{page_id}",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "New shared page"}]}},
            "last_edited_by": {"id": "editor"},
        })
        monkeypatch.setattr("app.notion_sync.notion_api.block_children", lambda *_args: {"results": [], "has_more": False})

        assert _sync_one(db, connection) == 1
        rows = db.scalars(select(InboundMessage).where(
            InboundMessage.organization_id == organization_id, InboundMessage.source == "notion",
        )).all()
        assert len(rows) == 1
        assert rows[0].external_id == f"{new_page_id}:{watermark}"
        assert rows[0].subject == "New shared page"
