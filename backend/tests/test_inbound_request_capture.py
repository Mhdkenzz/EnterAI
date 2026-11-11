from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def _auth(client: TestClient):
    key = uuid4().hex
    response = client.post("/api/auth/register", json={
        "organization_name": f"Request workspace {key}",
        "name": "Request Admin",
        "email": f"request-{key}@example.com",
        "password": f"Request-password-{key}",
    })
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_manual_request_with_source_url_is_saved():
    with TestClient(app) as client:
        headers = _auth(client)
        response = client.post("/api/inbound-messages", headers=headers, json={
            "sender": "request@example.test",
            "subject": "Prepare a launch checklist",
            "body": "Create a launch checklist and assign owners.",
            "source_url": "https://example.test/launch-request",
        })

        assert response.status_code == 201, response.text
        assert response.json()["source_url"] == "https://example.test/launch-request"


def test_manual_request_rejects_invalid_or_oversized_source_url():
    with TestClient(app) as client:
        headers = _auth(client)
        base = {"body": "Create a launch checklist."}

        invalid = client.post("/api/inbound-messages", headers=headers, json={
            **base, "source_url": "not a URL",
        })
        oversized = client.post("/api/inbound-messages", headers=headers, json={
            **base, "source_url": "https://example.test/" + "a" * 2000,
        })

        assert invalid.status_code == 422
        assert oversized.status_code == 422
