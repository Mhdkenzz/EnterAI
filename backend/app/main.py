import os
import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Literal
import jwt
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import AnyHttpUrl, BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .auth import CONFIRMATION_SECRET, CSRF_HEADER, SESSION_COOKIE, clear_session_cookie, revoke_session_token, create_token, current_user, hash_password, set_session_cookie, verify_password
from .database import Base, engine, get_db
from .time_utils import utcnow_naive
from .models import Activity, AgentMessage, AgentNextStep, Attachment, AuditEvent, Comment, CopilotConfirmation, Decision, HierarchyConfig, InboundMessage, InboundPlan, Notification, Organization, Project, ProjectDocument, ProjectMilestone, Task, TaskDependency, Team, User
from .copilot import AgentAccessDenied, CopilotProviderError, CopilotService, TOOL_ROLES, WorkspaceTools, read_agent_confirmation, read_confirmation
from .agent_access import agent_project_access, agent_tool_allowed
from .agent_workspace import REPO_PATCH_NAME, apply_confirmed_repo_patch
from .observability import execution_allowed, audit_context, audit, provider_call, legacy_detail, configure_logging, configure_error_reporting
from . import execution, gmail_sync
from .hierarchy import MAX_HIERARCHY_AGENTS, desired_counts, reconcile_agents
from .services import ProjectDraftProvider, advance_project_to_completion_review, ensure_hierarchy_config, extract_document_text, log, seed, sync_agent_task_pointers
from .request_planning import InboundPlanProposal, create_inbound_plan
from .storage import get_storage
from .indexing import index_document
from .task_dependencies import list_dependencies, replace_dependencies, unmet_dependency_ids
from .quality import run_task_quality_checks
from .knowledge_access import document_is_visible, is_workspace_manager, task_is_visible, visible_project_ids
from .deployment_config import is_deployment_environment, migration_revision_status, validate_deployment_configuration
from .ratelimit import OpenRouterRateLimitExceeded

environment = os.getenv("ENVIRONMENT", "development").strip().lower()
demo_seed_enabled = os.getenv("ENABLE_DEMO_SEED", "false" if is_deployment_environment(environment) else "true").strip().lower() in {"1", "true", "yes", "on"}
docs_enabled = os.getenv("ENABLE_API_DOCS", "false" if is_deployment_environment(environment) else "true").strip().lower() in {"1", "true", "yes", "on"}
# Before the app is built, so the SDK's FastAPI integration hooks every request.
configure_error_reporting()


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_deployment_configuration()
    configure_logging()
    with next(get_db()) as db:
        seed(db, demo_content=demo_seed_enabled)
    from .billing_service import sync_plans_from_env
    with next(get_db()) as db:
        sync_plans_from_env(db)
    execution.start_background_loop()
    gmail_sync.start_gmail_sync_loop()
    privacy_service.start_background_loop()
    from . import observability as _observability
    _observability.start_aggregation_background_loop()
    mailer.start_retry_loop()
    try:
        yield
    finally:
        await execution.stop_background_loop()
        await gmail_sync.stop_gmail_sync_loop()
        await privacy_service.stop_background_loop()
        await _observability.stop_aggregation_background_loop()
        await mailer.stop_retry_loop()


