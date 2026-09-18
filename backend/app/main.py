import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session
from .auth import create_token, current_user, hash_password, verify_password
from .database import Base, engine, get_db
from .models import Activity, AgentMessage, Attachment, Comment, HierarchyConfig, Notification, Organization, Project, ProjectDocument, Task, Team, User
from .copilot import CopilotProviderError, CopilotService, TOOL_ROLES, WorkspaceTools, read_agent_confirmation, read_confirmation
from .observability import execution_allowed, audit_context, audit, provider_call, legacy_detail, configure_logging
from . import execution
from .hierarchy import MAX_HIERARCHY_AGENTS, desired_counts, reconcile_agents
from .ratelimit import build_rate_limiter
from .services import ProjectDraftProvider, ensure_hierarchy_config, extract_document_text, log, seed, sync_agent_task_pointers
from .storage import get_storage
from .indexing import index_document

environment = os.getenv("ENVIRONMENT", "development").strip().lower()
demo_seed_enabled = os.getenv("ENABLE_DEMO_SEED", "false" if environment == "production" else "true").strip().lower() in {"1", "true", "yes", "on"}
docs_enabled = os.getenv("ENABLE_API_DOCS", "false" if environment == "production" else "true").strip().lower() in {"1", "true", "yes", "on"}

app = FastAPI(
    title="Enter AI API",
    version="0.1.0",
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
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
from .privacy import router as privacy_router, admin_router as admin_privacy_router
from . import privacy_service
from .sso_routes import router_admin as sso_admin_router, router_login as sso_login_router, router_scim as scim_router
from .enterprise_routes import router as enterprise_router
app.include_router(admin_router)
app.include_router(sso_admin_router)
app.include_router(sso_login_router)
app.include_router(scim_router)
app.include_router(enterprise_router)
app.include_router(accounts_router)
app.include_router(billing_webhook_router)
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

@app.on_event("startup")
def startup():
    configure_logging()
    with next(get_db()) as db: seed(db, demo_content=demo_seed_enabled)
    execution.start_background_loop()
    privacy_service.start_background_loop()
    from . import observability as _observability
    _observability.start_aggregation_background_loop()

@app.on_event("shutdown")
def shutdown():
    execution.stop_background_loop()
    privacy_service.stop_background_loop()
    from . import observability as _observability
    _observability.stop_aggregation_background_loop()

class SignIn(BaseModel): email: str = Field(min_length=3, max_length=255); password: str
class Register(BaseModel): organization_name: str = Field(min_length=2); name: str; email: EmailStr; password: str = Field(min_length=8)
class ProjectIn(BaseModel): name: str; code: str; description: str | None = None; team_id: str | None = None; health: str = "on_track"; color: str = "#22c55e"; due_date: datetime | None = None; source_document_ids: list[str] = []
class ProjectUpdate(BaseModel): name: str | None = None; code: str | None = None; description: str | None = None; status: str | None = None; health: str | None = None; color: str | None = None; team_id: str | None = None; due_date: datetime | None = None
class TaskIn(BaseModel): project_id: str; title: str; description: str | None = None; parent_id: str | None = None; status: str = "todo"; priority: str = "medium"; assignee_id: str | None = None; due_date: datetime | None = None; labels: list[str] = []
class TaskUpdate(BaseModel): title: str | None = None; description: str | None = None; status: str | None = None; priority: str | None = None; assignee_id: str | None = None; due_date: datetime | None = None; labels: list[str] | None = None
class CommentIn(BaseModel): body: str = Field(min_length=1)
class TeamIn(BaseModel): name: str; description: str | None = None
class AIRequest(BaseModel): message: str = Field(min_length=1, max_length=4000); project_id: str | None = None
class ConfirmAction(BaseModel): confirmation_token: str = Field(min_length=20, max_length=10000)
class AssignAgent(BaseModel): agent_id: str
class AgentMessageIn(BaseModel): message: str = Field(min_length=1, max_length=4000)
class HierarchyConfigUpdate(BaseModel):
    vp_count: int | None = Field(default=None, ge=0, le=20)
    directors_per_vp: int | None = Field(default=None, ge=0, le=20)
    managers_per_director: int | None = Field(default=None, ge=0, le=20)
    workers_per_manager: int | None = Field(default=None, ge=0, le=20)

def user_out(user: User): return {"id":user.id,"name":user.name,"email":user.email,"role":user.role,"kind":user.kind,"title":user.title,"avatar":user.avatar,"email_verified":user.email_verified_at is not None}
def project_out(p: Project, db: Session):
    total = db.query(Task).filter(Task.project_id == p.id, Task.parent_id.is_(None)).count(); done = db.query(Task).filter(Task.project_id == p.id, Task.status == "done", Task.parent_id.is_(None)).count()
    owner = db.get(User,p.owner_id) if p.owner_id else None; team = db.get(Team,p.team_id) if p.team_id else None
    documents = db.scalars(select(ProjectDocument).where(ProjectDocument.project_id == p.id)).all()
    return {"id":p.id,"name":p.name,"code":p.code,"description":p.description,"status":p.status,"health":p.health,"color":p.color,"due_date":p.due_date,"owner":user_out(owner) if owner else None,"team":team.name if team else None,"progress":round(done/total*100) if total else 0,"task_count":total,"documents":[{"id":d.id,"file_name":d.file_name,"created_at":d.created_at} for d in documents]}
def task_out(t: Task, db: Session):
    assignee = db.get(User,t.assignee_id) if t.assignee_id else None
    return {"id":t.id,"project_id":t.project_id,"parent_id":t.parent_id,"title":t.title,"description":t.description,"status":t.status,"priority":t.priority,"assignee":user_out(assignee) if assignee else None,"due_date":t.due_date,"labels":t.labels,"completed_at":t.completed_at,"created_at":t.created_at,"updated_at":t.updated_at}
def ensure_project(db, user, project_id):
    project = db.get(Project, project_id)
    if not project or project.organization_id != user.organization_id: raise HTTPException(404,"Project not found")
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
    if assignee.kind == "agent":
        human_admin(user)
        if assignee.retired_at is not None:
            raise HTTPException(409, "This agent has been retired and cannot be assigned new work")
    return assignee

def ensure_task_parent(db, project, parent_id):
    """A subtask must live in the same project as its parent; otherwise the task tree
    can be stitched across projects -- and across organizations."""
    if not parent_id: return None
    parent = db.get(Task, parent_id)
    if not parent or parent.project_id != project.id:
        raise HTTPException(422, "Parent task not found in this project")
    return parent

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

@app.get("/health")
def health(db: Session = Depends(get_db)):
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
    healthy = checks["db"] == "ok" and checks["redis"] != "error"
    body = {"ok": healthy, **checks}
    if not healthy:
        raise HTTPException(status_code=503, detail=body)
    return body

@app.get("/")
def root(): return {"service": "Enter AI API", "status": "ok"}

# A real hash of a value nobody holds, verified against when the submitted address
# has no account so that both paths do the same amount of password hashing.
_ABSENT_USER_HASH = hash_password(os.urandom(24).hex())

@app.post("/api/auth/register")
def register(data: Register, request: Request, db: Session = Depends(get_db)):
    """Self-serve signup: creates the organization and its first admin.

    A taken email address is answered with the same message as any other rejected
    signup, and the notice that an account already exists goes to the address
    itself rather than to whoever submitted the form. An organization name clash is
    reported plainly -- names are chosen, not secret, and the person needs to know
    to pick another one.
    """
    enforce(_SIGNUP_LIMITER, request)
    address = accounts.normalize(data.email)
    slug = data.organization_name.lower().replace(" ","-")[:70]
    existing = db.scalar(select(User).where(func.lower(User.email) == address))
    if existing:
        organization = db.get(Organization, existing.organization_id)
        mailer.send_existing_account_notice(address, organization.name if organization else "Enter AI")
        raise HTTPException(409, "We couldn't create an account with those details.")
    if db.scalar(select(Organization).where(Organization.slug == slug)): raise HTTPException(409,"Organization already exists")
    org = Organization(name=data.organization_name, slug=slug); db.add(org); db.flush()
    user = User(organization_id=org.id,name=data.name,email=address,password_hash=hash_password(data.password),role="admin",avatar="".join(x[0] for x in data.name.split())[:2].upper()); db.add(user)
    ensure_hierarchy_config(db, org.id)
    from .billing_service import ensure_trial_subscription
    ensure_trial_subscription(db, org.id)
    db.flush(); log(db, org.id, user.id, "user", user.id, "registered")
    verification = accounts.issue_verification(db, user)
    db.commit()
    mailer.send_verification(user.email, user.name, verification)
    return {"token":create_token(user),"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.post("/api/auth/login")
def login(data: SignIn, request: Request, db: Session = Depends(get_db)):
    enforce(_LOGIN_LIMITER, request)
    user = db.scalar(select(User).where(func.lower(User.email) == accounts.normalize(data.email)))
    if not user:
        # Hash anyway. Returning early for an unknown address answers in a fraction
        # of the time a real one takes, which turns the login form into an account
        # enumeration oracle no matter how generic the message is.
        verify_password(data.password, _ABSENT_USER_HASH)
        raise HTTPException(401,"Incorrect email or password")
    if not verify_password(data.password,user.password_hash): raise HTTPException(401,"Incorrect email or password")
    if not user.active: raise HTTPException(401,"Incorrect email or password")
    from .enterprise_models import SessionPolicy
    policy = db.scalar(select(SessionPolicy).where(SessionPolicy.organization_id == user.organization_id))
    if policy and policy.sso_only_enforced:
        raise HTTPException(403, "This organization requires signing in through SSO.")
    log(db,user.organization_id,user.id,"user",user.id,"logged_in"); db.commit()
    org=db.get(Organization,user.organization_id); return {"token":create_token(user),"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)): return {"user":user_out(user),"organization":{"id":user.organization_id,"name":db.get(Organization,user.organization_id).name}}

@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    projects=db.scalars(select(Project).where(Project.organization_id==user.organization_id).order_by(Project.created_at.desc())).all()
    assigned=db.scalars(select(Task).join(Project).where(Project.organization_id==user.organization_id, Task.assignee_id==user.id).order_by(Task.due_date)).all()
    tasks=sorted(assigned,key=lambda task: (task.status=="done", task.due_date or datetime.max))
    now = datetime.now(timezone.utc); week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    return {"projects":[project_out(p,db) for p in projects],"my_tasks":[task_out(t,db) for t in tasks],"stats":{"active_projects":len([p for p in projects if p.status=="active"]),"at_risk":len([p for p in projects if p.health=="at_risk"]),"my_open_tasks":len([task for task in assigned if task.status!="done"]),"completed_this_week":len([task for task in assigned if task.completed_at and task.completed_at >= week_start])}}

@app.get("/api/projects")
def projects(user: User = Depends(current_user), db: Session = Depends(get_db)): return [project_out(p,db) for p in db.scalars(select(Project).where(Project.organization_id==user.organization_id)).all()]
@app.post("/api/projects", status_code=201)
def create_project(data: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_org_team(db, user, data.team_id)
    name, code = data.name.strip(), data.code.strip().upper()
    if not name or not code: raise HTTPException(422, "Project name and code are required")
    duplicate = db.scalar(select(Project).where(Project.organization_id==user.organization_id, or_(Project.name.ilike(name), Project.code.ilike(code))))
    if duplicate: raise HTTPException(409, "A project with this name or code already exists")
    values=data.model_dump(exclude={"source_document_ids"}); values.update(name=name, code=code)
    p=Project(**values,organization_id=user.organization_id,owner_id=user.id); db.add(p); db.flush()
    if data.source_document_ids:
        docs=db.scalars(select(ProjectDocument).where(ProjectDocument.id.in_(data.source_document_ids),ProjectDocument.organization_id==user.organization_id,ProjectDocument.project_id.is_(None))).all()
        for document in docs: document.project_id=p.id
    log(db,user.organization_id,user.id,"project",p.id,"created",name=p.name); db.commit(); return project_out(p,db)
@app.get("/api/projects/{project_id}")
def project(project_id: str,user: User=Depends(current_user),db:Session=Depends(get_db)): return project_out(ensure_project(db,user,project_id),db)
@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, data: ProjectUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = ensure_project(db, user, project_id)
    values = data.model_dump(exclude_unset=True)
    if "name" in values: values["name"] = values["name"].strip()
    if "code" in values: values["code"] = values["code"].strip().upper()
    if values.get("name") == "" or values.get("code") == "": raise HTTPException(422, "Project name and code cannot be empty")
    if "team_id" in values: ensure_org_team(db, user, values["team_id"])
    duplicate = db.scalar(select(Project).where(Project.organization_id==user.organization_id, Project.id != p.id, or_(Project.name.ilike(values.get("name", p.name)), Project.code.ilike(values.get("code", p.code)))))
    if duplicate: raise HTTPException(409, "A project with this name or code already exists")
    for key, value in values.items(): setattr(p, key, value)
    log(db, user.organization_id, user.id, "project", p.id, "updated", fields=list(values)); db.commit(); return project_out(p, db)
@app.get("/api/projects/{project_id}/tasks")
def project_tasks(project_id: str,user: User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_project(db,user,project_id); return [task_out(t,db) for t in db.scalars(select(Task).where(Task.project_id==project_id).order_by(Task.position)).all()]

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
    ensure_project(db,user,project_id)
    return [{"id":d.id,"file_name":d.file_name,"content_type":d.content_type,"created_at":d.created_at} for d in db.scalars(select(ProjectDocument).where(ProjectDocument.project_id==project_id)).all()]

@app.post("/api/projects/{project_id}/documents",status_code=201)
async def add_project_document(project_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_project(db,user,project_id)
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
    p=ensure_project(db,user,data.project_id)
    ensure_org_assignee(db,user,data.assignee_id); ensure_task_parent(db,p,data.parent_id)
    t=Task(**data.model_dump(),reporter_id=user.id); db.add(t); db.flush(); log(db,user.organization_id,user.id,"task",t.id,"created",title=t.title); db.commit(); return task_out(t,db)
@app.patch("/api/tasks/{task_id}")
def update_task(task_id:str,data:TaskUpdate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    p=ensure_project(db,user,t.project_id)
    if "assignee_id" in data.model_fields_set:
        ensure_org_assignee(db, user, data.assignee_id)
        if t.assignee_id and t.assignee_id != data.assignee_id:
            previous_assignee = db.get(User, t.assignee_id)
            if previous_assignee and previous_assignee.kind == "agent":
                human_admin(user)
    before, before_assignee_id = t.status, t.assignee_id
    for key,value in data.model_dump(exclude_unset=True).items(): setattr(t,key,value)
    if "status" in data.model_fields_set and data.status != before:
        t.completed_at = datetime.now(timezone.utc) if data.status == "done" else None
    sync_agent_task_pointers(db, t, before_assignee_id)
    log(db,user.organization_id,user.id,"task",t.id,"updated",from_status=before,to_status=t.status); db.commit(); return task_out(t,db)
def detach_task_references(db, task):
    """PostgreSQL enforces the foreign keys SQLite ignores, so every row pointing at
    this task has to be resolved before the DELETE or the request fails outright.
    Comments and attachments are owned by the task and go with it; subtasks are
    promoted to top level rather than silently deleting work nobody asked to delete;
    agent pointers are cleared so the agent outlives its task."""
    db.execute(delete(Comment).where(Comment.task_id == task.id))
    db.execute(delete(Attachment).where(Attachment.task_id == task.id))
    db.execute(update(Task).where(Task.parent_id == task.id).values(parent_id=None))
    db.execute(update(User).where(User.current_task_id == task.id).values(current_task_id=None))
    db.execute(update(User).where(User.last_completed_task_id == task.id).values(last_completed_task_id=None))

@app.delete("/api/tasks/{task_id}",status_code=204)
def delete_task(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    ensure_project(db,user,t.project_id); log(db,user.organization_id,user.id,"task",t.id,"deleted")
    detach_task_references(db,t); db.delete(t); db.commit()

@app.get("/api/tasks/{task_id}/comments")
def comments(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    ensure_project(db,user,t.project_id)
    comments_list = db.scalars(select(Comment).where(Comment.task_id==task_id)).all()
    author_ids = [c.author_id for c in comments_list if c.author_id]
    authors = {a.id: a for a in db.scalars(select(User).where(User.id.in_(author_ids))).all()}
    return [{"id":c.id,"body":c.body,"created_at":c.created_at,"author":user_out(authors.get(c.author_id))} for c in comments_list]
@app.post("/api/tasks/{task_id}/comments",status_code=201)
def add_comment(task_id:str,data:CommentIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    ensure_project(db,user,t.project_id); c=Comment(task_id=task_id,author_id=user.id,body=data.body); db.add(c); log(db,user.organization_id,user.id,"task",task_id,"commented"); db.commit(); return {"id":c.id,"body":c.body}
@app.post("/api/tasks/{task_id}/attachments",status_code=201)
async def add_attachment(task_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    ensure_project(db,user,t.project_id)
    raw=await file.read()
    if len(raw) > MAX_UPLOAD_BYTES: raise HTTPException(413,"Attachments must be 5 MB or smaller")
    path=get_storage().save("task-attachments", file.filename or "attachment", raw); a=Attachment(task_id=task_id,uploaded_by=user.id,file_name=file.filename or "attachment",path=path,content_type=file.content_type); db.add(a); log(db,user.organization_id,user.id,"task",task_id,"attachment_uploaded"); db.commit(); return {"id":a.id,"file_name":a.file_name}

@app.get("/api/teams")
def teams(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":t.id,"name":t.name,"description":t.description,"projects":db.query(Project).filter(Project.team_id==t.id, Project.organization_id==user.organization_id).count()} for t in db.scalars(select(Team).where(Team.organization_id==user.organization_id)).all()]
@app.get("/api/teams/{team_id}/projects")
def team_projects(team_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    team=db.get(Team,team_id)
    if not team or team.organization_id!=user.organization_id: raise HTTPException(404,"Team not found")
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.team_id==team_id, Project.organization_id==user.organization_id)).all()]
@app.post("/api/teams",status_code=201)
def create_team(data:TeamIn,user:User=Depends(human_admin),db:Session=Depends(get_db)):
    t=Team(organization_id=user.organization_id,**data.model_dump()); db.add(t); db.flush(); log(db,user.organization_id,user.id,"team",t.id,"created"); db.commit(); return {"id":t.id,"name":t.name,"description":t.description}
@app.get("/api/users")
def users(user:User=Depends(current_user),db:Session=Depends(get_db)): return [user_out(u) for u in db.scalars(select(User).where(User.organization_id==user.organization_id,User.kind=="human")).all()]

def agent_out(a: User, db: Session) -> dict:
    current = db.get(Task, a.current_task_id) if a.current_task_id else None
    last_completed = db.get(Task, a.last_completed_task_id) if a.last_completed_task_id else None
    blocked = a.current_task_id is not None and a.consecutive_task_failures >= execution.MAX_CONSECUTIVE_FAILURES
    return {
        "id": a.id, "name": a.name, "title": a.title, "avatar": a.avatar,
        "hierarchy_level": a.hierarchy_level, "parent_agent_id": a.parent_agent_id,
        "current_task": task_out(current, db) if current else None,
        "last_completed_task": task_out(last_completed, db) if last_completed else None,
        "is_working": a.current_task_id is not None and not blocked,
        "is_blocked": blocked,
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
    prospective = {**_hierarchy_config_out(cfg), **values}
    total = sum(desired_counts(**prospective).values())
    if total > MAX_HIERARCHY_AGENTS:
        raise HTTPException(422, f"This configuration would create {total} agents, above the {MAX_HIERARCHY_AGENTS}-agent limit. Reduce the counts per level.")
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
    before_assignee_id = t.assignee_id
    t.assignee_id = agent.id
    sync_agent_task_pointers(db, t, before_assignee_id)
    agent.consecutive_task_failures = 0  # a fresh assignment is a human intervention -- give it a clean start
    log(db, user.organization_id, user.id, "task", t.id, "delegated_to_agent", agent_id=agent.id, agent_name=agent.name)
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
    if not AI_PLAN_RATE_LIMITER.allow(user.id):
        raise HTTPException(429, "Too many Copilot requests. Wait a moment and try again.")
    _enforce_ai_usage_limit(db, user.organization_id)
    agent = _get_org_agent(db, user, agent_id)
    if agent.retired_at is not None:
        raise HTTPException(409, "This agent has been retired and can no longer be messaged")
    if not execution_allowed(db, agent):
        raise HTTPException(403, "Agent execution is disabled")
    agent.consecutive_task_failures = 0  # a human reaching out is an intervention -- give the agent a clean start
    db.add(AgentMessage(agent_id=agent.id, author_id=user.id, role="user", body=data.message))
    audit(db,user.organization_id,user.id,"agent",agent.id,"message_sent")
    scope_project_id = None
    if agent.current_task_id:
        current_task = db.get(Task, agent.current_task_id)
        if current_task:
            scope_project_id = current_task.project_id
    try:
        with audit_context("agent", agent.id, user.id):
            result = CopilotService().plan(data.message, WorkspaceTools(db, agent, scope_project_id=scope_project_id))
    except CopilotProviderError as error:
        db.commit()
        raise HTTPException(503, "The configured AI provider is unavailable. Try again later.") from error
    with audit_context("agent", agent.id, user.id):
        audit(db,user.organization_id,agent.id,"agent",agent.id,"message_sent")
    db.add(AgentMessage(agent_id=agent.id, author_id=None, role="agent", body=result["reply"]))
    _record_ai_usage(db, user.organization_id)
    db.commit()
    return result
@app.get("/api/notifications")
def notifications(user:User=Depends(current_user),db:Session=Depends(get_db)): return [{"id":n.id,"title":n.title,"body":n.body,"href":n.href,"read":n.read,"created_at":n.created_at} for n in db.scalars(select(Notification).where(Notification.user_id==user.id).order_by(Notification.created_at.desc())).all()]
@app.patch("/api/notifications/{notification_id}/read")
def read_notification(notification_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    n=db.get(Notification,notification_id)
    if not n or n.user_id!=user.id: raise HTTPException(404,"Notification not found")
    n.read=True; log(db,user.organization_id,user.id,"notification",n.id,"read"); db.commit(); return {"ok":True}
@app.get("/api/activity")
def activity(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":a.id,"action":a.action,"entity_type":a.entity_type,"detail":legacy_detail(a.detail or {}),"created_at":a.created_at,"actor":user_out(db.get(User,a.actor_id)) if a.actor_id else None} for a in db.scalars(select(Activity).where(Activity.organization_id==user.organization_id).order_by(Activity.created_at.desc()).limit(50)).all()]
@app.get("/api/search")
def search(q:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    term=f"%{q}%"; ps=db.scalars(select(Project).where(Project.organization_id==user.organization_id,or_(Project.name.ilike(term),Project.code.ilike(term)))).all(); ts=db.scalars(select(Task).join(Project).where(Project.organization_id==user.organization_id,Task.title.ilike(term))).all(); return {"projects":[project_out(p,db) for p in ps],"tasks":[task_out(t,db) for t in ts]}
@app.get("/api/risks")
def risks(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.organization_id==user.organization_id,Project.health=="at_risk")).all()]
AI_PLAN_RATE_LIMITER = build_rate_limiter(
    max_calls=int(os.getenv("AI_PLAN_RATE_LIMIT_PER_MINUTE", "20")), window_seconds=60,
    namespace="ai-plan",
)

def _enforce_ai_usage_limit(db: Session, organization_id: str) -> None:
    from .billing_service import BillingService
    if not BillingService(db).enforce_plan_limit(organization_id, "ai_calls", 1):
        raise HTTPException(402, "This workspace's AI usage limit has been reached for the current plan.")


def _record_ai_usage(db: Session, organization_id: str) -> None:
    from .billing_service import BillingService
    BillingService(db).record_usage(organization_id, "ai_calls", 1)


@app.post("/api/ai/plan")
def ai_plan(data:AIRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if not AI_PLAN_RATE_LIMITER.allow(user.id):
        raise HTTPException(429, "Too many Copilot requests. Wait a moment and try again.")
    _enforce_ai_usage_limit(db, user.organization_id)
    if data.project_id:
        ensure_project(db, user, data.project_id)
    try:
        with audit_context("copilot", user.id, user.id):
            result = CopilotService().plan(data.message, WorkspaceTools(db, user, scope_project_id=data.project_id))
            audit(db,user.organization_id,user.id,"copilot",user.id,"planned")
        _record_ai_usage(db, user.organization_id)
        db.commit()
        return result
    except CopilotProviderError as error:
        raise HTTPException(503, "The configured AI provider is unavailable. Try again later.") from error
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

# Confirmation tokens are meant to be used once. This in-memory set (bounded by the
# token's own 10-minute expiry, pruned on every call) stops the same proposal from being
# replayed twice by a network retry or a malicious actor who intercepts the token.
_CONSUMED_CONFIRMATIONS: dict[str, float] = {}
_CONFIRMATION_TTL_SECONDS = 600

def _consume_confirmation_once(token: str) -> bool:
    now = time.monotonic()
    for expired in [t for t, expires_at in _CONSUMED_CONFIRMATIONS.items() if expires_at < now]:
        _CONSUMED_CONFIRMATIONS.pop(expired, None)
    if token in _CONSUMED_CONFIRMATIONS:
        return False
    _CONSUMED_CONFIRMATIONS[token] = now + _CONFIRMATION_TTL_SECONDS
    return True

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
    if proposer_agent_id and not execution_allowed(db, db.get(User, proposer_agent_id)):
        raise HTTPException(403, "Agent execution is disabled")
    handler = WRITE_TOOL_HANDLERS.get(tool)
    if not handler:
        raise HTTPException(400, "Unsupported action")
    if user.role not in TOOL_ROLES.get(tool, frozenset()):
        raise HTTPException(403, "Your role cannot perform this Copilot action.")
    if not _consume_confirmation_once(data.confirmation_token):
        raise HTTPException(409, "This Copilot proposal has already been used. Ask again for a new proposal.")
    before = _audit_before_state(tool, args, db)
    try:
        with audit_context("agent" if proposer_agent_id else "copilot", proposer_agent_id or user.id, user.id):
            result = handler(args, user, db)
    except (TypeError, KeyError, ValidationError) as error:
        raise HTTPException(422, "This Copilot proposal is missing required details.") from error
    with audit_context("agent" if proposer_agent_id else "copilot", proposer_agent_id or user.id, user.id):
        log(db, user.organization_id, user.id, "copilot", user.id, "confirmed",
            tool=tool, args=jsonable_encoder(args), before=jsonable_encoder(before),
            after=jsonable_encoder(result) if isinstance(result, dict) else None,
            proposed_by_agent_id=proposer_agent_id)
    db.commit()
    return result
