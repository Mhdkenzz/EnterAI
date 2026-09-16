def test_health_contract():
    from app.main import app
    assert app.title == "Enter AI API"

def test_copilot_provider_ranks_read_only_workspace_data():
    from app.copilot import DeterministicCopilotProvider
    result = DeterministicCopilotProvider().answer(
        "What should I work on today?",
        {"projects": [], "tasks": [], "my_tasks": [{"title": "Ship review", "priority": "high", "project_name": "Atlas", "status": "todo", "due_date": None}]},
    )
    assert "Ship review" in result
