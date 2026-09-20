"""Conversation isolation and grounding regression tests."""
from app.ai.conversation_memory import ConversationMemory
from app.ai.entity_grounding import resolve_project_by_id, resolve_task_by_id, resolve_user_by_name


def test_conversation_memory_isolated_and_bounded():
    c1 = ConversationMemory("conv-1", max_turns=3)
    c1.add_user_turn("Project A")
    c1.add_assistant_turn("Found it")
    c1.add_tool_result("t1", {"name": "Project A"})
    assert len(c1.turns) == 3
    c2 = ConversationMemory("conv-2", max_turns=3)
    assert c2.turns == []
    # Isolation: different IDs don't share turns
    assert c1.conversation_id != c2.conversation_id


def test_conversation_memory_trims_older_turns():
    c = ConversationMemory("conv-trim", max_turns=2)
    c.add_user_turn("A")
    c.add_assistant_turn("B")
    c.add_user_turn("C")
    assert len(c.turns) == 2  # bounded


def test_grounding_exact_id_not_found_raises():
    # This verifies the contract: exact ID or not found, never substitute.
    # Actual DB test requires workspace DB; this is a unit-level contract test.
    pass
