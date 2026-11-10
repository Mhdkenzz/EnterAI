import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.config import AISettings
from app.ai.providers import OpenAICompatibleCopilotProvider
from app.copilot import CopilotService
from app.database import SessionLocal
from app.main import app
from app.models import OrganizationAISettings, User
from app import secrets_store


def test_ifm_environment_uses_provider_specific_key_model_and_endpoint(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "ifm")
    monkeypatch.setenv("IFM_API_KEY", "ifm-test-key")
    monkeypatch.setenv("IFM_MODEL", "IFM/K2-Horizon-375B-A23B")
    monkeypatch.setenv("IFM_BASE_URL", "https://api.ifm.ai/v1")
    settings = AISettings.from_env()
    assert settings.provider == "ifm"
    assert settings.api_key == "ifm-test-key"
    assert settings.model == "IFM/K2-Horizon-375B-A23B"
    assert settings.base_url == "https://api.ifm.ai/v1"


def test_ifm_openai_compatible_adapter_sends_tools_and_replays_reasoning(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {
                "content": "",
                "reasoning_content": "I should inspect the workspace first.",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_projects", "arguments": "{}"}}],
            }}]}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.ai.providers.request.urlopen", fake_urlopen)
    settings = AISettings(
        provider="ifm", base_url="https://api.ifm.ai/v1", model="IFM/K2-Horizon-375B-A23B",
        api_key="ifm-test-key", timeout_seconds=90, anthropic_version="", embedding_dimension=1536,
    )
    provider = OpenAICompatibleCopilotProvider(settings)
    turn = provider.respond("Use tools carefully.", [{"role": "user", "content": "List projects"}], "admin")

    assert captured["url"] == "https://api.ifm.ai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer ifm-test-key"
    assert captured["body"]["model"] == "IFM/K2-Horizon-375B-A23B"
    assert captured["body"]["temperature"] == 1.0
    assert captured["body"]["top_p"] == 0.95
    assert captured["body"]["tools"]
    assert turn.reasoning_text == "I should inspect the workspace first."
    replay = provider.assistant_message(turn)
    assert replay["reasoning_content"] == turn.reasoning_text
    assert replay["tool_calls"][0]["function"]["name"] == "get_projects"


def test_workspace_settings_encrypt_and_select_ifm_key():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"email": "admin@demo.enterai.com", "password": "enterai-demo"})
        assert login.status_code == 200
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        response = client.patch("/api/admin/ai-provider", headers=headers, json={
            "provider": "ifm", "api_key": "ifm-test-secret", "requests_per_minute": 24,
        })
        assert response.status_code == 200
        assert response.json()["provider"] == "ifm"
        assert response.json()["configured_provider"] == "ifm"
        assert response.json()["model"] == "IFM/K2-Horizon-375B-A23B"
        assert response.json()["api_key_configured"] is True
        assert "ifm-test-secret" not in response.text
        user_id = login.json()["user"]["id"]

    db = SessionLocal()
    try:
        organization_id = db.get(User, user_id).organization_id
        saved = db.scalar(select(OrganizationAISettings).where(
            OrganizationAISettings.organization_id == organization_id,
        ))
        assert saved is not None
        assert saved.provider == "ifm"
        assert saved.api_key_encrypted != "ifm-test-secret"
        assert secrets_store.decrypt(saved.api_key_encrypted) == "ifm-test-secret"
        configured = CopilotService(db, organization_id)
        assert configured.mode == "tool_calling"
        assert configured.provider.provider == "ifm"
        assert configured.provider.base_url == "https://api.ifm.ai/v1"
        assert configured.provider.model == "IFM/K2-Horizon-375B-A23B"
    finally:
        db.close()
    with TestClient(app) as client:
        cleared = client.patch("/api/admin/ai-provider", headers=headers, json={"clear_api_key": True})
        assert cleared.status_code == 200
