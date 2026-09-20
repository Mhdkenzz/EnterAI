"""Provider adapters: turn a system prompt + message history into a
ProviderTurn, using whatever wire format that provider's API expects.

Role-filtered tool specs and CopilotProviderError come from app.copilot,
imported lazily inside respond() rather than at module top, to avoid a
circular import -- copilot.py needs these classes to construct
CopilotService.provider, and these classes need copilot.py's role-filtered
tool-spec builders. The same pattern is already used by
WorkspaceTools.search_documents for its own app.embeddings import.
"""
from __future__ import annotations

import json
from typing import Any
from urllib import error, request

from .config import AISettings
from .profiles import resolve_profile
from .textual_toolcall_adapter import adapt_textual_tool_calls
from .turn import ProviderTurn


def _priority_tasks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        (task for task in snapshot["my_tasks"] if task["status"] != "done"),
        key=lambda task: (rank.get(task["priority"], 3), task["due_date"] or "9999-12-31"),
    )


class DeterministicCopilotProvider:
    """Useful, testable offline Copilot behaviour for local development."""

    def answer(self, message: str, snapshot: dict[str, Any]) -> str:
        normalized = message.lower().strip()
        projects = snapshot["projects"]
        if any(term in normalized for term in ("what should", "work on today", "priorit", "my tasks")):
            tasks = _priority_tasks(snapshot)
            if not tasks:
                return "You have no open assigned tasks. A good next step is to review your project risks and agree the next milestone."
            items = "; ".join(
                f"{task['title']} ({task['priority'].title()} · {task['project_name']})" for task in tasks[:3]
            )
            return f"Today, focus on {items}. I ranked open work by priority and due date."
        if "summar" in normalized or "project" in normalized:
            match = next(
                (project for project in projects if project["name"].lower() in normalized or project["code"].lower() in normalized),
                None,
            )
            if match:
                task_count = len([task for task in snapshot["tasks"] if task["project_name"] == match["name"] and task["status"] != "done"])
                description = match["description"] or "No project description has been added."
                return f"{match['name']} is {match['status'].replace('_', ' ')} and {match['health'].replace('_', ' ')}. {description} It has {task_count} open task{'s' if task_count != 1 else ''}."
        at_risk = [project["name"] for project in projects if project["health"] == "at_risk"]
        if "risk" in normalized:
            return f"At-risk projects: {', '.join(at_risk) if at_risk else 'none'}. Ask me to summarize a project or plan a confirmed task."
        return "I can use your workspace data to list projects, summarize a project, or prioritize your assigned tasks. I can also propose a task, but I will never create it without your confirmation."


class AnthropicCopilotProvider:
    """Structured tool-calling adapter for the Anthropic Messages API."""

    def __init__(self, settings: AISettings | None = None):
        settings = settings or AISettings.from_env()
        self.base_url = settings.base_url
        self.model = settings.model
        self.api_key = settings.api_key
        self.version = settings.anthropic_version
        # Timeout precedence: an explicit AI_TIMEOUT_SECONDS always wins (no existing
        # .env breaks); otherwise the resolved model's capability profile supplies a
        # speed-appropriate default instead of one flat number for every provider.
        self.profile = resolve_profile(settings.provider, settings.model)
        self.timeout = settings.timeout_seconds if settings.timeout_seconds is not None else self.profile.timeout_seconds

    def respond(self, system: str, messages: list[dict[str, Any]], role: str | None = None, extra_tools: list[dict[str, Any]] | None = None) -> ProviderTurn:
        from ..copilot import CopilotProviderError, anthropic_tool_specs
        if not self.api_key:
            raise CopilotProviderError("AI_API_KEY is not configured. Set it to use AI_PROVIDER=anthropic.")
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": messages,
            "tools": anthropic_tool_specs(role) + list(extra_tools or []),
        }).encode()
        headers = {"Content-Type": "application/json", "x-api-key": self.api_key, "anthropic-version": self.version}
        outgoing = request.Request(f"{self.base_url}/v1/messages", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:  # nosec B310 -- URL is explicit operator configuration
                payload = json.loads(response.read().decode())
        except (error.URLError, ValueError) as exc:
            raise CopilotProviderError("The configured AI provider is unavailable. Check AI_PROVIDER settings and try again.") from exc
        if "error" in payload:
            raise CopilotProviderError("The configured AI provider returned an error.")
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({"id": block.get("id"), "name": block.get("name"), "args": block.get("input") or {}})
        return ProviderTurn(text="\n".join(part for part in text_parts if part).strip() or None, tool_calls=tool_calls)

    def assistant_message(self, turn: ProviderTurn) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if turn.text:
            blocks.append({"type": "text", "text": turn.text})
        for call in turn.tool_calls:
            blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
        return {"role": "assistant", "content": blocks}

    def tool_result_message(self, tool_call_id: str, content: Any, is_error: bool = False) -> dict[str, Any]:
        text = content if isinstance(content, str) else json.dumps(content)
        block = {"type": "tool_result", "tool_use_id": tool_call_id, "content": text}
        if is_error:
            block["is_error"] = True
        return {"role": "user", "content": [block]}

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}


