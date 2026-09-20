"""Shared argument validator for tool calls, used by both the native
message.tool_calls path and the textual tool-call compatibility adapter -- a
model-supplied argument dict is never trusted until it passes this check,
regardless of which path produced it.

Lifted verbatim from the proven eval-only adapter at
/home/kenz/falcon_h1_3b_eval/workspace/backend/app/falcon_textual_toolcall_adapter.py
(unit-tested there against 7 synthetic malformed/unknown-tool/schema-violation
cases, all correctly rejected), not rewritten, so its already-proven behavior
carries over exactly.
"""
from __future__ import annotations

from typing import Any

_JSON_TO_PY_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_tool_args(args: Any, schema: dict[str, Any]) -> dict[str, Any] | None:
    """Validate `args` against `schema` (required keys / additionalProperties /
    enum membership / coarse type check). Returns `args` unchanged if valid,
    or None if anything is wrong -- never a partially-repaired dict.
    """
    if not isinstance(args, dict):
        return None
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = schema.get("required", []) or []
    additional_ok = schema.get("additionalProperties", True) is not False
    for key in args:
        if key not in properties and not additional_ok:
            return None
    for key in required:
        if key not in args:
            return None
    for key, value in args.items():
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        expected_type = _JSON_TO_PY_TYPES.get(prop_schema.get("type", ""))
        if expected_type and not isinstance(value, expected_type):
            # bool is a subclass of int in Python; only reject genuine mismatches.
            if expected_type is int and isinstance(value, bool):
                return None
            if not (expected_type is (int, float) and isinstance(value, (int, float)) and not isinstance(value, bool)):
                if not isinstance(value, expected_type):
                    return None
        enum = prop_schema.get("enum")
        if enum and value not in enum:
            return None
    return args
