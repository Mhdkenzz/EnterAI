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


def test_copilot_provider_summarises_a_minimised_project_snapshot():
    from app.copilot import DeterministicCopilotProvider
    result = DeterministicCopilotProvider().answer(
        "Summarize AI Workspace",
        {
            "projects": [{"name": "AI Workspace", "code": "AIW", "status": "active", "health": "on_track", "description": "Safe actions"}],
            "tasks": [{"title": "Review policy", "status": "todo", "priority": "high", "project_name": "AI Workspace", "project_health": "on_track", "due_date": None}],
            "my_tasks": [],
        },
    )
    assert "It has 1 open task" in result
