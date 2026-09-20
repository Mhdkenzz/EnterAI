"""Canonical, provider-agnostic representation of one model turn.

Every provider adapter (Anthropic, OpenAI-compatible/LM Studio, the offline
deterministic fake) returns exactly this type, regardless of its own wire
format. Callers -- today just copilot.py's tool-calling loop -- read `.text`
and `.tool_calls` the same way no matter which provider produced them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class ProviderTurn:
    """One model turn: free text and/or a batch of tool calls to act on.

    `reasoning_text` is kept separate from `text` deliberately: a model's
    hidden chain-of-thought must never be silently concatenated into the
    user-facing reply. `adapter_used` is set when a textual tool-call
    compatibility parser (see textual_toolcall_adapter.py) recovered
    `tool_calls` from leaked text rather than a native provider response.
    """

    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    reasoning_text: str | None = None
    adapter_used: str | None = None
    # Provider-specific metadata (e.g. raw provider/model IDs, wire format info)
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    # Parse warnings emitted by adapter or native parser (e.g. partial parse, tag mismatch)
    parse_warnings: list[str] = field(default_factory=list)
    # Reference to raw response (e.g. file path, request ID, or truncated raw body reference)
    raw_response_ref: str | None = None
    # Timing metadata (ms): first_token_latency, turn_latency, total_latency, etc.
    timing_ms: dict[str, float] | None = None
