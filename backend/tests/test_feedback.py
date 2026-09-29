from fastapi.testclient import TestClient

from app.main import app

from test_observability import identity, workspace


def test_member_feedback_is_redacted_and_admin_triage_is_org_scoped(monkeypatch):
    monkeypatch.setenv("RELEASE_SHA", "demo-2026.09.29")
    with TestClient(app) as client:
        admin_headers, account = workspace(client)
        org_id = account["organization"]["id"]
        member_id, member_headers = identity(org_id)
        submitted = client.post(
            "/api/feedback",
            headers=member_headers,
            json={
                "category": "bug",
                "impact": "high",
                "description": "The save button fails; token=top-secret and password=hunter2",
                "page": "https://demo.example.test/work?private=token",
                "client_version": "web-2026.09.29",
            },
        )
        assert submitted.status_code == 201, submitted.text
        report = submitted.json()
        assert report["description"].endswith("token=[REDACTED] and password=[REDACTED]")
        assert report["release_identifier"] == "demo-2026.09.29"
        assert report["page"] == "/work"
        assert report["diagnostics"] == {
            "environment": "development",
            "release": "demo-2026.09.29",
            "page": "/work",
            "client_version": "web-2026.09.29",
            "api_path": "/api/feedback",
        }
        assert "Authorization" not in str(report)
        assert client.get("/api/feedback", headers=member_headers).json()[0]["id"] == report["id"]
        assert client.get("/api/admin/feedback", headers=member_headers).status_code == 403

        triage = client.get("/api/admin/feedback", headers=admin_headers)
        assert triage.status_code == 200
        assert triage.json()["items"][0]["reporter"]["id"] == member_id
        updated = client.patch(
            f"/api/admin/feedback/{report['id']}",
            headers=admin_headers,
            json={"status": "investigating", "assignee_id": account["user"]["id"], "admin_note": "Investigating token=never-store"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["status"] == "investigating"
        assert updated.json()["assignee"]["id"] == account["user"]["id"]
        assert updated.json()["admin_note"] == "Investigating token=[REDACTED]"


def test_feedback_reports_never_cross_organization_boundaries():
    with TestClient(app) as client:
        _first_admin, first = workspace(client)
        second_admin, _second = workspace(client)
        _first_member_id, first_member = identity(first["organization"]["id"])
        created = client.post("/api/feedback", headers=first_member, json={
            "category": "usability", "impact": "low", "description": "The navigation is confusing for new users.",
        })
        assert created.status_code == 201
        report_id = created.json()["id"]
        assert client.get("/api/admin/feedback", headers=second_admin).json()["items"] == []
        assert client.patch(f"/api/admin/feedback/{report_id}", headers=second_admin,
                            json={"status": "closed"}).status_code == 404
        assert client.get("/api/feedback", headers=second_admin).json() == []
