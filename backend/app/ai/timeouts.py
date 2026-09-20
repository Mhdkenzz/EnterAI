"""Timeout and reasoning-model policy configuration.

Generalizes timeouts beyond a single profile timeout:
- AI_FIRST_TOKEN_TIMEOUT: max time before first token returned
- AI_TURN_TIMEOUT: max time per LLM turn
- AI_TOTAL_REQUEST_TIMEOUT: max total wall-clock time for the request
- AI_MAX_TOOL_ROUNDS: max rounds (matches profile max_tool_turns but can override)

Profile-driven defaults are preserved; environment overrides take precedence.
"""
from __future__ import annotations
from .profiles import resolve_profile, DEFAULT_PROFILE


def get_timeouts(provider: str, model: str, env_first_token: float | None = None, env_turn: float | None = None, env_total: float | None = None, env_max_rounds: int | None = None):
    profile = resolve_profile(provider, model)
    # Environment overrides > profile defaults
    return {
        "first_token_seconds": env_first_token if env_first_token is not None else profile.timeout_seconds * 0.3,
        "turn_seconds": env_turn if env_turn is not None else profile.timeout_seconds,
        "total_request_seconds": env_total if env_total is not None else profile.timeout_seconds * profile.max_tool_turns * 1.5,
        "max_rounds": env_max_rounds if env_max_rounds is not None else profile.max_tool_turns,
    }
