"""Pure provider-dialect rendering from one canonical tool-spec list.

Takes an already role-filtered list of {"name", "description", "input_schema"}
dicts (see app/copilot.py's TOOL_SPECS) and renders it into the wire format a
given provider's tools/functions parameter expects. Knows nothing about
roles, WorkspaceTools, or which tools exist -- purely a dialect translator.
"""
from __future__ import annotations

from typing import Any


def render_anthropic(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in specs]


def render_openai(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in specs
    ]