app = FastAPI(
    title="Enter AI API",
    version="0.1.0",
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Apply defensive browser headers to every API response.

    The API serves JSON and upload responses, so a strict policy is safe here.
    Swagger UI is development-only and gets a narrowly scoped policy because
    FastAPI's generated page loads its CDN assets when docs are enabled.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Embedder-Policy", "require-corp")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")

    if request.url.path in {"/docs", "/redoc"} and docs_enabled:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'",
        )
    else:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'",
        )

    # Authenticated API responses and uploaded document metadata must never be
    # stored by an intermediary or browser cache.
    response.headers.setdefault("Cache-Control", "no-store, max-age=0")
    response.headers.setdefault("Pragma", "no-cache")
    if request.url.scheme == "https" or environment == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def custom_openapi():
    """Advertise the auth and domain errors returned by every API operation."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    error_responses = {
        "400": "Bad request",
        "401": "Authentication required",
        "403": "Forbidden",
        "404": "Not found",
        "409": "Conflict",
    }
    for path in schema.get("paths", {}).values():
        for operation in path.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            for status, description in error_responses.items():
                operation["responses"].setdefault(status, {"description": description})
    app.openapi_schema = schema
    return schema


from .admin import router as admin_router, human_admin
from . import accounts, email as mailer
from .accounts import router as accounts_router, enforce, _LOGIN_LIMITER, _SIGNUP_LIMITER
from .billing_webhook import router as billing_webhook_router
from .billing_routes import router as billing_router
from .integrations import router as integrations_router
from .privacy import router as privacy_router, admin_router as admin_privacy_router
from . import privacy_service
from . import secrets_store
from .sso_routes import router_admin as sso_admin_router, router_login as sso_login_router, router_scim as scim_router
from .enterprise_routes import router as enterprise_router
app.include_router(admin_router)
app.include_router(sso_admin_router)
app.include_router(sso_login_router)
app.include_router(scim_router)
app.include_router(enterprise_router)
app.include_router(accounts_router)
app.include_router(billing_webhook_router)
app.include_router(billing_router)
app.include_router(integrations_router)
app.include_router(privacy_router)
app.include_router(admin_privacy_router)
app.openapi = custom_openapi
_default_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:6767",
    "http://127.0.0.1:6767",
]
_configured_origins = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
allowed_origins = [origin.strip() for origin in _configured_origins.split(",") if origin.strip()] or _default_origins
_configured_hosts = [host.strip() for host in os.getenv("TRUSTED_HOSTS", "").split(",") if host.strip()]
allowed_hosts = _configured_hosts or ["localhost", "127.0.0.1", "testserver", "[::1]"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", CSRF_HEADER],
)

# Upper bounds matter as much as lower ones: an unbounded password is hashed with
# Argon2 in full on every attempt, and names past the column width fail as a 500.
class SignIn(BaseModel): email: str = Field(min_length=3, max_length=255); password: str = Field(max_length=200)
class Register(BaseModel): organization_name: str = Field(min_length=2, max_length=160); name: str = Field(min_length=1, max_length=160); email: EmailStr; password: str = Field(min_length=8, max_length=200); accept_terms: bool = False
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=180); code: str = Field(min_length=1, max_length=16); description: str | None = None; team_id: str | None = None; health: Literal["on_track", "at_risk", "off_track"] = "on_track"; color: str = "#22c55e"; due_date: datetime | None = None; source_document_ids: list[str] = []
    north_star_goal: str | None = Field(default=None, max_length=2000); success_metric: str | None = Field(default=None, max_length=180); metric_unit: str | None = Field(default=None, max_length=60); baseline_value: float | None = None; current_value: float | None = None; target_value: float | None = None
class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=180); code: str | None = Field(default=None, min_length=1, max_length=16); description: str | None = None; status: Literal["planning", "active", "on_hold", "completed", "archived"] | None = None; health: Literal["on_track", "at_risk", "off_track"] | None = None; color: str | None = None; team_id: str | None = None; due_date: datetime | None = None
    north_star_goal: str | None = Field(default=None, max_length=2000); success_metric: str | None = Field(default=None, max_length=180); metric_unit: str | None = Field(default=None, max_length=60); baseline_value: float | None = None; current_value: float | None = None; target_value: float | None = None
class MilestoneIn(BaseModel): title: str = Field(min_length=1, max_length=180); description: str | None = Field(default=None, max_length=2000); due_date: datetime | None = None
class MilestoneUpdate(BaseModel): title: str | None = Field(default=None, min_length=1, max_length=180); description: str | None = Field(default=None, max_length=2000); due_date: datetime | None = None
class TaskIn(BaseModel): project_id: str; title: str = Field(min_length=1, max_length=240); description: str | None = None; parent_id: str | None = None; milestone_id: str | None = None; status: Literal["backlog", "todo", "in_progress", "review", "done"] = "todo"; priority: Literal["low", "medium", "high"] = "medium"; assignee_id: str | None = None; due_date: datetime | None = None; labels: list[str] = []; depends_on: list[str] = Field(default_factory=list); qa_required: bool = False; requires_human_approval: bool = False; acceptance_criteria: list[str] = Field(default_factory=list, max_length=20); review_rubric: list[str] = Field(default_factory=list, max_length=20)
class TaskUpdate(BaseModel): title: str | None = Field(default=None, min_length=1, max_length=240); description: str | None = None; status: Literal["backlog", "todo", "in_progress", "review", "blocked", "done"] | None = None; priority: Literal["low", "medium", "high"] | None = None; assignee_id: str | None = None; milestone_id: str | None = None; due_date: datetime | None = None; labels: list[str] | None = None; depends_on: list[str] | None = None; qa_required: bool | None = None; requires_human_approval: bool | None = None; acceptance_criteria: list[str] | None = Field(default=None, max_length=20); review_rubric: list[str] | None = Field(default=None, max_length=20); qa_evidence: list[str] | None = Field(default=None, max_length=50)
class QAPeerReviewRequest(BaseModel): reviewer_agent_id: str
class QAPeerReviewDecision(BaseModel): outcome: Literal["approved", "changes_requested"]; summary: str = Field(min_length=8, max_length=2000)
class CommentIn(BaseModel): body: str = Field(min_length=1)
class TeamIn(BaseModel): name: str; description: str | None = None
class AIRequest(BaseModel): message: str = Field(min_length=1, max_length=4000); project_id: str | None = None
class ConfirmAction(BaseModel): confirmation_token: str = Field(min_length=20, max_length=10000)
class AssignAgent(BaseModel): agent_id: str
class AgentMessageIn(BaseModel): message: str = Field(min_length=1, max_length=4000)
class InboundMessageCreate(BaseModel):
    source: Literal["gmail", "slack", "notion", "manual"] = "manual"
    external_id: str | None = Field(default=None, min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, max_length=500)
    sender: str | None = Field(default=None, max_length=500)
    subject: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=50000)
    source_url: AnyHttpUrl | None = Field(default=None, max_length=2000)
    received_at: datetime | None = None
class InboundMessageStatus(BaseModel):
    status: Literal["new", "ignored"]
class InboundPlanEdit(BaseModel):
    summary: str = Field(min_length=1, max_length=2000)
    proposal: InboundPlanProposal
class HierarchyConfigUpdate(BaseModel):
    vp_count: int | None = Field(default=None, ge=0)
    directors_per_vp: int | None = Field(default=None, ge=0)
    managers_per_director: int | None = Field(default=None, ge=0)
    workers_per_manager: int | None = Field(default=None, ge=0)

def user_out(user: User): return {"id":user.id,"name":user.name,"email":user.email,"role":user.role,"kind":user.kind,"title":user.title,"avatar":user.avatar,"email_verified":user.email_verified_at is not None}
def _north_star_values(values: dict[str, Any], existing: Project | None = None) -> dict[str, Any]:
    result = {name: getattr(existing, name, None) for name in (
        "north_star_goal", "success_metric", "metric_unit", "baseline_value", "current_value", "target_value",
    )} if existing else {}
    result.update(values)
    goal = (result.get("north_star_goal") or "").strip()
    metric = (result.get("success_metric") or "").strip()
    fields_present = bool(goal or metric or result.get("baseline_value") is not None
                         or result.get("current_value") is not None or result.get("target_value") is not None)
    if not fields_present:
        return result
    if not goal or not metric or result.get("target_value") is None:
        raise HTTPException(422, "A measurable project goal needs a north-star outcome, a named success metric, and a target value")
    baseline = result.get("baseline_value")
    current = result.get("current_value")
    target = result.get("target_value")
    if baseline is None:
        baseline = 0.0
        result["baseline_value"] = baseline
    if current is None:
        current = baseline
        result["current_value"] = current
    if baseline == target:
        raise HTTPException(422, "The metric target must differ from its baseline")
    if not all(value == value and abs(value) != float("inf") for value in (baseline, current, target)):
        raise HTTPException(422, "Project metric values must be finite numbers")
    result["north_star_goal"] = goal
    result["success_metric"] = metric
    result["metric_unit"] = (result.get("metric_unit") or "count").strip() or "count"
    return result


def _metric_progress(project: Project) -> float | None:
    if project.baseline_value is None or project.target_value is None or project.current_value is None:
        return None
    span = project.target_value - project.baseline_value
    if span == 0:
        return None
    return round(max(0.0, min(100.0, (project.current_value - project.baseline_value) / span * 100)), 1)


def milestone_out(milestone: ProjectMilestone, db: Session) -> dict[str, Any]:
    tasks = db.scalars(select(Task).where(
        Task.project_id == milestone.project_id, Task.milestone_id == milestone.id, Task.parent_id.is_(None),
    )).all()
    total = len(tasks)
    done = sum(task.status == "done" for task in tasks)
    blocked = sum(task.status == "blocked" for task in tasks)
    status = "completed" if total and done == total else "blocked" if blocked else "in_progress" if any(task.status in {"in_progress", "review"} for task in tasks) else "planned"
    assignments = []
    for task in tasks:
        assignee = db.get(User, task.assignee_id) if task.assignee_id else None
        assignments.append({"id": task.id, "title": task.title, "status": task.status,
            "priority": task.priority, "assignee_name": assignee.name if assignee else None,
            "assignee_kind": assignee.kind if assignee else None})
    return {"id": milestone.id, "project_id": milestone.project_id, "title": milestone.title,
            "description": milestone.description, "due_date": milestone.due_date,
            "status": status, "progress": round(done / total * 100) if total else 0,
            "task_count": total, "completed_task_count": done, "blocked_task_count": blocked,
            "tasks": assignments,
            "created_at": milestone.created_at, "updated_at": milestone.updated_at}


def next_step_out(suggestion: AgentNextStep, db: Session) -> dict[str, Any]:
    agent = db.get(User, suggestion.agent_id)
    task = db.get(Task, suggestion.source_task_id)
    milestone = db.get(ProjectMilestone, suggestion.milestone_id) if suggestion.milestone_id else None
    return {"id": suggestion.id, "project_id": suggestion.project_id, "source_task_id": suggestion.source_task_id,
            "source_task_title": task.title if task else "Task", "agent_id": suggestion.agent_id,
            "agent_name": agent.name if agent else "Agent", "milestone_id": suggestion.milestone_id,
            "milestone_title": milestone.title if milestone else None, "title": suggestion.title,
            "description": suggestion.description, "rationale": suggestion.rationale,
            "priority": suggestion.priority, "status": suggestion.status,
            "created_task_id": suggestion.created_task_id, "created_at": suggestion.created_at}


def _require_project_lead(project: Project, user: User) -> None:
    if user.kind != "human" or not user.active or (user.role not in {"admin", "manager"} and project.owner_id != user.id):
        raise HTTPException(403, "Only the project owner, a manager, or an administrator can change project coordination")


def project_out(p: Project, db: Session):
    total = db.query(Task).filter(Task.project_id == p.id, Task.parent_id.is_(None)).count(); done = db.query(Task).filter(Task.project_id == p.id, Task.status == "done", Task.parent_id.is_(None)).count()
    owner = db.get(User,p.owner_id) if p.owner_id else None; team = db.get(Team,p.team_id) if p.team_id else None
    documents = db.scalars(select(ProjectDocument).where(ProjectDocument.project_id == p.id)).all()
    milestones = db.scalars(select(ProjectMilestone).where(ProjectMilestone.project_id == p.id).order_by(ProjectMilestone.due_date, ProjectMilestone.created_at)).all()
    north_star = None
    if p.north_star_goal:
        north_star = {"goal": p.north_star_goal, "metric": p.success_metric, "unit": p.metric_unit,
                      "baseline": p.baseline_value, "current": p.current_value, "target": p.target_value,
                      "progress": _metric_progress(p)}
    return {"id":p.id,"name":p.name,"code":p.code,"description":p.description,"status":p.status,"health":p.health,"color":p.color,"due_date":p.due_date,"owner":user_out(owner) if owner else None,"team":team.name if team else None,"progress":round(done/total*100) if total else 0,"task_count":total,"documents":[{"id":d.id,"file_name":d.file_name,"created_at":d.created_at} for d in documents],"north_star":north_star,"milestones":[milestone_out(m,db) for m in milestones]}
def task_out(t: Task, db: Session):
    assignee = db.get(User,t.assignee_id) if t.assignee_id else None
    dependencies = list_dependencies(db, t.id)
    reviewer = db.get(User, t.peer_review_agent_id) if t.peer_review_agent_id else None
    milestone = db.get(ProjectMilestone, t.milestone_id) if t.milestone_id else None
    return {"id":t.id,"project_id":t.project_id,"parent_id":t.parent_id,"milestone_id":t.milestone_id,"milestone_title":milestone.title if milestone else None,"title":t.title,"description":t.description,"status":t.status,"blocked_reason":t.blocked_reason,"escalation_category":t.escalation_category,"priority":t.priority,"assignee":user_out(assignee) if assignee else None,"depends_on":[{"id":dependency.id,"title":dependency.title,"status":dependency.status} for dependency in dependencies],"due_date":t.due_date,"labels":t.labels,"qa_required":t.qa_required,"requires_human_approval":t.requires_human_approval,"acceptance_criteria":t.acceptance_criteria or [],"review_rubric":t.review_rubric or [],"qa_evidence":t.qa_evidence or [],"qa_checks":t.qa_checks or [],"qa_checked_at":t.qa_checked_at,"peer_review_status":t.peer_review_status,"peer_review_agent":user_out(reviewer) if reviewer else None,"peer_review_task_id":t.peer_review_task_id,"peer_review_summary":t.peer_review_summary,"peer_review_decision":t.peer_review_decision,"completed_at":t.completed_at,"created_at":t.created_at,"updated_at":t.updated_at}
def notify(db, recipient_id: str | None, actor: User, title: str, body: str | None = None, href: str | None = None) -> None:
    """Tell a person about something someone else did to their work. Never the actor
    themselves, and never an agent (agents have no inbox to read)."""
    if not recipient_id or recipient_id == actor.id:
        return
    recipient = db.get(User, recipient_id)
    if recipient is None or recipient.kind != "human" or not recipient.active or recipient.organization_id != actor.organization_id:
        return
    db.add(Notification(user_id=recipient_id, title=title[:220], body=body, href=href))


def require_manager(user: User) -> None:
    """Destructive project-level changes (delete/archive a project, delete someone
    else's task, create teams) belong to people who run projects: human admins and
    managers. This is what distinguishes the manager role from a member."""
    if user.kind != "human" or user.role not in ("admin", "manager"):
        raise HTTPException(403, "Only managers and administrators can do this")


def ensure_project(db, user, project_id):
    project = db.get(Project, project_id)
    if not project or project.organization_id != user.organization_id: raise HTTPException(404,"Project not found")
    return project


def ensure_knowledge_project(db: Session, user: User, project_id: str) -> Project:
    project = db.scalar(select(Project).where(
        Project.id == project_id, Project.id.in_(visible_project_ids(db, user)),
    ))
    if not project:
        if user.kind == "agent" and db.scalar(select(Project.id).where(Project.id == project_id, Project.organization_id == user.organization_id)):
            from .agent_access import record_access_denial
            record_access_denial(db, user, tool="direct_api_request", resource_type="project", reason="out_of_scope")
        raise HTTPException(404, "Project not found")
    return project

def ensure_org_team(db, user, team_id):
    if team_id is None: return None
    team = db.get(Team, team_id)
    if not team or team.organization_id != user.organization_id:
        raise HTTPException(422, "Team not found")
    return team


def ensure_org_assignee(db, user, assignee_id):
    """Tasks carry no organization_id of their own -- tenancy is inherited from the
    project. That makes assignee_id a hole: without this check a caller can point a
    task at ANY user id in the system, and task_out will then render that foreign
    user's name, email and role straight back to them. Validate it explicitly."""
    if not assignee_id: return None
    assignee = db.get(User, assignee_id)
    if not assignee or assignee.organization_id != user.organization_id:
        raise HTTPException(422, "Assignee not found in this workspace")
    if assignee.kind == "human" and not assignee.active:
        raise HTTPException(409, "This person has been removed from the workspace")
    if assignee.kind == "agent":
        human_admin(user)
        if assignee.retired_at is not None:
            raise HTTPException(409, "This agent has been retired and cannot be assigned new work")
    return assignee


def ensure_agent_has_no_active_conflict(db: Session, agent_id: str, task_id: str) -> None:
    """An agent may queue work, but must never own two tasks in progress."""
    agent = db.scalar(select(User).where(
        User.id == agent_id, User.kind == "agent", User.retired_at.is_(None),
    ).with_for_update())
    if not agent:
        raise HTTPException(409, "This agent is no longer available for work")
    active_task = db.scalar(select(Task).where(
        Task.assignee_id == agent_id, Task.id != task_id, Task.status == "in_progress",
    ).join(Project, Project.id == Task.project_id).where(
        Project.organization_id == agent.organization_id,
    ).order_by(Task.updated_at.desc()).limit(1))
    if active_task:
        raise HTTPException(409, "This agent already has active work. Finish or pause that task before starting another.")


def accountability_person(db: Session, user_id: str | None, organization_id: str) -> dict[str, Any] | None:
    if not user_id:
        return None
    person = db.scalar(select(User).where(
        User.id == user_id, User.organization_id == organization_id,
    ))
    if not person:
        return None
    return {
        "id": person.id, "name": person.name, "kind": person.kind,
        "role": person.role, "title": person.title,
        "hierarchy_level": person.hierarchy_level,
    }

def ensure_task_parent(db, project, parent_id):
    """A subtask must live in the same project as its parent; otherwise the task tree
    can be stitched across projects -- and across organizations."""
    if not parent_id: return None
    parent = db.get(Task, parent_id)
    if not parent or parent.project_id != project.id:
        raise HTTPException(422, "Parent task not found in this project")
    return parent

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

@app.get("/live")
def live():
    """Process liveness: deliberately avoids external dependencies."""
    return {"ok": True}


def _readiness(db: Session) -> dict[str, str]:
    """A replica that answers here but cannot reach its database or shared rate
    limiter is not actually healthy -- a load balancer or orchestrator routing
    traffic to it on a bare 200 would be routing into a wall. Both checks are a
    single cheap round trip each, so this stays fast enough for a tight probe
    interval."""
    from sqlalchemy import text as _text
    checks: dict[str, str] = {}
    try:
        db.execute(_text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            from .ratelimit import get_redis
            get_redis(redis_url).ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"
    else:
        checks["redis"] = "not_configured"
    if checks["db"] == "ok":
        try:
            current, _actual, _expected = migration_revision_status(db.connection())
            checks["migrations"] = "ok" if current else "error"
        except Exception:
            checks["migrations"] = "error"
    else:
        checks["migrations"] = "not_checked"
    return checks


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    checks = _readiness(db)
    healthy = checks["db"] == "ok" and checks["redis"] != "error" and checks["migrations"] == "ok"
    if not healthy:
        raise HTTPException(status_code=503, detail={"ok": False, **checks})
    return {"ok": True, **checks}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Compatibility alias for existing operational checks; use /ready for routing."""
    return ready(db)

@app.get("/")
def root(): return {"service": "Enter AI API", "status": "ok"}

# A real hash of a value nobody holds, verified against when the submitted address
# has no account so that both paths do the same amount of password hashing.
_ABSENT_USER_HASH = hash_password(os.urandom(24).hex())

@app.post("/api/auth/register")
def register(data: Register, request: Request, response: Response, db: Session = Depends(get_db)):
    """Self-serve signup: creates the organization and its first admin.

    A taken email address is answered with the same message as any other rejected
    signup, and the notice that an account already exists goes to the address
    itself rather than to whoever submitted the form. Organization names need not be
    unique -- refusing a taken name would reveal who is already a customer -- so the
    slug gets a random suffix when the plain one is in use.
    """
    enforce(_SIGNUP_LIMITER, request)
    address = accounts.normalize(data.email)
    accounts.require_strong_password(data.password, address)
    slug = re.sub(r"[^a-z0-9]+", "-", data.organization_name.lower()).strip("-")[:70] or "workspace"
    existing = db.scalar(select(User).where(func.lower(User.email) == address))
    if existing:
        organization = db.get(Organization, existing.organization_id)
        mailer.send_existing_account_notice(address, organization.name if organization else "Enter AI")
        raise HTTPException(409, "We couldn't create an account with those details.")
    while db.scalar(select(Organization.id).where(Organization.slug == slug)):
        slug = f"{slug[:70]}-{secrets.token_hex(3)}"
    org = Organization(name=data.organization_name, slug=slug); db.add(org); db.flush()
    user = User(organization_id=org.id,name=data.name,email=address,password_hash=hash_password(data.password),role="admin",avatar="".join(x[0] for x in data.name.split())[:2].upper()); db.add(user)
    accounts.record_terms_acceptance(user, data.accept_terms)
    ensure_hierarchy_config(db, org.id)
    from .billing_service import ensure_trial_subscription
    ensure_trial_subscription(db, org.id)
    db.flush(); log(db, org.id, user.id, "user", user.id, "registered")
    verification = accounts.issue_verification(db, user)
    db.commit()
    mailer.send_verification(user.email, user.name, verification)
    token = create_token(user); set_session_cookie(response, token)
    return {"token":token,"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.post("/api/auth/login")
def login(data: SignIn, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce(_LOGIN_LIMITER, request)
    user = db.scalar(select(User).where(func.lower(User.email) == accounts.normalize(data.email)))
    if not user:
        # Hash anyway. Returning early for an unknown address answers in a fraction
        # of the time a real one takes, which turns the login form into an account
        # enumeration oracle no matter how generic the message is.
        verify_password(data.password, _ABSENT_USER_HASH)
        accounts.enforce_account_login_budget(accounts.normalize(data.email))
        raise HTTPException(401,"Incorrect email or password")
    if not verify_password(data.password,user.password_hash):
        accounts.enforce_account_login_budget(accounts.normalize(data.email))
        raise HTTPException(401,"Incorrect email or password")
    if not user.active: raise HTTPException(401,"Incorrect email or password")
    from .enterprise_models import SessionPolicy
    policy = db.scalar(select(SessionPolicy).where(SessionPolicy.organization_id == user.organization_id))
    if policy and policy.sso_only_enforced:
        raise HTTPException(403, "This organization requires signing in through SSO.")
    log(db,user.organization_id,user.id,"user",user.id,"logged_in"); db.commit()
    org=db.get(Organization,user.organization_id); token=create_token(user); set_session_cookie(response, token)
    return {"token":token,"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Ends the calling session -- the browser's cookie, or the bearer token the CLI
    sends -- and revokes that token server-side, so a copy of it stops working too.
    Other sessions are untouched; ending all of them is what a password reset is for."""
    header = request.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else request.cookies.get(SESSION_COOKIE)
    revoke_session_token(db, raw)
    clear_session_cookie(response)

@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)): return {"user":user_out(user),"organization":{"id":user.organization_id,"name":db.get(Organization,user.organization_id).name}}

@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    projects=db.scalars(select(Project).where(Project.id.in_(visible_project_ids(db,user))).order_by(Project.created_at.desc())).all()
    assigned=db.scalars(select(Task).where(task_is_visible(db,user), Task.assignee_id==user.id)).all()
    priority_order = {"high": 0, "medium": 1, "low": 2}
    tasks=sorted(assigned,key=lambda task: (task.status=="done", priority_order.get(task.priority, 1), task.due_date or datetime.max, task.position, task.created_at))
    new_requests = 0 if not is_workspace_manager(user) else (db.scalar(select(func.count(InboundMessage.id)).where(
        InboundMessage.organization_id == user.organization_id, InboundMessage.status == "new",
    )) or 0)
    plans_awaiting_approval = 0 if not is_workspace_manager(user) else (db.scalar(select(func.count(InboundPlan.id)).where(
        InboundPlan.organization_id == user.organization_id, InboundPlan.status == "pending",
    )) or 0)
    blocked_tasks = db.scalar(select(func.count(Task.id)).where(
        task_is_visible(db,user), Task.status == "blocked",
    )) or 0
    projects_awaiting_review = 0 if not is_workspace_manager(user) else (db.scalar(select(func.count(Project.id)).where(
        Project.id.in_(visible_project_ids(db,user)), Project.status == "review",
    )) or 0)
    now = datetime.now(timezone.utc); week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    return {"projects":[project_out(p,db) for p in projects],"my_tasks":[task_out(t,db) for t in tasks],"stats":{"active_projects":len([p for p in projects if p.status=="active"]),"at_risk":len([p for p in projects if p.health=="at_risk"]),"my_open_tasks":len([task for task in assigned if task.status!="done"]),"completed_this_week":len([task for task in assigned if task.completed_at and task.completed_at >= week_start]),"new_requests":new_requests,"plans_awaiting_approval":plans_awaiting_approval,"blocked_tasks":blocked_tasks,"projects_awaiting_review":projects_awaiting_review}}

