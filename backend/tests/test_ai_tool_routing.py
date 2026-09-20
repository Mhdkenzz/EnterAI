from app.ai.tool_routing import detect_intent_categories, preflight_tool_calls
from app.copilot import ProviderTurn, WorkspaceTools
from test_copilot_tool_calling import _ScriptedProvider, _login_and_load_user, _service_with


def test_detect_intent_categories_matches_document_referencing_phrasing():
    for message in (
        "According to the uploaded document, why is the permit delayed?",
        "Search the project documents for vendor payment terms.",
        "What does the attached file say about the budget?",
        "Can you check the attachments on this task?",
    ):
        assert "documents" in detect_intent_categories(message), message


def test_detect_intent_categories_is_empty_for_ordinary_questions():
    for message in ("Which projects are at risk?", "Create a task for the launch.", "wut projcts r blocked rn"):
        assert detect_intent_categories(message) == set()


def test_preflight_tool_calls_targets_search_documents_with_the_message_as_query():
    calls = preflight_tool_calls("According to the uploaded document, why is the permit delayed?", None)
    assert calls == [("search_documents", {"query": "According to the uploaded document, why is the permit delayed?"})]


def test_preflight_tool_calls_respects_project_scope():
    calls = preflight_tool_calls("What do the documents say?", "proj-123")
    assert calls == [("search_documents", {"query": "What do the documents say?", "project_id": "proj-123"})]


def test_preflight_tool_calls_empty_for_ordinary_questions():
    assert preflight_tool_calls("Which projects are at risk?", None) == []


def test_preflight_fires_search_documents_before_the_models_first_turn(monkeypatch):
    # search_documents itself needs real pgvector, unavailable on this suite's SQLite
    # engine (see app/indexing.py's search_similar_chunks) -- monkeypatched here so
    # this test exercises the SPLICING logic this increment actually adds, not the
    # separate, pre-existing, out-of-scope SQLite/pgvector engine gap.
    monkeypatch.setattr(WorkspaceTools, "search_documents", lambda self, query, project_id=None, limit=5: [
        {"document_id": "doc-1", "document_name": "permit_memo.txt", "chunk_index": 0, "snippet": "Permit delayed 12 days.", "token_count": 6, "citation": {}},
    ])
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([ProviderTurn(text="The permit is delayed due to a pending city review.", tool_calls=[])])
        result = _service_with(provider).plan("According to the uploaded document, why is the permit delayed?", tools)

        assert result["reply"] == "The permit is delayed due to a pending city review."
        # Regression (bug #6): search_documents actually executed, not skipped.
        assert result["read_tools"] == ["search_documents"]

        # It appears before the model's first turn: the model's own respond() call
        # only ever saw ONE prior message list -- and that list already contains the
        # preflight's tool-use/tool-result pair ahead of the model doing anything.
        assert len(provider.seen_messages) == 1
        first_call_messages = provider.seen_messages[0]
        assert first_call_messages[0]["content"] == "According to the uploaded document, why is the permit delayed?"
        assert first_call_messages[1]["tool_calls"][0]["name"] == "search_documents"
        assert first_call_messages[2]["role"] == "tool"
        assert first_call_messages[2]["is_error"] is False
        assert "Permit delayed 12 days." in str(first_call_messages[2]["content"])
    finally:
        db.close()


def test_preflight_does_not_count_against_max_tool_turns(monkeypatch):
    from app.copilot import MAX_TOOL_TURNS
    monkeypatch.setattr(WorkspaceTools, "search_documents", lambda self, query, project_id=None, limit=5: [])
    monkeypatch.setattr(WorkspaceTools, "get_comments", lambda self, task_id: [])
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        # Exactly MAX_TOOL_TURNS scripted model turns (the max the real loop can ever
        # consume): MAX_TOOL_TURNS-1 that call a tool with a DIFFERENT argument each
        # time (get_comments' loop-hardening duplicate-call detection would otherwise
        # short-circuit repeats of the exact same call, which isn't what this test is
        # about), then one final answer. If the preflight wrongly consumed one
        # iteration of that same budget, the model would only get MAX_TOOL_TURNS-1
        # turns and plan() would raise instead of ever reaching "Done." -- so this
        # only passes if the preflight is free.
        turns = [
            ProviderTurn(text=None, tool_calls=[{"id": f"c{i}", "name": "get_comments", "args": {"task_id": f"task-{i}"}}])
            for i in range(MAX_TOOL_TURNS - 1)
        ]
        turns.append(ProviderTurn(text="Done.", tool_calls=[]))
        provider = _ScriptedProvider(turns)
        result = _service_with(provider).plan("According to the uploaded document, what's the status?", tools)
        assert result["reply"] == "Done."
    finally:
        db.close()


def test_preflight_degrades_gracefully_without_crashing_when_search_documents_fails(monkeypatch):
    # Engine-independent: forces the failure via monkeypatch rather than relying on
    # this suite's SQLite/pgvector incompatibility. That incompatibility was real
    # and was exactly what this test originally exercised unintentionally -- but
    # increment 9 fixed search_documents's real query path (it was missing a
    # ::vector cast on its bound parameter), so it now genuinely succeeds against
    # real Postgres, which would make an engine-coupled version of this test
    # start failing there for the wrong reason (proving the fix, not a
    # regression). The property under test -- broad exception handling degrades
    # gracefully, without leaking exception/SQL text -- has nothing to do with
    # which engine is configured, so it shouldn't depend on one.
    def always_fails(self, query, project_id=None, limit=5):
        raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(WorkspaceTools, "search_documents", always_fails)
    db, user = _login_and_load_user()
    try:
        tools = WorkspaceTools(db, user)
        provider = _ScriptedProvider([ProviderTurn(text="Here is what I found.", tool_calls=[])])
        result = _service_with(provider).plan("According to the uploaded document, why is the permit delayed?", tools)
        assert result["reply"] == "Here is what I found."
        assert result["read_tools"] == []
        tool_result = provider.seen_messages[0][2]
        assert tool_result["is_error"] is True
        assert "simulated transient failure" not in str(tool_result["content"])
        assert "vector" not in str(tool_result["content"]).lower()
        assert "sql" not in str(tool_result["content"]).lower()
    finally:
        db.close()