class OpenAICompatibleCopilotProvider:
    """Structured tool-calling adapter for LM Studio or any OpenAI-compatible endpoint."""

    def __init__(self, settings: AISettings | None = None):
        settings = settings or AISettings.from_env()
        self.base_url = settings.base_url
        self.model = settings.model
        self.api_key = settings.api_key
        self.profile = resolve_profile(settings.provider, settings.model)
        self.timeout = settings.timeout_seconds if settings.timeout_seconds is not None else self.profile.timeout_seconds

    def respond(self, system: str, messages: list[dict[str, Any]], role: str | None = None, extra_tools: list[dict[str, Any]] | None = None) -> ProviderTurn:
        from ..copilot import CopilotProviderError, openai_tool_specs
        extra = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in (extra_tools or [])]
        rendered_tools = openai_tool_specs(role) + extra
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": rendered_tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        outgoing = request.Request(f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:  # nosec B310 -- URL is explicit operator configuration
                payload = json.loads(response.read().decode())
            choice = payload["choices"][0]["message"]
        except (error.URLError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise CopilotProviderError("The configured AI provider is unavailable. Check AI_PROVIDER settings and try again.") from exc
        text = (choice.get("content") or "").strip() or None
        tool_calls: list[dict[str, Any]] = []
        for call in choice.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            tool_calls.append({"id": call.get("id"), "name": fn.get("name"), "args": args})

        # Only attempted when the resolved model profile says this model needs it
        # (see profiles.py) -- e.g. fabliq's native tool_calls are already reliable,
        # so this never runs for it. A model that already produced real tool_calls
        # never reaches here either; this only recovers a *leaked* call.
        adapter_used: str | None = None
        if not tool_calls and text and self.profile.needs_textual_adapter:
            recovered_calls, cleaned_text = adapt_textual_tool_calls(text, rendered_tools)
            if recovered_calls:
                tool_calls = recovered_calls
                text = cleaned_text
                adapter_used = "textual_v1"

        turn_result = ProviderTurn(text=text, tool_calls=tool_calls, adapter_used=adapter_used, provider_metadata={"provider":"openai_compatible","model":self.model,"profile_name":getattr(self.profile,"name",None)})
        try:
            from .telemetry import sanitize_metrics
            sanitize_metrics({"provider":"openai_compatible","model":self.model,"adapter_used":adapter_used,"profile_name":getattr(self.profile,"name",None)})
        except Exception:
            pass
        return turn_result

    def assistant_message(self, turn: ProviderTurn) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": turn.text}
        if turn.tool_calls:
            message["tool_calls"] = [
                {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["args"])}}
                for call in turn.tool_calls
            ]
        return message

    def tool_result_message(self, tool_call_id: str, content: Any, is_error: bool = False) -> dict[str, Any]:
        text = content if isinstance(content, str) else json.dumps(content)
        if is_error:
            text = f"Error: {text}"
        return {"role": "tool", "tool_call_id": tool_call_id, "content": text}

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}