@app.get("/api/projects")
def projects(user: User = Depends(current_user), db: Session = Depends(get_db)): return [project_out(p,db) for p in db.scalars(select(Project).where(Project.id.in_(visible_project_ids(db,user)))).all()]
@app.post("/api/projects", status_code=201)
def create_project(data: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_org_team(db, user, data.team_id)
    name, code = data.name.strip(), data.code.strip().upper()
    if not name or not code: raise HTTPException(422, "Project name and code are required")
    duplicate = db.scalar(select(Project).where(Project.organization_id==user.organization_id, or_(Project.name.ilike(name), Project.code.ilike(code))))
    if duplicate: raise HTTPException(409, "A project with this name or code already exists")
    values=data.model_dump(exclude={"source_document_ids"})
    values.update(_north_star_values(values), name=name, code=code)
    p=Project(**values,organization_id=user.organization_id,owner_id=user.id); db.add(p); db.flush()
    if data.source_document_ids:
        docs=db.scalars(select(ProjectDocument).where(
            ProjectDocument.id.in_(data.source_document_ids),
            ProjectDocument.project_id.is_(None),
            document_is_visible(db, user),
        )).all()
        for document in docs: document.project_id=p.id
    log(db,user.organization_id,user.id,"project",p.id,"created",name=p.name); db.commit(); return project_out(p,db)
@app.get("/api/projects/{project_id}")
def project(project_id: str,user: User=Depends(current_user),db:Session=Depends(get_db)): return project_out(ensure_knowledge_project(db,user,project_id),db)
@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, data: ProjectUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = ensure_knowledge_project(db, user, project_id)
    values = data.model_dump(exclude_unset=True)
    metric_fields = {"north_star_goal", "success_metric", "metric_unit", "baseline_value", "current_value", "target_value"}
    if metric_fields.intersection(values):
        _require_project_lead(p, user)
        before_metric = p.current_value
        normalized = _north_star_values(values, p)
        values.update({key: normalized.get(key) for key in metric_fields})
        if p.current_value != before_metric and "current_value" in values:
            values["current_value"] = normalized["current_value"]
    if values.get("status") == "completed":
        raise HTTPException(409, "Approve completion after reviewing all finished tasks")
    if p.status == "completed" and values.get("status") not in {None, "archived"}:
        raise HTTPException(409, "Reopen the completed project before changing its status")
    if "name" in values: values["name"] = values["name"].strip()
    if "code" in values: values["code"] = values["code"].strip().upper()
    if values.get("name") == "" or values.get("code") == "": raise HTTPException(422, "Project name and code cannot be empty")
    if "team_id" in values: ensure_org_team(db, user, values["team_id"])
    if "status" in values and "archived" in (values["status"], p.status) and values["status"] != p.status:
        require_manager(user)  # archiving hides the project from everyone's portfolio
    duplicate = db.scalar(select(Project).where(Project.organization_id==user.organization_id, Project.id != p.id, or_(Project.name.ilike(values.get("name", p.name)), Project.code.ilike(values.get("code", p.code)))))
    if duplicate: raise HTTPException(409, "A project with this name or code already exists")
    for key, value in values.items(): setattr(p, key, value)
    log(db, user.organization_id, user.id, "project", p.id, "updated", fields=list(values))
    if "current_value" in values:
        log(db, user.organization_id, user.id, "project", p.id, "north_star_metric_updated",
            current_value=values.get("current_value"), target_value=values.get("target_value", p.target_value),
            metric=values.get("success_metric", p.success_metric))
    db.commit(); return project_out(p, db)

@app.post("/api/projects/{project_id}/approve-completion")
def approve_project_completion(project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must approve project completion")
    p = ensure_knowledge_project(db, user, project_id)
    if user.role not in {"admin", "manager"} and p.owner_id != user.id:
        raise HTTPException(403, "Only the project owner or a manager can approve completion")
    if p.status != "review":
        raise HTTPException(409, "This project is not awaiting completion review")
    tasks = db.scalars(select(Task).where(Task.project_id == p.id)).all()
    if not tasks or any(task.status != "done" for task in tasks):
        raise HTTPException(409, "Every project task must be complete before approval")
    p.status = "completed"
    log(db, user.organization_id, user.id, "project", p.id, "completion_approved", task_count=len(tasks))
    for task in tasks:
        log(db, user.organization_id, user.id, "task", task.id, "task_review_approved",
            reviewed_by_id=user.id, approved_by_id=user.id, project_id=p.id)
    db.commit()
    return project_out(p, db)

@app.post("/api/projects/{project_id}/reopen")
def reopen_project(project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_manager(user)
    p = ensure_project(db, user, project_id)
    if p.status not in {"review", "completed"}:
        raise HTTPException(409, "Only a project awaiting review or marked complete can be reopened")
    p.status = "active"
    log(db, user.organization_id, user.id, "project", p.id, "reopened")
    db.commit()
    return project_out(p, db)
@app.get("/api/projects/{project_id}/tasks")
def project_tasks(project_id: str,user: User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_knowledge_project(db,user,project_id); return [task_out(t,db) for t in db.scalars(select(Task).where(Task.project_id==project_id,task_is_visible(db,user)).order_by(Task.position)).all()]


@app.post("/api/projects/{project_id}/milestones", status_code=201)
def create_milestone(project_id: str, data: MilestoneIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = ensure_knowledge_project(db, user, project_id)
    _require_project_lead(project, user)
    if project.status == "completed":
        raise HTTPException(409, "Reopen the completed project before adding a milestone")
    title = " ".join(data.title.split())
    milestone = ProjectMilestone(organization_id=user.organization_id, project_id=project.id,
        title=title, description=data.description, due_date=data.due_date, created_by=user.id)
    db.add(milestone); db.flush()
    log(db, user.organization_id, user.id, "project_milestone", milestone.id, "created", project_id=project.id)
    db.commit(); db.refresh(milestone)
    return milestone_out(milestone, db)


@app.patch("/api/projects/{project_id}/milestones/{milestone_id}")
def update_milestone(project_id: str, milestone_id: str, data: MilestoneUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = ensure_knowledge_project(db, user, project_id)
    _require_project_lead(project, user)
    milestone = db.scalar(select(ProjectMilestone).where(
        ProjectMilestone.id == milestone_id, ProjectMilestone.project_id == project.id,
        ProjectMilestone.organization_id == user.organization_id,
    ))
    if not milestone:
        raise HTTPException(404, "Milestone not found")
    values = data.model_dump(exclude_unset=True)
    if "title" in values:
        values["title"] = " ".join(values["title"].split())
    for key, value in values.items():
        setattr(milestone, key, value)
    log(db, user.organization_id, user.id, "project_milestone", milestone.id, "updated", fields=list(values))
    db.commit(); db.refresh(milestone)
    return milestone_out(milestone, db)


@app.get("/api/projects/{project_id}/next-steps")
def list_project_next_steps(project_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_knowledge_project(db, user, project_id)
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "A human must review agent next-step suggestions")
    rows = db.scalars(select(AgentNextStep).where(
        AgentNextStep.organization_id == user.organization_id, AgentNextStep.project_id == project_id,
        AgentNextStep.status == "pending",
    ).order_by(AgentNextStep.created_at.desc())).all()
    return [next_step_out(row, db) for row in rows]


@app.post("/api/projects/{project_id}/next-steps/{suggestion_id}/approve", status_code=201)
def approve_project_next_step(project_id: str, suggestion_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = ensure_knowledge_project(db, user, project_id)
    _require_project_lead(project, user)
    if project.status == "completed":
        raise HTTPException(409, "Reopen the completed project before creating work from a suggestion")
    suggestion = db.scalar(select(AgentNextStep).where(
        AgentNextStep.id == suggestion_id, AgentNextStep.project_id == project.id,
        AgentNextStep.organization_id == user.organization_id,
    ).with_for_update())
    if not suggestion or suggestion.status != "pending":
        raise HTTPException(409, "This next-step suggestion is no longer pending")
    milestone = None
    if suggestion.milestone_id:
        milestone = db.scalar(select(ProjectMilestone).where(
            ProjectMilestone.id == suggestion.milestone_id, ProjectMilestone.project_id == project.id,
        ))
        if not milestone:
            raise HTTPException(409, "The suggested milestone no longer exists")
    high = suggestion.priority == "high"
    task = Task(
        project_id=project.id, milestone_id=milestone.id if milestone else None,
        title=suggestion.title, description=suggestion.description, priority=suggestion.priority,
        status="todo", assignee_id=user.id, reporter_id=user.id,
        qa_required=high, requires_human_approval=high,
        acceptance_criteria=["Deliver the proposed next step and show evidence of the outcome"] if high else [],
        review_rubric=["Evidence supports the result", "No unresolved blocker remains"] if high else [],
    )
    db.add(task); db.flush()
    if project.status == "review":
        project.status = "active"
        log(db, user.organization_id, user.id, "project", project.id,
            "completion_review_reopened", reason="agent_next_step_approved")
    suggestion.status = "approved"
    suggestion.created_task_id = task.id
    suggestion.reviewed_by = user.id
    suggestion.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    log(db, user.organization_id, user.id, "agent_next_step", suggestion.id, "approved",
        task_id=task.id, project_id=project.id, milestone_id=milestone.id if milestone else None)
    log(db, user.organization_id, user.id, "task", task.id, "created_from_agent_next_step",
        suggestion_id=suggestion.id, source_task_id=suggestion.source_task_id)
    db.commit(); db.refresh(task)
    return task_out(task, db)


@app.post("/api/projects/{project_id}/next-steps/{suggestion_id}/reject")
def reject_project_next_step(project_id: str, suggestion_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = ensure_knowledge_project(db, user, project_id)
    _require_project_lead(project, user)
    suggestion = db.scalar(select(AgentNextStep).where(
        AgentNextStep.id == suggestion_id, AgentNextStep.project_id == project.id,
        AgentNextStep.organization_id == user.organization_id,
    ).with_for_update())
    if not suggestion or suggestion.status != "pending":
        raise HTTPException(409, "This next-step suggestion is no longer pending")
    suggestion.status = "rejected"
    suggestion.reviewed_by = user.id
    suggestion.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    log(db, user.organization_id, user.id, "agent_next_step", suggestion.id, "rejected",
        project_id=project.id, source_task_id=suggestion.source_task_id)
    db.commit()
    return {"ok": True}

@app.post("/api/project-drafts/assist", status_code=201)
async def assist_project_draft(file: UploadFile=File(...), user: User=Depends(current_user), db: Session=Depends(get_db)):
    raw=await file.read()
    if len(raw) > MAX_UPLOAD_BYTES: raise HTTPException(413,"Documents must be 5 MB or smaller")
    try: extracted=extract_document_text(file.filename or "document.txt",raw)
    except ValueError as error: raise HTTPException(415,str(error)) from error
    if not extracted.strip(): raise HTTPException(422,"No readable text was found in this document")
    stored = get_storage().save("project-documents", file.filename or "document", raw)
    document=ProjectDocument(organization_id=user.organization_id,uploaded_by=user.id,file_name=file.filename or "document",path=str(stored),content_type=file.content_type,extracted_text=extracted[:50000])
    try:
        draft = provider_call(WorkspaceTools(db, user), "deterministic", lambda: ProjectDraftProvider().project_draft(document.file_name, document.extracted_text))
    except CopilotProviderError:
        raise HTTPException(503, "The configured AI provider is unavailable. Try again later.") from None
    db.add(document); db.flush()
    log(db,user.organization_id,user.id,"project_document",document.id,"draft_created")
    db.commit()
    # Index the document for RAG
    try:
        index_document(db, document)
    except Exception as e:
        # Log but don't fail the request - indexing is async-friendly
        import logging
        logging.getLogger(__name__).warning("Failed to index document %s: %s", document.id, e)
    return {"document":{"id":document.id,"file_name":document.file_name},"draft":draft}

@app.get("/api/projects/{project_id}/documents")
def project_documents(project_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_knowledge_project(db,user,project_id)
    return [{"id":d.id,"file_name":d.file_name,"content_type":d.content_type,"created_at":d.created_at} for d in db.scalars(select(ProjectDocument).where(
        ProjectDocument.project_id == project_id, document_is_visible(db, user),
    )).all()]

@app.post("/api/projects/{project_id}/documents",status_code=201)
async def add_project_document(project_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_knowledge_project(db,user,project_id)
    raw=await file.read()
    if len(raw) > MAX_UPLOAD_BYTES: raise HTTPException(413,"Documents must be 5 MB or smaller")
    try: extracted=extract_document_text(file.filename or "document.txt",raw)
    except ValueError as error: raise HTTPException(415,str(error)) from error
    stored = get_storage().save("project-documents", file.filename or "document", raw)
    document=ProjectDocument(organization_id=user.organization_id,project_id=project_id,uploaded_by=user.id,file_name=file.filename or "document",path=str(stored),content_type=file.content_type,extracted_text=extracted[:50000]); db.add(document); log(db,user.organization_id,user.id,"project",project_id,"document_uploaded",file_name=document.file_name); db.commit()
    # Index the document for RAG
    try:
        index_document(db, document)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to index document %s: %s", document.id, e)
    return {"id":document.id,"file_name":document.file_name}

@app.post("/api/tasks",status_code=201)
def create_task(data: TaskIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    p=ensure_knowledge_project(db,user,data.project_id)
    if p.status == "completed": raise HTTPException(409, "Reopen the completed project before adding work")
    if p.status == "review":
        p.status = "active"
        log(db, user.organization_id, user.id, "project", p.id, "completion_review_reopened", reason="task_added")
    assignee_id = data.assignee_id or user.id
    high_impact = data.priority.lower() == "high"
    if high_impact and (not data.qa_required or not data.requires_human_approval):
        data = data.model_copy(update={"qa_required": True, "requires_human_approval": True})
    if data.qa_required or data.requires_human_approval:
        if not data.acceptance_criteria or not data.review_rubric:
            raise HTTPException(422, "Add acceptance criteria and a review rubric before enabling the QA gate")
        if data.status == "done":
            raise HTTPException(409, "Create gated tasks in review only after delivering evidence and passing QA")
    for value in [*data.acceptance_criteria, *data.review_rubric]:
        if not value.strip() or len(value) > 500:
            raise HTTPException(422, "QA criteria and rubric items must contain 1–500 characters")
    assignee = ensure_org_assignee(db,user,assignee_id)
    if data.status == "in_progress" and assignee.kind == "agent":
        ensure_agent_has_no_active_conflict(db, assignee.id, "__new_task__")
    ensure_task_parent(db,p,data.parent_id)
    if data.milestone_id and not db.scalar(select(ProjectMilestone.id).where(
        ProjectMilestone.id == data.milestone_id, ProjectMilestone.project_id == p.id,
        ProjectMilestone.organization_id == user.organization_id,
    )):
        raise HTTPException(422, "Choose a milestone from this project")
    values=data.model_dump(); dependency_ids=values.pop("depends_on")
    values["qa_required"] = data.qa_required or data.requires_human_approval
    values["assignee_id"] = assignee_id
    t=Task(**values,reporter_id=user.id); db.add(t); db.flush()
    try: replace_dependencies(db,t,dependency_ids)
    except ValueError as error: raise HTTPException(422,str(error)) from error
    if t.status == "done" and unmet_dependency_ids(db,t.id): raise HTTPException(409,"Complete prerequisite tasks before marking this task done")
    log(db,user.organization_id,user.id,"task",t.id,"created",title=t.title,
        assignee_id=assignee_id, requested_by_id=user.id, assignment_reason="task_created")
    notify(db, t.assignee_id, user, f"{user.name} assigned you “{t.title}”", p.name, f"project:{p.id}")
    db.commit(); return task_out(t,db)
@app.get("/api/tasks/{task_id}")
def get_task(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    """A single task, for links that open it directly (/projects/<id>?task=<id>)."""
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    ensure_knowledge_project(db,user,t.project_id)
    return task_out(t,db)


@app.get("/api/tasks/{task_id}/accountability")
def task_accountability(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db, user)))
    if not task:
        raise HTTPException(404, "Task not found")
    ensure_knowledge_project(db, user, task.project_id)
    rows = list(db.scalars(select(AuditEvent).where(
        AuditEvent.organization_id == user.organization_id,
        AuditEvent.entity_type == "task", AuditEvent.entity_id == task.id,
    ).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(100)).all())
    rows.reverse()
    person_ids = {value for value in (task.reporter_id, task.assignee_id) if value}
    for row in rows:
        person_ids.update(value for value in (row.actor_id, row.initiator_id) if value)
        person_ids.update(value for key, value in (row.detail or {}).items()
                          if key in {"assignee_id", "from_assignee_id", "to_assignee_id", "requested_by_id",
                                     "reviewed_by_id", "approved_by_id", "proposed_by_agent_id", "agent_id"}
                          and isinstance(value, str))
    people = {person.id: accountability_person(db, person.id, user.organization_id)
              for person in db.scalars(select(User).where(
                  User.id.in_(person_ids), User.organization_id == user.organization_id,
              )).all()} if person_ids else {}

    def person(user_id: str | None):
        return people.get(user_id) if user_id else None

    events = [{
        "id": row.id, "action": row.action, "source": row.source,
        "actor": person(row.actor_id), "initiator": person(row.initiator_id),
        "created_at": row.created_at.isoformat(),
        "detail": {key: value for key, value in (row.detail or {}).items()
                   if key in {"from_status", "to_status", "assignment_changed", "assignee_id", "outcome", "passed",
                              "from_assignee_id", "to_assignee_id", "assignment_reason", "tool",
                              "agent_id", "reviewed_by_id", "approved_by_id"}},
    } for row in rows]
    assignment_event = next((event for event in reversed(events)
                             if event["action"] in {"created", "created_from_inbound_plan", "subtask_delegated", "delegated_to_agent", "task_reassigned"}
                             or event["detail"].get("assignment_changed")), None)
    execution_event = next((event for event in reversed(events)
                            if event["action"] in {"agent_claimed", "agent_started", "agent_progress", "agent_completed", "agent_submitted_for_review", "agent_blocked", "agent_action_proposed", "task_started", "task_completed"}), None)
    review_event = next((event for event in reversed(events)
                         if event["action"] in {"agent_action_approved", "agent_action_rejected", "task_reviewed", "task_review_approved", "task_peer_review_decided"}), None)
    approval_event = next((event for event in reversed(events)
                           if event["action"] in {"agent_action_approved", "task_review_approved"}
                           or (event["action"] == "task_peer_review_decided" and event["detail"].get("outcome") == "approved")), None)
    return {
        "requested_by": person(task.reporter_id), "owner": person(task.assignee_id),
        "assigned_by": assignment_event["actor"] if assignment_event else None,
        "executed_by": execution_event["actor"] if execution_event else None,
        "reviewed_by": review_event["actor"] if review_event else None,
        "approved_by": approval_event["actor"] if approval_event else None,
        "events": events,
    }
@app.patch("/api/tasks/{task_id}")
def update_task(task_id:str,data:TaskUpdate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    p=ensure_knowledge_project(db,user,t.project_id)
    if p.status == "completed" and data.model_fields_set:
        raise HTTPException(409, "Reopen the completed project before changing its tasks")
    changes = data.model_dump(exclude_unset=True)
    qa_admin_fields = {"qa_required", "requires_human_approval", "acceptance_criteria", "review_rubric"}
    if qa_admin_fields.intersection(changes) and (user.kind != "human" or user.role not in {"admin", "manager"}):
        raise HTTPException(403, "Only a human manager or administrator can change the task QA plan")
    if "qa_evidence" in changes and user.kind != "human":
        raise HTTPException(403, "A human must record delivery evidence")
    for key in ("acceptance_criteria", "review_rubric", "qa_evidence"):
        values = changes.get(key)
        if values is not None and any(not value.strip() or len(value) > 2000 for value in values):
            raise HTTPException(422, "QA items must contain 1–2,000 characters")
    if changes.get("priority", t.priority).lower() == "high":
        changes["qa_required"] = True
        changes["requires_human_approval"] = True
    if changes.get("requires_human_approval"):
        changes["qa_required"] = True
    if "milestone_id" in changes:
        if user.kind != "human" or (user.role not in {"admin", "manager"} and p.owner_id != user.id):
            raise HTTPException(403, "Only the project owner, a manager, or an administrator can link tasks to milestones")
        if changes["milestone_id"] and not db.scalar(select(ProjectMilestone.id).where(
            ProjectMilestone.id == changes["milestone_id"], ProjectMilestone.project_id == p.id,
            ProjectMilestone.organization_id == user.organization_id,
        )):
            raise HTTPException(422, "Choose a milestone from this project")
    next_qa_required = changes.get("qa_required", t.qa_required)
    next_requires_human_approval = changes.get("requires_human_approval", t.requires_human_approval)
    next_criteria = changes.get("acceptance_criteria", t.acceptance_criteria or [])
    next_rubric = changes.get("review_rubric", t.review_rubric or [])
    if (next_qa_required or next_requires_human_approval) and (not next_criteria or not next_rubric):
        raise HTTPException(422, "Add acceptance criteria and a review rubric before enabling the QA gate")
    if "assignee_id" in data.model_fields_set:
        if data.assignee_id is None:
            previous_assignee = db.get(User, t.assignee_id) if t.assignee_id else None
            if previous_assignee and previous_assignee.kind == "agent":
                human_admin(user)
            raise HTTPException(422, "Every task needs an owner. Assign it to a human or active agent.")
        ensure_org_assignee(db, user, data.assignee_id)
        if t.assignee_id and t.assignee_id != data.assignee_id:
            previous_assignee = db.get(User, t.assignee_id)
            if previous_assignee and previous_assignee.kind == "agent":
                human_admin(user)
                if t.status == "in_progress":
                    raise HTTPException(409, "Pause this agent's task before reassigning it. Its current work must be reviewed by a human first.")
    before, before_assignee_id, before_priority, before_milestone_id = t.status, t.assignee_id, t.priority, t.milestone_id
    next_assignee_id = changes.get("assignee_id", t.assignee_id)
    next_status = changes.get("status", t.status)
    next_assignee = db.get(User, next_assignee_id) if next_assignee_id else None
    if next_status == "in_progress" and next_assignee and next_assignee.kind == "agent":
        ensure_agent_has_no_active_conflict(db, next_assignee.id, t.id)
    if data.status == "done" and before == "blocked":
        raise HTTPException(409, "Resolve the blocker and reopen this task before completing it")
    if data.status == "done" and user.kind == "agent":
        raise HTTPException(403, "Agents must submit completed work through the review workflow")
    dependency_ids = changes.pop("depends_on", None)
    dependency_detail = {}
    qa_changed = bool(set(changes) & {
        "title", "description", "labels", "qa_required", "requires_human_approval",
        "acceptance_criteria", "review_rubric", "qa_evidence",
    }) or dependency_ids is not None
    for key,value in changes.items(): setattr(t,key,value)
    if t.requires_human_approval:
        t.qa_required = True
    if qa_changed:
        t.qa_checks = []
        t.qa_checked_at = None
    if (set(changes) & {"title", "description", "labels", "acceptance_criteria", "review_rubric", "qa_evidence"}
            or dependency_ids is not None):
        t.peer_review_status = "not_requested"
        t.peer_review_agent_id = None
        t.peer_review_task_id = None
        t.peer_review_summary = None
        t.peer_review_decision = None
    if data.status == "blocked" and before != "blocked":
        t.blocked_reason = f"Marked blocked by {user.name}."
        t.escalation_category = "other"
        db.add(Comment(task_id=t.id, author_id=user.id, body=f"Blocked: {t.blocked_reason}"))
        recipients = db.scalars(select(User).where(
            User.organization_id == user.organization_id, User.kind == "human",
            User.role == "admin", User.active.is_(True),
        )).all()
        for recipient in recipients:
            notify(db, recipient.id, user, f"Task blocked: {t.title}", t.blocked_reason, f"project:{p.id}")
    elif before == "blocked" and data.status is not None and data.status != "blocked":
        previous_blocker = t.blocked_reason
        t.blocked_reason = None
        t.escalation_category = None
        if t.assignee_id:
            previous_assignee = db.get(User, t.assignee_id)
            if previous_assignee and previous_assignee.kind == "agent":
                previous_assignee.consecutive_task_failures = 0
        db.add(Comment(task_id=t.id, author_id=user.id, body=f"Blocker cleared by {user.name}. {previous_blocker or ''}".strip()))
    elif data.status is not None and data.status != "blocked":
        t.blocked_reason = None
    if dependency_ids is not None:
        previous_dependency_ids = {dependency.id for dependency in list_dependencies(db, t.id)}
        next_dependency_ids = set(dependency_ids)
        try: replace_dependencies(db,t,dependency_ids)
        except ValueError as error: raise HTTPException(422,str(error)) from error
        if previous_dependency_ids != next_dependency_ids:
            dependency_detail = {
                "dependencies_changed": True,
                "dependencies_added": len(next_dependency_ids - previous_dependency_ids),
                "dependencies_removed": len(previous_dependency_ids - next_dependency_ids),
            }
    if data.status == "done" and (t.priority.lower() == "high" or t.qa_required or t.requires_human_approval):
        if user.kind != "human":
            raise HTTPException(403, "A human must approve gated task delivery")
        if not t.qa_checked_at or not t.qa_checks or any(not item.get("passed") for item in t.qa_checks):
            raise HTTPException(409, "Run and pass the QA checks before approving delivery")
        if t.peer_review_agent_id and t.peer_review_status != "approved":
            raise HTTPException(409, "The independent agent review must be approved before delivery")
    if data.status == "done" and unmet_dependency_ids(db,t.id): raise HTTPException(409,"Complete prerequisite tasks before marking this task done")
    if p.status == "completed" and data.status is not None and data.status != "done":
        raise HTTPException(409, "Reopen the completed project before reopening a task")
    if "status" in data.model_fields_set and data.status != before:
        t.completed_at = datetime.now(timezone.utc) if data.status == "done" else None
    sync_agent_task_pointers(db, t, before_assignee_id)
    if t.assignee_id != before_assignee_id and t.assignee_id:
        assigned_agent = db.get(User, t.assignee_id)
        if assigned_agent and assigned_agent.kind == "agent":
            assigned_agent.consecutive_task_failures = 0
    if before != t.status and t.status != "done" and p.status == "review":
        p.status = "active"
        log(db, user.organization_id, user.id, "project", p.id, "completion_review_reopened", reason="task_reopened", task_id=t.id)
    if before != t.status and t.status == "done":
        advance_project_to_completion_review(db, p, user.id)
    if t.assignee_id != before_assignee_id:
        notify(db, t.assignee_id, user, f"{user.name} assigned you “{t.title}”", p.name, f"project:{p.id}")
    owner_changed = t.assignee_id != before_assignee_id
    priority_changed = t.priority != before_priority
    log(db,user.organization_id,user.id,"task",t.id,"updated",from_status=before,to_status=t.status,
        assignment_changed=owner_changed,
        from_assignee_id=before_assignee_id if owner_changed else None,
        to_assignee_id=t.assignee_id if owner_changed else None,
        priority_changed=priority_changed, from_priority=before_priority if priority_changed else None,
        to_priority=t.priority if priority_changed else None,
        from_milestone_id=before_milestone_id if before_milestone_id != t.milestone_id else None,
        to_milestone_id=t.milestone_id if before_milestone_id != t.milestone_id else None,
        requested_by_id=t.reporter_id, **dependency_detail)
    if priority_changed:
        log(db, user.organization_id, user.id, "task", t.id, "task_priority_changed",
            from_priority=before_priority, to_priority=t.priority, project_id=p.id)
    if owner_changed:
        log(db, user.organization_id, user.id, "task", t.id, "task_reassigned",
            from_assignee_id=before_assignee_id, to_assignee_id=t.assignee_id,
            assignment_reason="human_reassignment")
    if before != t.status and t.status == "done":
        log(db, user.organization_id, user.id, "task", t.id, "task_completed",
            from_status=before, to_status="done")
    elif before != t.status and t.status == "in_progress":
        log(db, user.organization_id, user.id, "task", t.id, "task_started",
            from_status=before, to_status="in_progress")
    elif before != t.status and t.status == "review" and user.kind == "human":
        log(db, user.organization_id, user.id, "task", t.id, "task_reviewed",
            from_status=before, to_status="review", reviewed_by_id=user.id)
    db.commit(); return task_out(t,db)


@app.post("/api/tasks/{task_id}/qa/run")
def run_task_qa(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db, user)))
    if not task:
        raise HTTPException(404, "Task not found")
    ensure_knowledge_project(db, user, task.project_id)
    require_manager(user)
    checks = run_task_quality_checks(db, task)
    task.qa_checks = checks
    task.qa_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    passed = all(item["passed"] for item in checks)
    log(db, user.organization_id, user.id, "task", task.id, "task_qa_checked",
        passed=passed, checks=[{"name": item["name"], "passed": item["passed"]} for item in checks])
    db.commit()
    return task_out(task, db)


@app.post("/api/tasks/{task_id}/qa/peer-review")
def request_task_peer_review(task_id: str, data: QAPeerReviewRequest,
                             user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db, user)))
    if not task:
        raise HTTPException(404, "Task not found")
    project = ensure_knowledge_project(db, user, task.project_id)
    require_manager(user)
    if not (task.qa_required or task.requires_human_approval):
        raise HTTPException(409, "Enable the QA gate before requesting an independent review")
    if task.status != "review":
        raise HTTPException(409, "Submit the task for review before requesting a peer review")
    if not task.acceptance_criteria or not task.review_rubric or not task.qa_evidence:
        raise HTTPException(409, "Save acceptance criteria, a review rubric, and work evidence first")
    reviewer = db.scalar(select(User).where(
        User.id == data.reviewer_agent_id, User.organization_id == user.organization_id,
        User.kind == "agent", User.active.is_(True), User.retired_at.is_(None),
    ))
    if not reviewer:
        raise HTTPException(404, "Active reviewer agent not found")
    if reviewer.id == task.assignee_id:
        raise HTTPException(422, "The peer reviewer must be a different agent from the task owner")
    if task.peer_review_status == "pending" and task.peer_review_task_id:
        existing = db.get(Task, task.peer_review_task_id)
        if existing and existing.status not in {"done", "blocked"}:
            raise HTTPException(409, "A peer review is already in progress")
    review = Task(
        project_id=task.project_id, parent_id=task.id,
        title=f"Independent review: {task.title}"[:240],
        description=(
            "Review the completed work for the parent task against the acceptance criteria and rubric. "
            "Do not change the parent deliverable. Record concrete findings, cite evidence, and recommend "
            "approval or changes in your completion report. A human will make the final decision.\n\n"
            f"Parent task: {task.title}\n\nAcceptance criteria:\n- " + "\n- ".join(task.acceptance_criteria) +
            "\n\nReview rubric:\n- " + "\n- ".join(task.review_rubric) +
            "\n\nSubmitted evidence:\n- " + "\n- ".join(task.qa_evidence)
        )[:12000],
        status="todo", priority="high", assignee_id=reviewer.id, reporter_id=user.id,
        labels=["qa_peer_review"],
    )
    db.add(review)
    db.flush()
    task.peer_review_agent_id = reviewer.id
    task.peer_review_task_id = review.id
    task.peer_review_status = "pending"
    task.peer_review_summary = None
    task.peer_review_decision = None
    task.qa_checks = []
    task.qa_checked_at = None
    log(db, user.organization_id, user.id, "task", task.id, "task_peer_review_requested",
        reviewer_agent_id=reviewer.id, review_task_id=review.id)
    log(db, user.organization_id, user.id, "task", review.id, "task_peer_review_assigned",
        parent_task_id=task.id, reviewer_agent_id=reviewer.id)
    notify(db, reviewer.id, user, f"Independent review requested: {task.title}", project.name, f"project:{project.id}")
    db.commit()
    return {"task": task_out(task, db), "review_task": task_out(review, db)}


@app.post("/api/tasks/{task_id}/qa/peer-review/decision")
def decide_task_peer_review(task_id: str, data: QAPeerReviewDecision,
                            user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db, user)))
    if not task:
        raise HTTPException(404, "Task not found")
    ensure_knowledge_project(db, user, task.project_id)
    require_manager(user)
    review = db.get(Task, task.peer_review_task_id) if task.peer_review_task_id else None
    if task.peer_review_status != "submitted" or not review or review.status != "done":
        raise HTTPException(409, "The independent agent must submit its review before a decision")
    task.peer_review_status = data.outcome
    task.peer_review_decision = data.summary.strip()
    task.qa_checks = []
    task.qa_checked_at = None
    if data.outcome == "changes_requested":
        task.status = "in_progress"
        task.completed_at = None
        if task.assignee_id:
            owner = db.get(User, task.assignee_id)
            if owner and owner.kind == "agent":
                owner.current_task_id = task.id
        db.add(Comment(task_id=task.id, author_id=user.id,
                       body=f"Peer review requested changes: {data.summary.strip()}"))
    log(db, user.organization_id, user.id, "task", task.id, "task_peer_review_decided",
        outcome=data.outcome, reviewer_agent_id=task.peer_review_agent_id)
    notify(db, task.assignee_id, user, f"Peer review {data.outcome.replace('_', ' ')}: {task.title}",
           data.summary.strip(), f"project:{task.project_id}")
    db.commit()
    return task_out(task, db)


def detach_task_references(db, task):
    """PostgreSQL enforces the foreign keys SQLite ignores, so every row pointing at
    this task has to be resolved before the DELETE or the request fails outright.
    Comments and attachments are owned by the task and go with it; subtasks are
    promoted to top level rather than silently deleting work nobody asked to delete;
    agent pointers are cleared so the agent outlives its task."""
    db.execute(delete(Comment).where(Comment.task_id == task.id))
    db.execute(delete(Attachment).where(Attachment.task_id == task.id))
    db.execute(delete(TaskDependency).where(or_(TaskDependency.task_id == task.id, TaskDependency.depends_on_task_id == task.id)))
    db.execute(update(Task).where(Task.parent_id == task.id).values(parent_id=None))
    db.execute(update(User).where(User.current_task_id == task.id).values(current_task_id=None))
    db.execute(update(User).where(User.last_completed_task_id == task.id).values(last_completed_task_id=None))

@app.delete("/api/tasks/{task_id}",status_code=204)
def delete_task(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    project = ensure_knowledge_project(db,user,t.project_id)
    if project.status == "completed": raise HTTPException(409, "Reopen the completed project before deleting its tasks")
    if t.reporter_id != user.id: require_manager(user)  # members may delete only what they created
    log(db,user.organization_id,user.id,"task",t.id,"deleted",title=t.title)
    for a in db.scalars(select(Attachment).where(Attachment.task_id == t.id)).all():
        _discard_file(a.path)
    detach_task_references(db,t); db.delete(t); db.commit()


def _discard_file(identifier: str) -> None:
    try:
        get_storage().delete(identifier)
    except Exception:
        pass  # a missing file must not block deleting the row that pointed at it


@app.delete("/api/projects/{project_id}",status_code=204)
def delete_project(project_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    """Permanently delete a project with its tasks, comments, files and indexed text.
    Archiving (PATCH status=archived) is the reversible alternative."""
    require_manager(user)
    p=ensure_project(db,user,project_id)
    tasks=db.scalars(select(Task).where(Task.project_id==p.id)).all()
    task_ids=[t.id for t in tasks]
    if task_ids:
        for a in db.scalars(select(Attachment).where(Attachment.task_id.in_(task_ids))).all():
            _discard_file(a.path)
        db.execute(update(Task).where(Task.id.in_(task_ids)).values(parent_id=None))
        db.execute(delete(Comment).where(Comment.task_id.in_(task_ids)))
        db.execute(delete(Attachment).where(Attachment.task_id.in_(task_ids)))
        db.execute(update(User).where(User.current_task_id.in_(task_ids)).values(current_task_id=None))
        db.execute(update(User).where(User.last_completed_task_id.in_(task_ids)).values(last_completed_task_id=None))
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
    from .models import DocumentChunk
    db.execute(delete(DocumentChunk).where(DocumentChunk.project_id==p.id))
    documents=db.scalars(select(ProjectDocument).where(ProjectDocument.project_id==p.id)).all()
    for d in documents:
        _discard_file(d.path)
    if documents:
        # Explicit, ordered statements: the models declare no relationship between
        # documents and projects, so the ORM would not know to delete these first.
        doc_ids=[d.id for d in documents]
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids)))
        db.execute(delete(ProjectDocument).where(ProjectDocument.id.in_(doc_ids)))
    log(db,user.organization_id,user.id,"project",p.id,"deleted",name=p.name)
    db.execute(delete(Project).where(Project.id==p.id)); db.commit()

@app.get("/api/tasks/{task_id}/comments")
def comments(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    ensure_knowledge_project(db,user,t.project_id)
    comments_list = db.scalars(select(Comment).where(Comment.task_id==task_id)).all()
    author_ids = [c.author_id for c in comments_list if c.author_id]
    authors = {a.id: a for a in db.scalars(select(User).where(User.id.in_(author_ids))).all()}
    return [{"id":c.id,"body":c.body,"created_at":c.created_at,"author":user_out(authors.get(c.author_id))} for c in comments_list]


def decision_out(decision: Decision, author: User | None = None) -> dict[str, Any]:
    return {
        "id": decision.id, "title": decision.title, "body": decision.body,
        "created_at": decision.created_at.isoformat(), "project_id": decision.project_id,
        "task_id": decision.task_id, "source_comment_id": decision.source_comment_id,
        "source_ref": f"/projects/{decision.project_id}" + (f"?task={decision.task_id}" if decision.task_id else ""),
        "author": user_out(author) if author else None,
    }


@app.get("/api/tasks/{task_id}/decisions")
def task_decisions(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task or not db.scalar(select(Task.id).where(Task.id == task_id, task_is_visible(db, user))):
        raise HTTPException(404, "Task not found")
    decisions = db.scalars(select(Decision).where(
        Decision.organization_id == user.organization_id, Decision.task_id == task_id,
    ).order_by(Decision.created_at.desc()).limit(100)).all()
    authors = {row.id: row for row in db.scalars(select(User).where(User.id.in_([d.created_by for d in decisions]))).all()} if decisions else {}
    return [decision_out(d, authors.get(d.created_by)) for d in decisions]


@app.post("/api/tasks/{task_id}/decisions/from-comment/{comment_id}", status_code=201)
def record_comment_decision(task_id: str, comment_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must record a decision")
    task = db.get(Task, task_id)
    if not task or not db.scalar(select(Task.id).where(Task.id == task_id, task_is_visible(db, user))):
        raise HTTPException(404, "Task not found")
    comment = db.scalar(select(Comment).where(Comment.id == comment_id, Comment.task_id == task_id))
    if not comment:
        raise HTTPException(404, "Discussion comment not found")
    existing = db.scalar(select(Decision).where(Decision.source_comment_id == comment.id))
    if existing:
        author = db.get(User, existing.created_by)
        return decision_out(existing, author)
    body = comment.body.strip()
    if not body:
        raise HTTPException(422, "A decision needs discussion text")
    title = next((line.strip(" #-\t") for line in body.splitlines() if line.strip()), "Decision")[:240]
    decision = Decision(
        organization_id=user.organization_id, project_id=task.project_id, task_id=task.id,
        source_comment_id=comment.id, title=title, body=body[:20000], created_by=user.id,
    )
    db.add(decision)
    db.flush()
    log(db, user.organization_id, user.id, "decision", decision.id, "recorded", task_id=task.id, source_comment_id=comment.id)
    db.commit()
    db.refresh(decision)
    return decision_out(decision, user)


@app.post("/api/tasks/{task_id}/comments",status_code=201)
def add_comment(task_id:str,data:CommentIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    p=ensure_knowledge_project(db,user,t.project_id); c=Comment(task_id=task_id,author_id=user.id,body=data.body); db.add(c); log(db,user.organization_id,user.id,"task",task_id,"commented")
    for recipient in {t.assignee_id, t.reporter_id} - {None}:
        notify(db, recipient, user, f"{user.name} commented on “{t.title}”", data.body[:280], f"project:{p.id}")
    db.commit(); return {"id":c.id,"body":c.body}
@app.post("/api/tasks/{task_id}/attachments",status_code=201)
async def add_attachment(task_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    ensure_knowledge_project(db,user,t.project_id)
    raw=await file.read()
    if len(raw) > MAX_UPLOAD_BYTES: raise HTTPException(413,"Attachments must be 5 MB or smaller")
    path=get_storage().save("task-attachments", file.filename or "attachment", raw); a=Attachment(task_id=task_id,uploaded_by=user.id,file_name=file.filename or "attachment",path=path,content_type=file.content_type); db.add(a); log(db,user.organization_id,user.id,"task",task_id,"attachment_uploaded"); db.commit(); return {"id":a.id,"file_name":a.file_name}

def _download(identifier: str, file_name: str) -> Response:
    """Uploaded files are served as downloads, never rendered: an uploaded .html or
    .svg shown inline would run as script on the API's origin."""
    try:
        content = get_storage().read(identifier)
    except (FileNotFoundError, OSError):
        raise HTTPException(404, "This file is no longer available") from None
    except Exception as error:
        if type(error).__name__ in {"NoSuchKey", "ClientError"}:
            raise HTTPException(404, "This file is no longer available") from None
        raise
    from urllib.parse import quote
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", file_name)[:150] or "download"
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(file_name[:150])}",
    })


@app.get("/api/tasks/{task_id}/attachments")
def list_attachments(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.scalar(select(Task).where(Task.id == task_id, task_is_visible(db,user)))
    if not t: raise HTTPException(404,"Task not found")
    ensure_knowledge_project(db,user,t.project_id)
    rows=db.scalars(select(Attachment).where(Attachment.task_id==task_id).order_by(Attachment.created_at)).all()
    return [{"id":a.id,"file_name":a.file_name,"content_type":a.content_type,"created_at":a.created_at} for a in rows]


@app.get("/api/attachments/{attachment_id}/download")
def download_attachment(attachment_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    a=db.get(Attachment,attachment_id)
    t=db.scalar(select(Task).where(Task.id == a.task_id, task_is_visible(db,user))) if a else None
    if not a or not t:
        if user.kind == "agent":
            from .agent_access import record_access_denial
            record_access_denial(db, user, tool="direct_api_request", resource_type="document", reason="out_of_scope")
        raise HTTPException(404,"Attachment not found")
    ensure_knowledge_project(db,user,t.project_id)
    return _download(a.path, a.file_name)


@app.get("/api/project-documents/{document_id}/download")
def download_project_document(document_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    d=db.get(ProjectDocument,document_id)
    if not d or not db.scalar(select(ProjectDocument.id).where(ProjectDocument.id == document_id, document_is_visible(db,user))):
        if user.kind == "agent":
            from .agent_access import record_access_denial
            record_access_denial(db, user, tool="direct_api_request", resource_type="document", reason="out_of_scope")
        raise HTTPException(404,"Document not found")
    return _download(d.path, d.file_name)


@app.get("/api/teams")
def teams(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":t.id,"name":t.name,"description":t.description,"projects":db.query(Project).filter(Project.team_id==t.id, Project.organization_id==user.organization_id).count()} for t in db.scalars(select(Team).where(Team.organization_id==user.organization_id)).all()]
@app.get("/api/teams/{team_id}/projects")
def team_projects(team_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    team=db.get(Team,team_id)
    if not team or team.organization_id!=user.organization_id: raise HTTPException(404,"Team not found")
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.team_id==team_id, Project.organization_id==user.organization_id)).all()]
@app.post("/api/teams",status_code=201)
def create_team(data:TeamIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_manager(user)
    t=Team(organization_id=user.organization_id,**data.model_dump()); db.add(t); db.flush(); log(db,user.organization_id,user.id,"team",t.id,"created"); db.commit(); return {"id":t.id,"name":t.name,"description":t.description}
@app.get("/api/users")
def users(user:User=Depends(current_user),db:Session=Depends(get_db)): return [user_out(u) for u in db.scalars(select(User).where(User.organization_id==user.organization_id,User.kind=="human",User.active.is_(True))).all()]

def agent_out(a: User, db: Session) -> dict:
    current = db.get(Task, a.current_task_id) if a.current_task_id else None
    last_completed = db.get(Task, a.last_completed_task_id) if a.last_completed_task_id else None
    blocked = a.current_task_id is not None and a.consecutive_task_failures >= execution.MAX_CONSECUTIVE_FAILURES
    rate_limited = a.daily_limit_reached_at is not None and a.daily_limit_reached_at >= utcnow_naive() - timedelta(hours=24)
    return {
        "id": a.id, "name": a.name, "title": a.title, "avatar": a.avatar,
        "hierarchy_level": a.hierarchy_level, "parent_agent_id": a.parent_agent_id,
        "execution_enabled": a.execution_enabled,
        "current_task": task_out(current, db) if current else None,
        "last_completed_task": task_out(last_completed, db) if last_completed else None,
        "is_working": a.current_task_id is not None and not blocked and not rate_limited and current is not None and current.status != "review",
        "is_awaiting_approval": current is not None and current.status == "review",
        "is_blocked": blocked,
        "is_rate_limited": rate_limited,
        "daily_limit_reached_at": a.daily_limit_reached_at if rate_limited else None,
        "last_execution_at": a.last_execution_at,
        "retired": a.retired_at is not None,
        "retired_at": a.retired_at,
    }

@app.get("/api/agents")
def agents(include_retired: bool = False, user:User=Depends(current_user),db:Session=Depends(get_db)):
    stmt = select(User).where(User.organization_id==user.organization_id,User.kind=="agent")
    if not include_retired: stmt = stmt.where(User.retired_at.is_(None))
    rows = db.scalars(stmt).all()
    return [agent_out(a, db) for a in rows]

@app.post("/api/ceo-chat")
def bootstrap_ceo_chat(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Ensure every workspace has its single CEO chat entry point, even with no reports configured."""
    ensure_hierarchy_config(db, user.organization_id)
    reconcile_agents(db, user.organization_id)
    ceo = db.scalar(select(User).where(
        User.organization_id == user.organization_id, User.kind == "agent",
        User.hierarchy_level == "ceo", User.retired_at.is_(None),
    ).order_by(User.created_at))
    if not ceo:
        raise HTTPException(503, "CEO chat is not available right now")
    return agent_out(ceo, db)

@app.get("/api/ceo-chat/unread")
def ceo_chat_unread(since: str | None = Query(default=None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    ceo_id = db.scalar(select(User.id).where(
        User.organization_id == user.organization_id, User.kind == "agent",
        User.hierarchy_level == "ceo", User.retired_at.is_(None),
    ).order_by(User.created_at))
    if not ceo_id:
        return {"unread_count": 0}
    stmt = select(func.count(AgentMessage.id)).where(
        AgentMessage.agent_id == ceo_id, AgentMessage.role == "agent",
        AgentMessage.author_id.is_(None),
    )
    if since:
        try:
            read_at = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if read_at.tzinfo:
                read_at = read_at.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError as error:
            raise HTTPException(422, "Invalid unread timestamp") from error
        stmt = stmt.where(AgentMessage.created_at > read_at)
    return {"unread_count": db.scalar(stmt) or 0}

def _hierarchy_config_out(cfg: HierarchyConfig) -> dict:
    return {"vp_count":cfg.vp_count,"directors_per_vp":cfg.directors_per_vp,"managers_per_director":cfg.managers_per_director,"workers_per_manager":cfg.workers_per_manager}

@app.get("/api/hierarchy-config")
def hierarchy_config(user:User=Depends(current_user),db:Session=Depends(get_db)):
    cfg = ensure_hierarchy_config(db, user.organization_id); db.commit()
    return _hierarchy_config_out(cfg)

@app.patch("/api/hierarchy-config")
def update_hierarchy_config(data: HierarchyConfigUpdate, user:User=Depends(human_admin), db:Session=Depends(get_db)):
    cfg = ensure_hierarchy_config(db, user.organization_id)
    values = data.model_dump(exclude_unset=True)
    proposed = {key: values.get(key, getattr(cfg, key)) for key in (
        "vp_count", "directors_per_vp", "managers_per_director", "workers_per_manager",
    )}
    if sum(desired_counts(**proposed).values()) > MAX_HIERARCHY_AGENTS:
        raise HTTPException(422, f"An organization can have at most {MAX_HIERARCHY_AGENTS} active agents")
    for key, value in values.items(): setattr(cfg, key, value)
    db.commit()
    with audit_context(user.kind, user.id, user.id if user.kind == "human" else None):
        reconcile_agents(db, user.organization_id)
    log(db, user.organization_id, user.id, "hierarchy_config", cfg.id, "updated", **values); db.commit()
    return _hierarchy_config_out(cfg)

def assign_task_to_agent(task_id: str, data: AssignAgent, user: User, db: Session):
    human_admin(user)
    t = db.get(Task, task_id)
    if not t: raise HTTPException(404, "Task not found")
    ensure_project(db, user, t.project_id)
    agent = db.get(User, data.agent_id)
    if not agent or agent.kind != "agent" or agent.organization_id != user.organization_id:
        raise HTTPException(404, "Agent not found")
    if agent.retired_at is not None:
        raise HTTPException(409, "This agent has been retired and cannot be assigned new work")
    if t.assignee_id == agent.id:
        return task_out(t, db)
    if t.status == "in_progress":
        previous = db.get(User, t.assignee_id) if t.assignee_id else None
        if previous and previous.kind == "agent":
            raise HTTPException(409, "Pause this agent's task before reassigning it. Its current work must be reviewed by a human first.")
        ensure_agent_has_no_active_conflict(db, agent.id, t.id)
    before_assignee_id = t.assignee_id
    t.assignee_id = agent.id
    sync_agent_task_pointers(db, t, before_assignee_id)
    agent.consecutive_task_failures = 0  # a fresh assignment is a human intervention -- give it a clean start
    log(db, user.organization_id, user.id, "task", t.id, "delegated_to_agent",
        agent_id=agent.id, from_assignee_id=before_assignee_id,
        to_assignee_id=agent.id, assignment_reason="human_reassignment")
    db.commit()
    return task_out(t, db)

@app.post("/api/tasks/{task_id}/assign-agent")
def assign_agent_route(task_id: str, data: AssignAgent, user:User=Depends(human_admin), db:Session=Depends(get_db)):
    return assign_task_to_agent(task_id, data, user, db)

def _get_org_agent(db: Session, user: User, agent_id: str) -> User:
    agent = db.get(User, agent_id)
    if not agent or agent.kind != "agent" or agent.organization_id != user.organization_id:
        raise HTTPException(404, "Agent not found")
    return agent

def agent_message_out(m: AgentMessage) -> dict:
    return {"id": m.id, "role": m.role, "body": m.body, "author_id": m.author_id, "created_at": m.created_at}

@app.get("/api/agents/{agent_id}/messages")
def agent_messages(agent_id: str, user:User=Depends(current_user), db:Session=Depends(get_db)):
    _get_org_agent(db, user, agent_id)
    rows = db.scalars(select(AgentMessage).where(AgentMessage.agent_id == agent_id).order_by(AgentMessage.created_at)).all()
    return [agent_message_out(m) for m in rows]

@app.post("/api/agents/{agent_id}/messages", status_code=201)
def send_agent_message(agent_id: str, data: AgentMessageIn, user:User=Depends(current_user), db:Session=Depends(get_db)):
    agent = _get_org_agent(db, user, agent_id)
    if agent.retired_at is not None:
        raise HTTPException(409, "This agent has been retired and can no longer be messaged")
    if not execution_allowed(db, agent):
        raise HTTPException(403, "Agent execution is disabled")
    agent.consecutive_task_failures = 0  # a human reaching out is an intervention -- give the agent a clean start
    db.add(AgentMessage(agent_id=agent.id, author_id=user.id, role="user", body=data.message))
    audit(db,user.organization_id,user.id,"agent",agent.id,"message_sent")
    scope_project_id = None
    scope_task_id = None
    if agent.current_task_id:
        current_task = db.get(Task, agent.current_task_id)
        if current_task:
            scope_project_id = current_task.project_id
            scope_task_id = current_task.id
    try:
        with audit_context("agent", agent.id, user.id):
            result = CopilotService(db, user.organization_id).plan(data.message, WorkspaceTools(db, agent, scope_project_id=scope_project_id, scope_task_id=scope_task_id))
        _issue_result_confirmations(result, agent, db)
    except OpenRouterRateLimitExceeded as error:
        db.rollback()
        raise HTTPException(429, "AI provider request limit reached for this workspace. Try again in a minute.", headers={"Retry-After": "60"}) from error
    except AgentAccessDenied as error:
        db.commit()
        raise HTTPException(403, "The agent needs additional access to answer this request. A workspace administrator must review its access grants.") from error
    except CopilotProviderError as error:
        db.commit()
        raise HTTPException(503, "The configured AI provider is unavailable. Try again later.") from error
    except (ValueError, IntegrityError) as error:
        db.rollback()
        raise HTTPException(503, "Unable to issue the Copilot proposal. Try again later.") from error
    with audit_context("agent", agent.id, user.id):
        audit(db,user.organization_id,agent.id,"agent",agent.id,"message_sent")
    db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body=result["reply"]))
    _record_ai_usage(db, user.organization_id)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(503, "Unable to issue the Copilot proposal. Try again later.") from error
    return result
@app.get("/api/notifications")
def notifications(user:User=Depends(current_user),db:Session=Depends(get_db)):
    # The newest 100: an inbox that returns every notification ever sent grows without bound.
    return [{"id":n.id,"title":n.title,"body":n.body,"href":n.href,"read":n.read,"created_at":n.created_at} for n in db.scalars(select(Notification).where(Notification.user_id==user.id).order_by(Notification.created_at.desc()).limit(100)).all()]

@app.get("/api/work-queue-summary")
def work_queue_summary(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Small tenant-scoped counters for live navigation badges across workspace views."""
    new_requests = db.scalar(select(func.count(InboundMessage.id)).where(
        InboundMessage.organization_id == user.organization_id, InboundMessage.status == "new",
    )) or 0
    plans_awaiting_approval = db.scalar(select(func.count(InboundPlan.id)).where(
        InboundPlan.organization_id == user.organization_id, InboundPlan.status == "pending",
    )) or 0
    blocked_tasks = db.scalar(select(func.count(Task.id)).join(Project).where(
        Project.organization_id == user.organization_id, Task.status == "blocked",
    )) or 0
    projects_awaiting_review = db.scalar(select(func.count(Project.id)).where(
        Project.organization_id == user.organization_id, Project.status == "review",
    )) or 0
    unread_notifications = db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read.is_(False),
    )) or 0
    return {
        "new_requests": new_requests,
        "plans_awaiting_approval": plans_awaiting_approval,
        "blocked_tasks": blocked_tasks,
        "projects_awaiting_review": projects_awaiting_review,
        "unread_notifications": unread_notifications,
    }

@app.patch("/api/notifications/{notification_id}/read")
def read_notification(notification_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    n=db.get(Notification,notification_id)
    if not n or n.user_id!=user.id: raise HTTPException(404,"Notification not found")
    n.read=True; log(db,user.organization_id,user.id,"notification",n.id,"read"); db.commit(); return {"ok":True}

def inbound_message_out(message: InboundMessage) -> dict:
    return {
        "id": message.id, "source": message.source, "external_id": message.external_id,
        "thread_id": message.thread_id, "sender": message.sender, "subject": message.subject,
        "body": message.body, "source_url": message.source_url,
        "received_at": message.received_at, "status": message.status,
        "triage_status": message.triage_status, "triage_category": message.triage_category,
        "triage_reason": message.triage_reason, "triage_summary": message.triage_summary,
        "triage_concerns": message.triage_concerns or [], "triage_attempts": message.triage_attempts,
        "created_at": message.created_at,
    }


@app.get("/api/inbound-messages")
def list_inbound_messages(
    status: Literal["new", "ignored", "planned"] | None = None,
    source: Literal["gmail", "slack", "notion", "manual"] | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    stmt = select(InboundMessage).where(InboundMessage.organization_id == user.organization_id)
    if status:
        stmt = stmt.where(InboundMessage.status == status)
    if source:
        stmt = stmt.where(InboundMessage.source == source)
    rows = db.scalars(stmt.order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc()).offset(offset).limit(limit)).all()
    return [inbound_message_out(message) for message in rows]


@app.post("/api/inbound-messages", status_code=201)
def create_inbound_message(
    data: InboundMessageCreate, user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must submit an incoming message")
    if data.external_id:
        existing = db.scalar(select(InboundMessage).where(
            InboundMessage.organization_id == user.organization_id,
            InboundMessage.source == data.source,
            InboundMessage.external_id == data.external_id,
        ))
        if existing:
            return {**inbound_message_out(existing), "duplicate": True}
    received_at = data.received_at or datetime.now(timezone.utc)
    if received_at.tzinfo:
        received_at = received_at.astimezone(timezone.utc).replace(tzinfo=None)
    message = InboundMessage(
        organization_id=user.organization_id, created_by=user.id,
        source=data.source, external_id=data.external_id, thread_id=data.thread_id,
        sender=data.sender, subject=data.subject, body=data.body,
        source_url=str(data.source_url) if data.source_url else None,
        received_at=received_at.replace(tzinfo=None), status="new",
    )
    db.add(message)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if data.external_id:
            existing = db.scalar(select(InboundMessage).where(
                InboundMessage.organization_id == user.organization_id,
                InboundMessage.source == data.source,
                InboundMessage.external_id == data.external_id,
            ))
            if existing:
                return {**inbound_message_out(existing), "duplicate": True}
        raise HTTPException(409, "This source message could not be stored")
    audit(db, user.organization_id, user.id, "inbound_message", message.id, "received", source=data.source)
    db.commit()
    return {**inbound_message_out(message), "duplicate": False}


@app.patch("/api/inbound-messages/{message_id}")
def update_inbound_message(
    message_id: str, data: InboundMessageStatus,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must review an incoming message")
    message = db.scalar(select(InboundMessage).where(
        InboundMessage.id == message_id, InboundMessage.organization_id == user.organization_id,
    ).with_for_update())
    if not message:
        raise HTTPException(404, "Incoming message not found")
    if message.status == "planned":
        raise HTTPException(409, "An approved request cannot be reopened")
    message.status = data.status
    audit(db, user.organization_id, user.id, "inbound_message", message.id, data.status)
    db.commit()
    return inbound_message_out(message)


def inbound_plan_out(plan: InboundPlan) -> dict:
    return {
        "id": plan.id, "inbound_message_id": plan.inbound_message_id,
        "status": plan.status, "summary": plan.summary, "proposal": plan.proposal,
        "planner_provider": plan.planner_provider, "project_id": plan.project_id,
        "created_at": plan.created_at, "reviewed_at": plan.reviewed_at,
    }


@app.get("/api/inbound-plans")
def list_inbound_plans(
    status: Literal["pending", "approved", "rejected"] | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    stmt = select(InboundPlan).where(InboundPlan.organization_id == user.organization_id)
    if status:
        stmt = stmt.where(InboundPlan.status == status)
    rows = db.scalars(stmt.order_by(InboundPlan.created_at.desc(), InboundPlan.id.desc()).offset(offset).limit(limit)).all()
    return [inbound_plan_out(plan) for plan in rows]


@app.post("/api/inbound-messages/{message_id}/plans", status_code=201)
def draft_inbound_plan(
    message_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must start request planning")
    message = db.scalar(select(InboundMessage).where(
        InboundMessage.id == message_id, InboundMessage.organization_id == user.organization_id,
    ))
    if not message:
        raise HTTPException(404, "Incoming message not found")
    if message.status == "ignored":
        raise HTTPException(409, "Reopen this request before planning it")
    if message.status == "planned":
        raise HTTPException(409, "This request already has an approved plan")
    pending = db.scalar(select(InboundPlan).where(
        InboundPlan.inbound_message_id == message.id,
        InboundPlan.organization_id == user.organization_id,
        InboundPlan.status == "pending",
    ).order_by(InboundPlan.created_at.desc()))
    if pending:
        return inbound_plan_out(pending)
    try:
        proposal, planner_provider = create_inbound_plan(db, user, message)
    except OpenRouterRateLimitExceeded as error:
        raise HTTPException(429, "AI provider request limit reached for this workspace. Try again in a minute.", headers={"Retry-After": "60"}) from error
    except CopilotProviderError as error:
        raise HTTPException(503, str(error)) from error
    plan = InboundPlan(
        organization_id=user.organization_id, inbound_message_id=message.id,
        status="pending", summary=proposal["summary"], proposal=proposal,
        planner_provider=planner_provider, created_by=user.id,
    )
    db.add(plan)
    db.flush()
    log(db, user.organization_id, user.id, "inbound_plan", plan.id, "drafted", source_message_id=message.id)
    _record_ai_usage(db, user.organization_id)
    db.commit()
    return inbound_plan_out(plan)


@app.patch("/api/inbound-plans/{plan_id}")
def edit_inbound_plan(
    plan_id: str, data: InboundPlanEdit,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must edit a request plan")
    plan = db.scalar(select(InboundPlan).where(
        InboundPlan.id == plan_id, InboundPlan.organization_id == user.organization_id,
    ).with_for_update())
    if not plan:
        raise HTTPException(404, "Request plan not found")
    if plan.status != "pending":
        raise HTTPException(409, "Only a pending plan can be edited")
    proposal = InboundPlanProposal.model_validate(data.proposal).model_dump()
    proposal["project"]["name"] = " ".join(proposal["project"]["name"].split())
    proposal["project"]["code"] = re.sub(r"[^A-Z0-9_-]", "", proposal["project"]["code"].strip().upper())[:16]
    if len(proposal["project"]["name"]) < 2:
        raise HTTPException(422, "Project name must contain at least two characters")
    if len(proposal["project"]["code"]) < 2:
        raise HTTPException(422, "Project code must contain at least two letters or numbers")
    for task_item in proposal["project"]["tasks"]:
        task_item["title"] = " ".join(task_item["title"].split())
        if not task_item["title"]:
            raise HTTPException(422, "Task titles cannot be empty")
    proposal = InboundPlanProposal.model_validate(proposal).model_dump()
    if not data.summary.strip():
        raise HTTPException(422, "Plan summary cannot be empty")
    plan.summary = data.summary.strip()
    plan.proposal = proposal
    log(db, user.organization_id, user.id, "inbound_plan", plan.id, "edited")
    db.commit()
    return inbound_plan_out(plan)


@app.post("/api/inbound-plans/{plan_id}/approve")
def approve_inbound_plan(
    plan_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must approve a request plan")
    plan = db.scalar(select(InboundPlan).where(
        InboundPlan.id == plan_id, InboundPlan.organization_id == user.organization_id,
    ).with_for_update())
    if not plan:
        raise HTTPException(404, "Request plan not found")
    if plan.status == "approved" and plan.project_id:
        project = db.get(Project, plan.project_id)
        tasks = db.scalars(select(Task).where(Task.project_id == project.id).order_by(Task.position)).all() if project else []
        return {"plan": inbound_plan_out(plan), "project": project_out(project, db) if project else None,
                "tasks": [task_out(task, db) for task in tasks]}
    if plan.status != "pending":
        raise HTTPException(409, "Only a pending plan can be approved")
    proposal = InboundPlanProposal.model_validate(plan.proposal).model_dump()["project"]
    message = db.get(InboundMessage, plan.inbound_message_id)
    if not message or message.status != "new":
        raise HTTPException(409, "Reopen the source request before approving this plan")
    name, code = proposal["name"].strip(), proposal["code"].strip().upper()
    duplicate = db.scalar(select(Project).where(
        Project.organization_id == user.organization_id,
        or_(Project.name.ilike(name), Project.code.ilike(code)),
    ))
    if duplicate:
        raise HTTPException(409, "A project with this name or code already exists. Edit the plan and try again.")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    project = Project(
        organization_id=user.organization_id, owner_id=user.id, name=name, code=code,
        description=proposal["description"], status="planning", health=proposal["health"],
    )
    db.add(project)
    db.flush()
    priority_order = {"high": 0, "medium": 1, "low": 2}
    ordered_tasks = sorted(enumerate(proposal["tasks"]), key=lambda item: (priority_order[item[1]["priority"]], item[0]))
    ceo = db.scalar(select(User).where(
        User.organization_id == user.organization_id, User.kind == "agent",
        User.hierarchy_level == "ceo", User.retired_at.is_(None), User.execution_enabled.is_(True),
    ).order_by(User.created_at).limit(1))
    organization = db.get(Organization, user.organization_id)
    tasks: list[Task] = []
    autonomous_execution_ready = organization.execution_enabled and CopilotService(db, user.organization_id).mode == "tool_calling"
    tasks_by_plan_index: dict[int, Task] = {}
    for position, (plan_index, item) in enumerate(ordered_tasks):
        # The CEO owns the approved task queue. The scheduler claims one task at a
        # time; CEO/managers can then split work into directly assigned subtasks.
        assignee_id = ceo.id if ceo and autonomous_execution_ready else user.id
        task = Task(
            project_id=project.id, title=item["title"].strip(), description=item["description"].strip(),
            status="todo", priority=item["priority"], assignee_id=assignee_id,
            reporter_id=user.id, position=position,
        )
        db.add(task)
        tasks.append(task)
        tasks_by_plan_index[plan_index] = task
    db.flush()
    for plan_index, item in enumerate(proposal["tasks"]):
        task = tasks_by_plan_index[plan_index]
        try:
            replace_dependencies(db, task, [tasks_by_plan_index[index].id for index in item.get("depends_on", [])])
        except ValueError as error:
            raise HTTPException(422, f"Invalid plan task prerequisites: {error}") from error
    if tasks and tasks[0].assignee_id:
        sync_agent_task_pointers(db, tasks[0], None)
    plan.status = "approved"
    plan.reviewed_by = user.id
    plan.reviewed_at = now_utc
    plan.project_id = project.id
    message.status = "planned"
    log(db, user.organization_id, user.id, "project", project.id, "created_from_inbound_plan", plan_id=plan.id, source_message_id=message.id)
    for task in tasks:
        log(db, user.organization_id, user.id, "task", task.id, "created_from_inbound_plan",
            plan_id=plan.id, priority=task.priority, assignee_id=task.assignee_id,
            requested_by_id=user.id, assignment_reason="inbound_plan")
    log(db, user.organization_id, user.id, "inbound_plan", plan.id, "approved", project_id=project.id, source_message_id=message.id)
    db.commit()
    return {"plan": inbound_plan_out(plan), "project": project_out(project, db),
            "tasks": [task_out(task, db) for task in tasks]}


@app.post("/api/inbound-plans/{plan_id}/reject")
def reject_inbound_plan(
    plan_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must review a request plan")
    plan = db.scalar(select(InboundPlan).where(
        InboundPlan.id == plan_id, InboundPlan.organization_id == user.organization_id,
    ).with_for_update())
    if not plan:
        raise HTTPException(404, "Request plan not found")
    if plan.status != "pending":
        raise HTTPException(409, "Only a pending plan can be rejected")
    plan.status = "rejected"
    plan.reviewed_by = user.id
    plan.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    log(db, user.organization_id, user.id, "inbound_plan", plan.id, "rejected", source_message_id=plan.inbound_message_id)
    db.commit()
    return inbound_plan_out(plan)

# Housekeeping that belongs in the audit log, not the team's activity feed.
FEED_EXCLUDED_ACTIONS = ("logged_in", "read", "password_reset_requested")
@app.get("/api/activity")
def activity(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":a.id,"action":a.action,"entity_type":a.entity_type,"detail":legacy_detail(a.detail or {}),"created_at":a.created_at,"actor":user_out(db.get(User,a.actor_id)) if a.actor_id else None} for a in db.scalars(select(Activity).where(Activity.organization_id==user.organization_id,Activity.action.notin_(FEED_EXCLUDED_ACTIONS)).order_by(Activity.created_at.desc()).limit(50)).all()]
@app.get("/api/search")
def search(q:str=Query(min_length=1, max_length=200),user:User=Depends(current_user),db:Session=Depends(get_db)):
    """Search authorized workspace knowledge and return bounded, timestamped citations."""
    from .models import Decision
    # `%` and `_` are LIKE wildcards: escaped, "100%" finds "100%" rather than everything.
    term="%"+q.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")+"%"
    project_scope = visible_project_ids(db, user)
    task_scope = task_is_visible(db, user)

    def excerpt(value: str | None) -> str:
        text = " ".join((value or "").split())
        if len(text) <= 280:
            return text
        position = text.casefold().find(q.casefold())
        start = max(0, position - 90) if position >= 0 else 0
        end = min(len(text), start + 260)
        return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")

    def stamp(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    ps=db.scalars(select(Project).where(
        Project.id.in_(project_scope),
        or_(Project.name.ilike(term,escape="\\"), Project.code.ilike(term,escape="\\"), Project.description.ilike(term,escape="\\")),
    ).order_by(Project.created_at.desc()).limit(20)).all()
    ts=db.scalars(select(Task).where(
        task_scope,
        or_(Task.title.ilike(term,escape="\\"), Task.description.ilike(term,escape="\\")),
    ).order_by(Task.updated_at.desc()).limit(30)).all()
    docs=db.scalars(select(ProjectDocument).where(
        document_is_visible(db, user),
        or_(ProjectDocument.file_name.ilike(term,escape="\\"), ProjectDocument.extracted_text.ilike(term,escape="\\")),
    ).order_by(ProjectDocument.created_at.desc()).limit(20)).all()
    attachment_rows=db.execute(select(Attachment, Task).join(Task, Task.id == Attachment.task_id).where(
        task_scope, Attachment.file_name.ilike(term,escape="\\"),
    ).order_by(Attachment.created_at.desc()).limit(20)).all()
    discussion_rows=db.execute(select(Comment, Task).join(Task, Task.id == Comment.task_id).where(
        task_scope, Comment.body.ilike(term,escape="\\"),
    ).order_by(Comment.created_at.desc()).limit(30)).all()
    decision_rows=db.scalars(select(Decision).where(
        Decision.organization_id == user.organization_id,
        Decision.project_id.in_(project_scope),
        or_(Decision.task_id.is_(None), Decision.task_id.in_(select(Task.id).where(task_scope))),
        or_(Decision.title.ilike(term,escape="\\"), Decision.body.ilike(term,escape="\\")),
    ).order_by(Decision.created_at.desc()).limit(20)).all()

    result_projects=[{"id":p.id,"type":"project","title":p.name,"name":p.name,"code":p.code,"snippet":excerpt(p.description),"created_at":stamp(p.created_at),"project_id":p.id,"task_id":None,"source_ref":f"/projects/{p.id}"} for p in ps]
    result_tasks=[]
    for task in ts:
        project = db.get(Project, task.project_id)
        if project:
            result_tasks.append({"id":task.id,"type":"task","title":task.title,"snippet":excerpt(task.description),"created_at":stamp(task.updated_at),"project_id":task.project_id,"task_id":task.id,"source_ref":f"/projects/{task.project_id}?task={task.id}","status":task.status,"priority":task.priority})
    result_docs=[]
    for document in docs:
        ref = f"/projects/{document.project_id}" if document.project_id else f"/api/project-documents/{document.id}/download"
        result_docs.append({"id":document.id,"type":"document","title":document.file_name,"snippet":excerpt(document.extracted_text),"created_at":stamp(document.created_at),"project_id":document.project_id,"task_id":None,"source_ref":ref,"file_name":document.file_name})
    result_attachments=[]
    for attachment, task in attachment_rows:
        result_attachments.append({"id":attachment.id,"type":"attachment","title":attachment.file_name,"snippet":"File attached to " + task.title,"created_at":stamp(attachment.created_at),"project_id":task.project_id,"task_id":task.id,"source_ref":f"/projects/{task.project_id}?task={task.id}","file_name":attachment.file_name})
    result_discussions=[]
    for comment, task in discussion_rows:
        result_discussions.append({"id":comment.id,"type":"discussion","title":f"Discussion on {task.title}","snippet":excerpt(comment.body),"created_at":stamp(comment.created_at),"project_id":task.project_id,"task_id":task.id,"source_ref":f"/projects/{task.project_id}?task={task.id}"})
    result_decisions=[]
    for decision in decision_rows:
        result_decisions.append({"id":decision.id,"type":"decision","title":decision.title,"snippet":excerpt(decision.body),"created_at":stamp(decision.created_at),"project_id":decision.project_id,"task_id":decision.task_id,"source_comment_id":decision.source_comment_id,"source_ref":f"/projects/{decision.project_id}" + (f"?task={decision.task_id}" if decision.task_id else "")})
    groups={"projects":result_projects,"tasks":result_tasks,"documents":result_docs,"attachments":result_attachments,"discussions":result_discussions,"decisions":result_decisions}
    groups["results"]=[item for key in ("projects","tasks","documents","attachments","discussions","decisions") for item in groups[key]]
    return groups
@app.get("/api/risks")
def risks(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.organization_id==user.organization_id,Project.health=="at_risk")).all()]
def _record_ai_usage(db: Session, organization_id: str) -> None:
    from .billing_service import BillingService
    BillingService(db).record_usage(organization_id, "ai_calls", 1)


@app.post("/api/ai/plan")
def ai_plan(data:AIRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if data.project_id:
        ensure_knowledge_project(db, user, data.project_id)
    try:
        with audit_context("copilot", user.id, user.id):
            result = CopilotService(db, user.organization_id).plan(data.message, WorkspaceTools(db, user, scope_project_id=data.project_id))
            _issue_result_confirmations(result, user, db)
            audit(db,user.organization_id,user.id,"copilot",user.id,"planned")
        _record_ai_usage(db, user.organization_id)
        db.commit()
        return result
    except OpenRouterRateLimitExceeded as error:
        db.rollback()
        raise HTTPException(429, "AI provider request limit reached for this workspace. Try again in a minute.", headers={"Retry-After": "60"}) from error
    except CopilotProviderError as error:
        raise HTTPException(503, "The configured AI provider is unavailable. Try again later.") from error
    except (ValueError, IntegrityError) as error:
        db.rollback()
        raise HTTPException(503, "Unable to issue the Copilot proposal. Try again later.") from error
def _without(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in keys}

WRITE_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], User, Session], Any]] = {
    "create_task": lambda args, user, db: create_task(TaskIn(**args), user, db),
    "update_task": lambda args, user, db: update_task(args["task_id"], TaskUpdate(**_without(args, "task_id")), user, db),
    "create_project": lambda args, user, db: create_project(ProjectIn(**args), user, db),
    "update_project": lambda args, user, db: update_project(args["project_id"], ProjectUpdate(**_without(args, "project_id")), user, db),
    "add_comment": lambda args, user, db: add_comment(args["task_id"], CommentIn(**_without(args, "task_id")), user, db),
    "create_team": lambda args, user, db: create_team(TeamIn(**args), user, db),
    "delegate_task": lambda args, user, db: assign_task_to_agent(args["task_id"], AssignAgent(agent_id=args["agent_id"]), user, db),
    REPO_PATCH_NAME: lambda args, user, db: apply_confirmed_repo_patch(args, user, db),
}

def _audit_before_state(tool: str, args: dict[str, Any], db: Session) -> dict[str, Any] | None:
    """Capture the pre-change state for update tools, for the confirmed-action audit record."""
    if tool == "update_task":
        t = db.get(Task, args.get("task_id"))
        return task_out(t, db) if t else None
    if tool == "update_project":
        p = db.get(Project, args.get("project_id"))
        return project_out(p, db) if p else None
    return None

def _issue_confirmation(token: str, issuer: User, db: Session) -> None:
    """Persist one server-issued confirmation before returning it to a client.

    The raw token and its arguments remain only in the signed client token.  The
    database keeps the minimum shared state needed to prove that exact token was
    issued by this server and to consume it once across API replicas.
    """
    try:
        claim = jwt.decode(token, CONFIRMATION_SECRET, algorithms=["HS256"], options={"require": ["exp", "kind", "sub", "org", "tool"]})
    except jwt.PyJWTError as error:
        raise ValueError("Generated an invalid Copilot confirmation") from error
    expected_kind = "agent_copilot_confirmation" if issuer.kind == "agent" else "copilot_confirmation"
    subject_id, organization_id, tool = claim.get("sub"), claim.get("org"), claim.get("tool")
    expires_at = claim.get("exp")
    if (claim.get("kind") != expected_kind or subject_id != issuer.id or organization_id != issuer.organization_id
            or not isinstance(tool, str) or tool not in WRITE_TOOL_HANDLERS or not isinstance(expires_at, (int, float))):
        raise ValueError("Generated an invalid Copilot confirmation")
    db.add(CopilotConfirmation(
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        subject_id=subject_id,
        organization_id=organization_id,
        tool=tool,
        expires_at=datetime.fromtimestamp(expires_at, timezone.utc).replace(tzinfo=None),
    ))
    # Flush now: no token is returned if its pending state cannot be persisted.
    db.flush()


def _issue_result_confirmations(result: dict[str, Any], issuer: User, db: Session) -> None:
    for action in result.get("actions", []):
        token = action.get("confirmation_token") if isinstance(action, dict) else None
        if token is not None:
            if not isinstance(token, str) or not action.get("requires_confirmation"):
                raise ValueError("Generated an invalid Copilot proposal")
            _issue_confirmation(token, issuer, db)


def _consume_confirmation_once(token: str, subject_id: str, organization_id: str, tool: str, db: Session) -> str:
    """Atomically transition an issued, matching proposal from pending to consumed."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    consumed = db.execute(
        update(CopilotConfirmation)
        .where(
            CopilotConfirmation.token_hash == token_hash,
            CopilotConfirmation.subject_id == subject_id,
            CopilotConfirmation.organization_id == organization_id,
            CopilotConfirmation.tool == tool,
            CopilotConfirmation.expires_at >= now,
            CopilotConfirmation.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    if consumed.rowcount == 1:
        db.commit()
        return "consumed"

    state = db.scalar(select(CopilotConfirmation).where(CopilotConfirmation.token_hash == token_hash))
    if state is None:
        db.rollback()
        return "missing"
    if state.consumed_at is not None:
        db.rollback()
        return "used"
    db.rollback()
    return "unavailable"

@app.post("/api/ai/confirm")
def ai_confirm(data:ConfirmAction,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must confirm this action")
    proposer_agent_id: str | None = None
    try:
        tool, args = read_confirmation(data.confirmation_token, user)
    except ValueError as self_error:
        try:
            tool, args, proposer_agent_id = read_agent_confirmation(data.confirmation_token, user, db)
        except ValueError:
            raise HTTPException(400, str(self_error)) from self_error
    if proposer_agent_id:
        proposer = db.scalar(select(User).where(User.id == proposer_agent_id, User.organization_id == user.organization_id, User.kind == "agent"))
        if not proposer or not execution_allowed(db, proposer):
            raise HTTPException(403, "Agent execution is disabled")
        if not agent_tool_allowed(db, proposer, tool):
            from .agent_access import record_access_denial
            record_access_denial(db, proposer, tool=tool, reason="revoked")
            raise HTTPException(403, "The agent no longer has access to this action.")
        project_id = args.get("project_id")
        task_id = args.get("task_id")
        if task_id and not db.scalar(select(Task.id).where(Task.id == task_id, Task.assignee_id == proposer.id)):
            from .agent_access import record_access_denial
            record_access_denial(db, proposer, tool=tool, resource_type="project", reason="out_of_scope")
            raise HTTPException(403, "The agent no longer owns the task for this action.")
        report_id = args.get("agent_id")
        if report_id and not db.scalar(select(User.id).where(
            User.id == report_id, User.organization_id == proposer.organization_id,
            User.kind == "agent", User.parent_agent_id == proposer.id,
            User.active.is_(True), User.retired_at.is_(None),
        )):
            from .agent_access import record_access_denial
            record_access_denial(db, proposer, tool=tool, resource_type="tool", reason="out_of_scope")
            raise HTTPException(403, "The target is no longer a direct report for this action.")
        if project_id and not agent_project_access(db, proposer, project_id, "write", task_id=task_id):
            record_access_denial(db, proposer, tool=tool, resource_type="project", reason="revoked")
            raise HTTPException(403, "The agent no longer has access to this project.")
    handler = WRITE_TOOL_HANDLERS.get(tool)
    if not handler:
        raise HTTPException(400, "Unsupported action")
    if user.role not in TOOL_ROLES.get(tool, frozenset()):
        raise HTTPException(403, "Your role cannot perform this Copilot action.")
    proposal_hash = hashlib.sha256(data.confirmation_token.encode()).hexdigest()
    linked_proposal = db.scalar(select(CopilotConfirmation).where(
        CopilotConfirmation.token_hash == proposal_hash,
        CopilotConfirmation.organization_id == user.organization_id,
        CopilotConfirmation.task_id.is_not(None),
    ))
    confirmation_state = _consume_confirmation_once(
        data.confirmation_token, proposer_agent_id or user.id, user.organization_id, tool, db
    )
    if confirmation_state == "used":
        raise HTTPException(409, "This Copilot proposal has already been used. Ask again for a new proposal.")
    if confirmation_state != "consumed":
        raise HTTPException(400, "This Copilot proposal is not available. Ask again for a new proposal.")
    before = _audit_before_state(tool, args, db)
    try:
        with audit_context("agent" if proposer_agent_id else "copilot", proposer_agent_id or user.id, user.id):
            result = handler(args, user, db)
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    except (TypeError, KeyError, ValidationError) as error:
        db.rollback()
        raise HTTPException(422, "This Copilot proposal is missing required details.") from error
    with audit_context("agent" if proposer_agent_id else "copilot", proposer_agent_id or user.id, user.id):
        log(db, user.organization_id, user.id, "copilot", user.id, "confirmed",
            tool=tool, args=jsonable_encoder(args), before=jsonable_encoder(before),
            after=jsonable_encoder(result) if isinstance(result, dict) else None,
            proposed_by_agent_id=proposer_agent_id)
    if linked_proposal and linked_proposal.task_id:
        linked_task = db.get(Task, linked_proposal.task_id)
        if linked_task and linked_task.status == "review":
            linked_task.status = "in_progress"
            linked_task.blocked_reason = None
            linked_task.escalation_category = None
            db.add(Comment(task_id=linked_task.id, author_id=user.id,
                           body=f"Approved agent action: {linked_proposal.label or tool}. The agent can continue."))
            with audit_context("human", user.id):
                log(db, user.organization_id, user.id, "task", linked_task.id, "agent_action_approved",
                    reviewed_by_id=user.id, approved_by_id=user.id,
                    proposed_by_agent_id=proposer_agent_id)
    db.commit()
    return result


@app.get("/api/agent-proposals")
def list_agent_proposals(user:User=Depends(current_user), db:Session=Depends(get_db)):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must review agent proposals")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = db.scalars(select(CopilotConfirmation).where(
        CopilotConfirmation.organization_id == user.organization_id,
        CopilotConfirmation.task_id.is_not(None), CopilotConfirmation.token_ciphertext.is_not(None),
        CopilotConfirmation.consumed_at.is_(None), CopilotConfirmation.rejected_at.is_(None),
        CopilotConfirmation.expires_at >= now,
    ).order_by(CopilotConfirmation.created_at.desc())).all()
    result = []
    for row in rows:
        task = db.get(Task, row.task_id)
        agent = db.get(User, row.subject_id)
        if not task or not agent or task.project_id is None:
            continue
        result.append({
            "id": row.id, "task_id": task.id, "task_title": task.title,
            "project_id": task.project_id, "agent_name": agent.name,
            "label": row.label or row.tool, "details": row.details or [],
            "confirmation_token": secrets_store.decrypt(row.token_ciphertext),
            "expires_at": row.expires_at,
        })
    return result


@app.post("/api/agent-proposals/{proposal_id}/reject")
def reject_agent_proposal(proposal_id:str, user:User=Depends(current_user), db:Session=Depends(get_db)):
    if user.kind != "human" or not user.active:
        raise HTTPException(403, "An active human must review agent proposals")
    row = db.scalar(select(CopilotConfirmation).where(
        CopilotConfirmation.id == proposal_id,
        CopilotConfirmation.organization_id == user.organization_id,
        CopilotConfirmation.task_id.is_not(None),
        CopilotConfirmation.consumed_at.is_(None), CopilotConfirmation.rejected_at.is_(None),
    ))
    if not row:
        raise HTTPException(404, "Pending agent proposal not found")
    task = db.get(Task, row.task_id)
    if task and task.status == "review":
        task.status = "blocked"
        task.blocked_reason = f"A human rejected the proposed action: {row.label or row.tool}. Review and unblock when ready."
        task.escalation_category = "strategic_decision"
        db.add(Comment(task_id=task.id, author_id=user.id, body=f"Rejected agent action: {row.label or row.tool}. The task is paused for review."))
        log(db, user.organization_id, user.id, "task", task.id, "agent_action_rejected",
            reviewed_by_id=user.id, proposed_by_agent_id=row.subject_id)
    row.rejected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return {"ok": True}
