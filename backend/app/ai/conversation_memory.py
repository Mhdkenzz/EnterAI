"""Bounded conversation mechanism.

Supports conversation IDs, previous turns, tool call/result history,
compact summaries of older turns, and basic token budgeting.

Not yet integrated into copilot.py (that requires further design review),
but the protocol exists and is isolated.
"""
from __future__ import annotations
from typing import Any


class ConversationMemory:
    """Bounded conversation history with compact summary support."""

    def __init__(self, conversation_id: str, max_turns: int = 10):
        self.conversation_id = conversation_id
        self.max_turns = max_turns
        self.turns: list[dict[str, Any]] = []
        self.summaries: list[str] = []

    def add_user_turn(self, message: str) -> None:
        self.turns.append({"role": "user", "message": message})
        self._trim()

    def add_assistant_turn(self, text: str | None, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self.turns.append({"role": "assistant", "text": text, "tool_calls": tool_calls or []})
        self._trim()

    def add_tool_result(self, tool_call_id: str, result: Any) -> None:
        # Track results separately for reference resolution (e.g., "that project")
        if not self.turns:
            return
        last = self.turns[-1]
        if "results" not in last:
            last["results"] = {}
        last["results"][tool_call_id] = result

    def _trim(self) -> None:
        if len(self.turns) > self.max_turns:
            # Keep the most recent max_turns; optionally summarize older ones
            # For now, just drop oldest turns (future: summarize before drop)
            self.turns = self.turns[-self.max_turns:]

    def get_history_for_model(self) -> list[dict[str, Any]]:
        return self.turns

    def resolve_reference(self, phrase: str) -> dict[str, Any] | None:
        """Basic reference resolution: finds previous tool results or entity mentions."""
        # Minimal implementation; can be expanded with embedding/search
        for turn in reversed(self.turns):
            text = turn.get("message", "") or turn.get("text", "")
            if phrase.lower() in text.lower():
                return turn
        return None
