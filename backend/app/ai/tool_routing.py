"""Safe candidate-tool routing layer.

Purpose: reduce offered tool roster to improve latency/convergence
(Falcon multi-step weak, Fabliq multi-step >300s with full roster) while
preserving authorization. The router never grants permissions; it only
suggests which domains are relevant. Role filtering remains server-side
via openai_tool_specs(role) / anthropic_tool_specs(role).

Domains (deterministic keyword mapping):
- PROJECTS: "project", "projects", "health", "status", "at risk"
- TASKS: "task", "tasks", "to do", "todo", "priority", "assignee"
- DOCUMENTS: "document", "file", "uploaded", "according to"
- USERS: "user", "member", "team", "assign", "who"
- TEAMS: "team", "teams"
- ADMIN: "create team", "admin", "security"
- AGENTS: "agent", "delegate", "report"
- WRITES: "create task", "update task", "add comment", "propose"
- READ_ONLY: default when no domain detected; keeps basic reads.

Pipeline:
1. detect_domains(message) → candidate domains (deterministic keyword regex)
2. domain_to_candidates(domain) → list of tool names (canonical TOOL_SPECS mapping)
3. Combine, deduplicate, sort deterministically.
4. Role filtering applied by caller (copilot.py) using allowed_write_tools(role) + READ_TOOL_NAMES.
5. Low confidence (few/no keywords) widens to READ_ONLY + any mentioned domain.

Safety rules:
- Never offers tools not in TOOL_SPECS.
- Never changes RBAC (write tools filtered by role independently).
- Never removes required preflight (document intent preflight remains separate and unconditional for document keywords).
- General/no-tool conversation preserved (empty domain set falls back to READ_ONLY).
"""
from __future__ import annotations

import re

_DOCUMENT_INTENT_RE = re.compile(
    r"\b(document|documents|file|files|attachment|attachments|uploaded)\b"
    r"|according to the (uploaded )?(document|file)s?\b",
    re.IGNORECASE,
)

_DOMAIN_KEYWORDS = {
    "PROJECTS": re.compile(
        r"\b(project|projects|health|status|plan|launch|summary|summarize|at risk|blocked)\b",
        re.IGNORECASE,
    ),
    "TASKS": re.compile(
        r"\b(task|tasks|to do|todo|priority|assignee|due|blocked)\b",
        re.IGNORECASE,
    ),
    "DOCUMENTS": _DOCUMENT_INTENT_RE,
    "USERS": re.compile(
        r"\b(user|users|member|members|team|teams|assign|assignee|who|person)\b",
        re.IGNORECASE,
    ),
    "TEAMS": re.compile(r"\b(team|teams)\b", re.IGNORECASE),
    "ADMIN": re.compile(
        r"\b(admin|security|create team|new team|manage)\b", re.IGNORECASE,
    ),
    "AGENTS": re.compile(
        r"\b(agent|delegate|delegation|report|direct report)\b", re.IGNORECASE,
    ),
    "WRITES": re.compile(
        r"\b(create task|update task|add comment|propose|change|modify|make)\b",
        re.IGNORECASE,
    ),
}

_DOMAIN_CANDIDATES: dict[str, list[str]] = {
    "PROJECTS": ["get_projects", "search", "get_activity"],
    "TASKS": ["get_tasks", "get_my_tasks", "search", "get_comments"],
    "DOCUMENTS": ["search_documents", "get_projects"],
    "USERS": ["get_users", "get_teams", "get_direct_reports"],
    "TEAMS": ["get_teams"],
    "ADMIN": ["get_teams", "get_users", "get_projects"],
    "AGENTS": ["get_direct_reports", "get_tasks"],
    "WRITES": ["create_task", "update_task", "create_project", "update_project", "add_comment", "create_team", "delegate_task"],
    "READ_ONLY": [
        "get_projects", "get_tasks", "get_my_tasks", "get_teams",
        "get_users", "get_notifications", "get_activity", "search",
    ],
}


def detect_domains(message: str | None) -> set[str]:
    categories: set[str] = set()
    text = message or ""
    for domain, pattern in _DOMAIN_KEYWORDS.items():
        if pattern.search(text):
            categories.add(domain)
    # Always include READ_ONLY if nothing else detected, and DOCUMENTS
    # if any document keyword hits (preserves existing preflight contract).
    if not categories or "DOCUMENTS" in categories:
        categories.add("READ_ONLY")
    # Ensure DOCUMENTS remains if detected; READ_ONLY is base.
    if "DOCUMENTS" in categories:
        categories.discard("READ_ONLY")  # Let caller decide whether to include base reads separately
    return categories


def domain_candidates(domains: set[str]) -> list[str]:
    seen: list[str] = []
    # Always include base reads when domains are specific, so read context is available.
    if domains != {"READ_ONLY"} and domains != {"READ_ONLY", "DOCUMENTS"}:
        for name in _DOMAIN_CANDIDATES.get("READ_ONLY", []):
            if name not in seen:
                seen.append(name)
    for domain in sorted(domains):
        for name in _DOMAIN_CANDIDATES.get(domain, []):
            if name not in seen:
                seen.append(name)
    return seen


def preflight_tool_calls(message: str, scope_project_id: str | None) -> list[tuple[str, dict]]:
    """Returns [(tool_name, args), ...] to execute before the model's first turn.
    Preserves original behavior (document-intent preflight) and does not replace routing.
    """
    if "documents" in detect_intent_categories(message):
        args: dict = {"query": message}
        if scope_project_id:
            args["project_id"] = scope_project_id
        return [("search_documents", args)]
    return []


def detect_intent_categories(message: str | None) -> set[str]:
    categories: set[str] = set()
    if message and _DOCUMENT_INTENT_RE.search(message):
        categories.add("documents")
    return categories
