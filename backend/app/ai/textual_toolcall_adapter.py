"""Safe recovery of tool calls a model leaked as text instead of a native
provider response.

Generalized from the proven eval-only adapter at
/home/kenz/falcon_h1_3b_eval/workspace/backend/app/falcon_textual_toolcall_adapter.py,
which handled only Falcon's `<tool_call>...</tool_call>` leakage. This session's
own evaluation of fabliq-8b-agent-reasoning found two more leaked pseudo-tag
patterns (`<proposal>...</proposal>`, `<function-call>...</function-call>`),
neither ever a real tool call -- so the tag set is generalized rather than
hardcoded to one model's quirk, while every safety property of the original is
preserved exactly:

  - never eval() model output; only json.loads and ast.literal_eval (which
    parses literals only -- dict/list/str/num/bool/None -- and cannot execute
    code, call functions, or attribute-access anything).
  - a tool name is only accepted if it is in `available_tools`, which the
    caller MUST derive from the exact tool list actually offered to the model
    this turn (already role-filtered). This is what makes an admin-only or
    nonexistent tool name get rejected even if the model asks for it by name.
  - arguments are validated against that tool's JSON schema (see
    tool_validation.py). Any mismatch rejects the whole call -- no partial or
    best-effort argument repair.
  - fail closed: if nothing in the content parses into at least one valid
    call, this returns no calls and the ORIGINAL content untouched -- so
    behavior falls back to exactly what an unpatched provider would have
    done, leaked text included. This module never strips text it could not
    turn into a verified call; guessing at what to remove would be worse than
    leaving a visible leak.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

from .tool_validation import validate_tool_args

# Falcon leaks <tool_call>...</tool_call>; fabliq leaks <proposal>...</proposal>
# and <function-call>...</function-call> (both observed this session, neither
# ever containing a parseable {"name":..., "arguments":...} body in practice --
# see adapt_textual_tool_calls's docstring for why that's still handled safely).
# The backreference ties each closing tag to its own opening tag so a
# mismatched pair like <tool_call>...</proposal> is never matched.
_TOOL_CALL_BLOCK_RE = re.compile(
    r"<(?P<tag>tool_call|function-call|proposal)>\s*(?P<body>.*?)\s*</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_BARE_BOOL_NULL_RE = {
    "true": re.compile(r"(?<![\'\"\w])true(?![\'\"\w])"),
    "false": re.compile(r"(?<![\'\"\w])false(?![\'\"\w])"),
    "null": re.compile(r"(?<![\'\"\w])null(?![\'\"\w])"),
}


def _safe_parse_obj(raw: str) -> Any | None:
    """Parse a single tool-call block's body without ever executing code."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    # Some models mix Python-dict quoting with JSON's bare true/false/null.
    # Still only ever fed to ast.literal_eval -- never eval().
    normalized = raw
    normalized = _BARE_BOOL_NULL_RE["true"].sub("True", normalized)
    normalized = _BARE_BOOL_NULL_RE["false"].sub("False", normalized)
    normalized = _BARE_BOOL_NULL_RE["null"].sub("None", normalized)
    try:
        return ast.literal_eval(normalized)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def adapt_textual_tool_calls(
    content: str, available_tools: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract, validate, and convert leaked textual tool-call blocks.

    `available_tools` MUST be the exact OpenAI-format tool spec list offered
    to the model this turn (post role-filtering) -- this is the allow-list a
    call's `name` is checked against, and the source of each tool's argument
    schema. Returns `([], content)` unchanged if nothing valid was found --
    including the common case where a leaked tag's body is free prose (e.g.
    fabliq's observed "<proposal>Propose creating a task to investigate
    resource allocation...</proposal>") rather than a {"name", "arguments"}
    object: that's correctly treated as unrecoverable, not force-fit into a
    call, and the leak stays visible in `.text` exactly as it would without
    this module.
    """
    accepted: list[dict[str, Any]] = []
    matches = list(_TOOL_CALL_BLOCK_RE.finditer(content))
    if not matches:
        return [], content

    schema_by_name = {
        spec["function"]["name"]: spec["function"].get("parameters", {}) or {}
        for spec in available_tools
        if spec.get("type") == "function" and "function" in spec
    }

    for idx, match in enumerate(matches):
        obj = _safe_parse_obj(match.group("body"))
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str) or name not in schema_by_name:
            continue
        raw_args = obj.get("arguments", {})
        if raw_args is None:
            raw_args = {}
        validated_args = validate_tool_args(raw_args, schema_by_name[name])
        if validated_args is None:
            continue
        accepted.append({"id": f"textual-adapter-{idx}", "name": name, "args": validated_args})

    if not accepted:
        return [], content

    cleaned = _TOOL_CALL_BLOCK_RE.sub("", content).strip() or None
    return accepted, cleaned
