"""Token-aware sizing for tool-result content.

Several read tools (get_tasks, get_projects, get_users, get_teams) return
unbounded lists today -- nothing stops a single result from being large
enough to dominate or exceed a model's context window on its own, especially
for the 16K-context local models this project also targets. Capping each
result at creation time (rather than only trying to shrink "the oldest"
message after the transcript has already grown across many turns) directly
addresses that: a single oversized result has no earlier message to defer to,
so it must be safe on its own.

Uses tiktoken for accurate counting when available, falling back to a
character-based estimate -- mirrors app/chunking.py's own established pattern
for the same optional dependency.
"""
from __future__ import annotations

import json
from typing import Any

try:
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None  # type: ignore

DEFAULT_TOOL_RESULT_TOKEN_BUDGET = 4000


def _get_encoding(model: str = "cl100k_base"):
    if tiktoken is None:
        return None
    try:
        return tiktoken.get_encoding(model)
    except Exception:
        return None


def count_tokens(text: str, encoding=None) -> int:
    encoding = encoding if encoding is not None else _get_encoding()
    if encoding is not None:
        return len(encoding.encode(text))
    return max(1, len(text) // 4)  # ~4 chars/token, same fallback ratio as chunking.py


def cap_tool_result(result: Any, budget: int = DEFAULT_TOOL_RESULT_TOKEN_BUDGET) -> Any:
    """Returns `result` unchanged if it already fits `budget` tokens once
    serialized. Otherwise returns a small, honest, truncated replacement --
    never a silently-cut-off blob the model could mistake for the whole
    answer. List results keep as many *whole* leading items as fit (never a
    partial item) and report how many were shown vs. how many existed.
    """
    text = result if isinstance(result, str) else json.dumps(result)
    encoding = _get_encoding()
    if count_tokens(text, encoding) <= budget:
        return result

    if isinstance(result, list):
        kept: list[Any] = []
        for item in result:
            if count_tokens(json.dumps(kept + [item]), encoding) > budget:
                break
            kept.append(item)
        if not kept and result:
            kept = [result[0]]  # always show the real shape of at least one item
        return {"truncated": True, "shown": len(kept), "of": len(result), "items": kept}

    truncated_text = text
    while len(truncated_text) > 0 and count_tokens(truncated_text, encoding) > budget:
        truncated_text = truncated_text[: max(1, int(len(truncated_text) * 0.9))]
    return {"truncated": True, "content": truncated_text}
