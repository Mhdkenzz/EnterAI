from app.ai.context_budget import cap_tool_result
from app.copilot import SYSTEM_PROMPT, CopilotProviderError, ProviderTurn, WorkspaceTools
from test_copilot_tool_calling import _ScriptedProvider, _login_and_load_user, _service_with


def test_system_prompt_contains_entity_substitution_guidance():
    # Regression guard: this wording is a real safety property (never silently
    # substitute a different real entity for one that wasn't found), not just
    # incidental prose -- catches someone silently reverting/rewording it away.
    assert "never substitute a different real" in SYSTEM_PROMPT
    assert "tell them it was not found" in SYSTEM_PROMPT


def test_get_comments_with_unknown_id_raises_not_found_never_returns_a_different_tasks_comments():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        try:
            tools.get_comments("definitely-does-not-exist-12345")
            assert False, "expected ValueError -- must never silently return a different real task's comments"
        except ValueError as exc:
            assert "definitely-does-not-exist-12345" in str(exc)
    finally:
        db.close()


def test_tool_calling_loop_raises_when_wall_clock_deadline_exceeded(monkeypatch):
    import app.copilot as copilot_module

    # Login/setup (rate limiting, JWT, etc.) may itself call time.monotonic(), so
    # the fake clock is only installed around the plan() call itself, not before
    # -- an absolute call-count assumption spanning unrelated setup code would be
    # fragile and coupled to implementation details this test has no business
    # knowing about.
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([ProviderTurn(text="should never be reached", tool_calls=[])])
        call_count = {"n": 0}

        def fake_monotonic():
            call_count["n"] += 1
            # First call computes the deadline (real elapsed time so far); every
            # call after that reports as if a huge amount of time has passed, so
            # the very first top-of-loop check trips without waiting for anything.
            return 0.0 if call_count["n"] == 1 else 1_000_000.0

        monkeypatch.setattr(copilot_module.time, "monotonic", fake_monotonic)
        try:
            _service_with(provider).plan("Hello", tools)
            assert False, "expected CopilotProviderError once the wall-clock deadline is exceeded"
        except CopilotProviderError as exc:
            assert "time budget" in str(exc)
        assert provider.seen_messages == []  # never even reached the model
    finally:
        db.close()


def test_tool_calling_loop_redirects_first_duplicate_call_without_reexecuting(monkeypatch):
    call_count = {"n": 0}
    real_get_projects = WorkspaceTools.get_projects

    def counting_get_projects(self):
        call_count["n"] += 1
        return real_get_projects(self)

    monkeypatch.setattr(WorkspaceTools, "get_projects", counting_get_projects)
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "get_projects", "args": {}}]),
            ProviderTurn(text=None, tool_calls=[{"id": "c2", "name": "get_projects", "args": {}}]),  # exact repeat
            ProviderTurn(text="Done.", tool_calls=[]),
        ])
        result = _service_with(provider).plan("List projects", tools)
        assert result["reply"] == "Done."
        assert call_count["n"] == 1  # the duplicate was never actually re-executed
        assert result["read_tools"] == ["get_projects"]  # counted once, not twice
        second_turn_result_message = provider.seen_messages[2][-1]
        assert second_turn_result_message["is_error"] is True
        assert "already called" in second_turn_result_message["content"]
    finally:
        db.close()


def test_tool_calling_loop_aborts_after_repeated_duplicates():
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        same_call = ProviderTurn(text=None, tool_calls=[{"id": "c", "name": "get_projects", "args": {}}])
        provider = _ScriptedProvider([same_call, same_call, same_call])
        try:
            _service_with(provider).plan("Loop on the same call", tools)
            assert False, "expected CopilotProviderError after a second duplicate of the same call"
        except CopilotProviderError as exc:
            assert "repeated the same tool call" in str(exc)
    finally:
        db.close()


def test_tool_calling_loop_treats_different_arguments_as_distinct_not_duplicates(monkeypatch):
    # Same tool name, different args -- must never be flagged as a duplicate.
    monkeypatch.setattr(WorkspaceTools, "get_comments", lambda self, task_id: [])
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "get_comments", "args": {"task_id": "task-a"}}]),
            ProviderTurn(text=None, tool_calls=[{"id": "c2", "name": "get_comments", "args": {"task_id": "task-b"}}]),
            ProviderTurn(text="Done.", tool_calls=[]),
        ])
        result = _service_with(provider).plan("Check comments on two tasks", tools)
        assert result["reply"] == "Done."
        assert result["read_tools"] == ["get_comments", "get_comments"]
    finally:
        db.close()


def test_cap_tool_result_leaves_small_results_unchanged():
    small = [{"id": "1", "name": "Task A"}]
    assert cap_tool_result(small, budget=1000) is small


def test_cap_tool_result_truncates_oversized_list_keeping_only_whole_items():
    big = [{"id": str(i), "title": "x" * 50} for i in range(500)]
    capped = cap_tool_result(big, budget=200)
    assert capped["truncated"] is True
    assert capped["of"] == 500
    assert 0 < capped["shown"] < 500
    assert capped["items"] == big[: capped["shown"]]  # whole leading items, never a partial one


def test_cap_tool_result_always_shows_at_least_one_item_even_if_it_alone_exceeds_budget():
    huge_item = [{"id": "1", "title": "x" * 10000}]
    capped = cap_tool_result(huge_item, budget=10)
    assert capped["truncated"] is True
    assert capped["shown"] == 1


def test_tool_calling_loop_caps_an_oversized_read_result_before_it_reaches_the_model(monkeypatch):
    monkeypatch.setattr(WorkspaceTools, "get_projects", lambda self: [{"id": str(i), "name": "x" * 200} for i in range(2000)])
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([
            ProviderTurn(text=None, tool_calls=[{"id": "c1", "name": "get_projects", "args": {}}]),
            ProviderTurn(text="Done.", tool_calls=[]),
        ])
        _service_with(provider).plan("List all projects", tools)
        tool_result_content = provider.seen_messages[1][-1]["content"]
        assert isinstance(tool_result_content, dict) and tool_result_content.get("truncated") is True
    finally:
        db.close()
