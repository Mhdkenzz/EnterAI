"""Deterministic, side-effect-limited task delivery checks."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agent_workspace import repo_check
from .models import Project, Task, User
from .task_dependencies import unmet_dependency_ids


def run_task_quality_checks(db: Session, task: Task) -> list[dict]:
    """Run built-in consistency checks and fixed repository validations only.

    No task-provided shell commands are executed. Repository checks are the
    registered, bounded checks in agent_workspace.repo_check.
    """
    checks: list[dict] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail[:500],
                       "checked_at": datetime.now(timezone.utc).isoformat()})

    criteria = task.acceptance_criteria or []
    rubric = task.review_rubric or []
    evidence = task.qa_evidence or []
    record("acceptance_criteria", bool(criteria) and all(isinstance(item, str) and item.strip() for item in criteria),
           f"{len(criteria)} acceptance criteria recorded.")
    record("review_rubric", bool(rubric) and all(isinstance(item, str) and item.strip() for item in rubric),
           f"{len(rubric)} review rubric items recorded.")
    record("evidence", bool(evidence) and all(isinstance(item, str) and item.strip() for item in evidence),
           f"{len(evidence)} evidence items recorded.")

    project = db.get(Project, task.project_id)
    owner = db.get(User, task.assignee_id) if task.assignee_id else None
    owner_ok = bool(owner and project and owner.organization_id == project.organization_id
                    and owner.active and owner.retired_at is None)
    record("owner", owner_ok, "Task owner is active." if owner_ok else "Task must have an active owner.")

    unmet = unmet_dependency_ids(db, task.id)
    record("prerequisites", not unmet,
           "All prerequisites are complete." if not unmet else f"{len(unmet)} prerequisite task(s) remain open.")
    open_subtask = db.scalar(select(Task.id).where(Task.parent_id == task.id, Task.status != "done").limit(1))
    record("subtasks", open_subtask is None,
           "All subtasks are complete." if open_subtask is None else "One or more subtasks remain open.")
    independent_review_ok = not task.peer_review_agent_id or task.peer_review_status == "approved"
    if task.peer_review_agent_id:
        independent_detail = ("Independent review was approved by a human."
                              if independent_review_ok else "Independent review is not yet approved by a human.")
    else:
        independent_detail = "No independent agent review was requested."
    record("independent_review", independent_review_ok, independent_detail)

    if "repository" in (task.labels or []):
        for check_name in ("python_syntax", "diff_whitespace"):
            try:
                result = repo_check({"check": check_name}, project.organization_id if project else "", db)
                detail = result.get("output") or (
                    f"Scanned {result.get('files_scanned', 0)} Python files; "
                    f"{len(result.get('errors', []))} syntax error(s)."
                )
                record(check_name, result.get("passed", False), str(detail))
            except (ValueError, OSError) as error:
                record(check_name, False, str(error))

    return checks
