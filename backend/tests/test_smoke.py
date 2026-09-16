def test_health_contract():
    from app.main import app
    assert app.title == "Enter AI API"

def test_ai_provider_requires_confirmation_for_creates():
    from app.services import AIProvider
    class P: id="1"; name="Atlas Launch"; health="on_track"
    result = AIProvider().plan("create a task to prepare handoff", [P()])
    assert result["actions"][0]["requires_confirmation"] is True
