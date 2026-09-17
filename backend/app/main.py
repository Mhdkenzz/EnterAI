import os
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from .auth import create_token, current_user, hash_password, verify_password
from .database import Base, engine, get_db
from .models import Activity, Attachment, Comment, HierarchyConfig, Notification, Organization, Project, ProjectDocument, Task, Team, User
from .copilot import CopilotProviderError, CopilotService, TOOL_ROLES, WorkspaceTools, read_confirmation
from .ratelimit import SlidingWindowRateLimiter
from .services import ProjectDraftProvider, ensure_hierarchy_config, extract_document_text, log, seed
from .storage import get_storage

environment = os.getenv("ENVIRONMENT", "development").strip().lower()
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


app.openapi = custom_openapi
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:6767",
    "http://127.0.0.1:6767",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    with next(get_db()) as db: seed(db)

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

def user_out(user: User): return {"id":user.id,"name":user.name,"email":user.email,"role":user.role,"title":user.title,"avatar":user.avatar}
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

@app.get("/health")
def health(): return {"ok":True}

@app.get("/")
def root(): return {"service": "Enter AI API", "status": "ok"}

@app.post("/api/auth/register")
def register(data: Register, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email==data.email)): raise HTTPException(409,"Email already exists")
    slug = data.organization_name.lower().replace(" ","-")[:70]
    if db.scalar(select(Organization).where(Organization.slug == slug)): raise HTTPException(409,"Organization already exists")
    org = Organization(name=data.organization_name, slug=slug); db.add(org); db.flush()
    user = User(organization_id=org.id,name=data.name,email=data.email,password_hash=hash_password(data.password),role="admin",avatar="".join(x[0] for x in data.name.split())[:2].upper()); db.add(user)
    ensure_hierarchy_config(db, org.id); db.commit()
    return {"token":create_token(user),"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.post("/api/auth/login")
def login(data: SignIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email==data.email))
    if not user or not verify_password(data.password,user.password_hash): raise HTTPException(401,"Incorrect email or password")
    org=db.get(Organization,user.organization_id); return {"token":create_token(user),"user":user_out(user),"organization":{"id":org.id,"name":org.name}}

@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)): return {"user":user_out(user),"organization":{"id":user.organization_id,"name":db.get(Organization,user.organization_id).name}}

@app.get("/api/dashboard")
def dashboard(user: User = Depends(current_user), db: Session = Depends(get_db)):
    projects=db.scalars(select(Project).where(Project.organization_id==user.organization_id).order_by(Project.created_at.desc())).all()
    assigned=db.scalars(select(Task).join(Project).where(Project.organization_id==user.organization_id, Task.assignee_id==user.id).order_by(Task.due_date)).all()
    tasks=sorted(assigned,key=lambda task: (task.status=="done", task.due_date or datetime.max))
    now = datetime.utcnow(); week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    return {"projects":[project_out(p,db) for p in projects],"my_tasks":[task_out(t,db) for t in tasks],"stats":{"active_projects":len([p for p in projects if p.status=="active"]),"at_risk":len([p for p in projects if p.health=="at_risk"]),"my_open_tasks":len([task for task in assigned if task.status!="done"]),"completed_this_week":len([task for task in assigned if task.completed_at and task.completed_at >= week_start])}}

@app.get("/api/projects")
def projects(user: User = Depends(current_user), db: Session = Depends(get_db)): return [project_out(p,db) for p in db.scalars(select(Project).where(Project.organization_id==user.organization_id)).all()]
@app.post("/api/projects", status_code=201)
def create_project(data: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
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
    if "team_id" in values and values["team_id"]:
        team = db.get(Team, values["team_id"])
        if not team or team.organization_id != user.organization_id: raise HTTPException(422, "Team not found")
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
    if len(raw) > 5 * 1024 * 1024: raise HTTPException(413,"Documents must be 5 MB or smaller")
    try: extracted=extract_document_text(file.filename or "document.txt",raw)
    except ValueError as error: raise HTTPException(415,str(error)) from error
    if not extracted.strip(): raise HTTPException(422,"No readable text was found in this document")
    stored = get_storage().save("project-documents", file.filename or "document", raw)
    document=ProjectDocument(organization_id=user.organization_id,uploaded_by=user.id,file_name=file.filename or "document",path=str(stored),content_type=file.content_type,extracted_text=extracted[:50000]); db.add(document); db.flush()
    draft=ProjectDraftProvider().project_draft(document.file_name,document.extracted_text); db.commit()
    return {"document":{"id":document.id,"file_name":document.file_name},"draft":draft}

@app.get("/api/projects/{project_id}/documents")
def project_documents(project_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_project(db,user,project_id)
    return [{"id":d.id,"file_name":d.file_name,"content_type":d.content_type,"created_at":d.created_at} for d in db.scalars(select(ProjectDocument).where(ProjectDocument.project_id==project_id)).all()]

@app.post("/api/projects/{project_id}/documents",status_code=201)
async def add_project_document(project_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    ensure_project(db,user,project_id)
    raw=await file.read()
    if len(raw) > 5 * 1024 * 1024: raise HTTPException(413,"Documents must be 5 MB or smaller")
    try: extracted=extract_document_text(file.filename or "document.txt",raw)
    except ValueError as error: raise HTTPException(415,str(error)) from error
    stored = get_storage().save("project-documents", file.filename or "document", raw)
    document=ProjectDocument(organization_id=user.organization_id,project_id=project_id,uploaded_by=user.id,file_name=file.filename or "document",path=str(stored),content_type=file.content_type,extracted_text=extracted[:50000]); db.add(document); log(db,user.organization_id,user.id,"project",project_id,"document_uploaded",file_name=document.file_name); db.commit()
    return {"id":document.id,"file_name":document.file_name}

@app.post("/api/tasks",status_code=201)
def create_task(data: TaskIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    p=ensure_project(db,user,data.project_id); t=Task(**data.model_dump(),reporter_id=user.id); db.add(t); db.flush(); log(db,user.organization_id,user.id,"task",t.id,"created",title=t.title); db.commit(); return task_out(t,db)
@app.patch("/api/tasks/{task_id}")
def update_task(task_id:str,data:TaskUpdate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id); ensure_project(db,user,t.project_id) if t else (_ for _ in ()).throw(HTTPException(404,"Task not found"))
    before=t.status
    for key,value in data.model_dump(exclude_unset=True).items(): setattr(t,key,value)
    if "status" in data.model_fields_set and data.status != before:
        t.completed_at = datetime.utcnow() if data.status == "done" else None
    log(db,user.organization_id,user.id,"task",t.id,"updated",from_status=before,to_status=t.status); db.commit(); return task_out(t,db)
@app.delete("/api/tasks/{task_id}",status_code=204)
def delete_task(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id)
    if not t: raise HTTPException(404,"Task not found")
    ensure_project(db,user,t.project_id); db.delete(t); db.commit()

@app.get("/api/tasks/{task_id}/comments")
def comments(task_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id); ensure_project(db,user,t.project_id) if t else (_ for _ in ()).throw(HTTPException(404,"Task not found"))
    return [{"id":c.id,"body":c.body,"created_at":c.created_at,"author":user_out(db.get(User,c.author_id))} for c in db.scalars(select(Comment).where(Comment.task_id==task_id)).all()]
@app.post("/api/tasks/{task_id}/comments",status_code=201)
def add_comment(task_id:str,data:CommentIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id); ensure_project(db,user,t.project_id) if t else (_ for _ in ()).throw(HTTPException(404,"Task not found")); c=Comment(task_id=task_id,author_id=user.id,body=data.body); db.add(c); log(db,user.organization_id,user.id,"task",task_id,"commented"); db.commit(); return {"id":c.id,"body":c.body}
@app.post("/api/tasks/{task_id}/attachments",status_code=201)
async def add_attachment(task_id:str,file:UploadFile=File(...),user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=db.get(Task,task_id); ensure_project(db,user,t.project_id) if t else (_ for _ in ()).throw(HTTPException(404,"Task not found")); path=get_storage().save("task-attachments", file.filename or "attachment", await file.read()); a=Attachment(task_id=task_id,uploaded_by=user.id,file_name=file.filename or "attachment",path=path,content_type=file.content_type); db.add(a); db.commit(); return {"id":a.id,"file_name":a.file_name}

@app.get("/api/teams")
def teams(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":t.id,"name":t.name,"description":t.description,"projects":db.query(Project).filter(Project.team_id==t.id).count()} for t in db.scalars(select(Team).where(Team.organization_id==user.organization_id)).all()]
@app.get("/api/teams/{team_id}/projects")
def team_projects(team_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    team=db.get(Team,team_id)
    if not team or team.organization_id!=user.organization_id: raise HTTPException(404,"Team not found")
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.team_id==team_id)).all()]
@app.post("/api/teams",status_code=201)
def create_team(data:TeamIn,user:User=Depends(current_user),db:Session=Depends(get_db)):
    t=Team(organization_id=user.organization_id,**data.model_dump()); db.add(t); db.commit(); return {"id":t.id,"name":t.name,"description":t.description}
@app.get("/api/users")
def users(user:User=Depends(current_user),db:Session=Depends(get_db)): return [user_out(u) for u in db.scalars(select(User).where(User.organization_id==user.organization_id,User.kind=="human")).all()]

def agent_out(a: User, db: Session) -> dict:
    current = db.get(Task, a.current_task_id) if a.current_task_id else None
    last_completed = db.get(Task, a.last_completed_task_id) if a.last_completed_task_id else None
    return {
        "id": a.id, "name": a.name, "title": a.title, "avatar": a.avatar,
        "hierarchy_level": a.hierarchy_level, "parent_agent_id": a.parent_agent_id,
        "current_task": task_out(current, db) if current else None,
        "last_completed_task": task_out(last_completed, db) if last_completed else None,
    }

@app.get("/api/agents")
def agents(user:User=Depends(current_user),db:Session=Depends(get_db)):
    rows = db.scalars(select(User).where(User.organization_id==user.organization_id,User.kind=="agent")).all()
    return [agent_out(a, db) for a in rows]

@app.get("/api/hierarchy-config")
def hierarchy_config(user:User=Depends(current_user),db:Session=Depends(get_db)):
    cfg = ensure_hierarchy_config(db, user.organization_id); db.commit()
    return {"vp_count":cfg.vp_count,"directors_per_vp":cfg.directors_per_vp,"managers_per_director":cfg.managers_per_director,"workers_per_manager":cfg.workers_per_manager}
@app.get("/api/notifications")
def notifications(user:User=Depends(current_user),db:Session=Depends(get_db)): return [{"id":n.id,"title":n.title,"body":n.body,"href":n.href,"read":n.read,"created_at":n.created_at} for n in db.scalars(select(Notification).where(Notification.user_id==user.id).order_by(Notification.created_at.desc())).all()]
@app.patch("/api/notifications/{notification_id}/read")
def read_notification(notification_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    n=db.get(Notification,notification_id)
    if not n or n.user_id!=user.id: raise HTTPException(404,"Notification not found")
    n.read=True; db.commit(); return {"ok":True}
@app.get("/api/activity")
def activity(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [{"id":a.id,"action":a.action,"entity_type":a.entity_type,"detail":a.detail,"created_at":a.created_at,"actor":user_out(db.get(User,a.actor_id)) if a.actor_id else None} for a in db.scalars(select(Activity).where(Activity.organization_id==user.organization_id).order_by(Activity.created_at.desc()).limit(50)).all()]
@app.get("/api/search")
def search(q:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    term=f"%{q}%"; ps=db.scalars(select(Project).where(Project.organization_id==user.organization_id,or_(Project.name.ilike(term),Project.code.ilike(term)))).all(); ts=db.scalars(select(Task).join(Project).where(Project.organization_id==user.organization_id,Task.title.ilike(term))).all(); return {"projects":[project_out(p,db) for p in ps],"tasks":[task_out(t,db) for t in ts]}
@app.get("/api/risks")
def risks(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [project_out(p,db) for p in db.scalars(select(Project).where(Project.organization_id==user.organization_id,Project.health=="at_risk")).all()]
AI_PLAN_RATE_LIMITER = SlidingWindowRateLimiter(
    max_calls=int(os.getenv("AI_PLAN_RATE_LIMIT_PER_MINUTE", "20")), window_seconds=60,
)

@app.post("/api/ai/plan")
def ai_plan(data:AIRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if not AI_PLAN_RATE_LIMITER.allow(user.id):
        raise HTTPException(429, "Too many Copilot requests. Wait a moment and try again.")
    if data.project_id:
        ensure_project(db, user, data.project_id)
    try:
        return CopilotService().plan(data.message, WorkspaceTools(db, user, scope_project_id=data.project_id))
    except CopilotProviderError as error:
        raise HTTPException(503, str(error)) from error
def _without(args: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in args.items() if key not in keys}

WRITE_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], User, Session], Any]] = {
    "create_task": lambda args, user, db: create_task(TaskIn(**args), user, db),
    "update_task": lambda args, user, db: update_task(args["task_id"], TaskUpdate(**_without(args, "task_id")), user, db),
    "create_project": lambda args, user, db: create_project(ProjectIn(**args), user, db),
    "update_project": lambda args, user, db: update_project(args["project_id"], ProjectUpdate(**_without(args, "project_id")), user, db),
    "add_comment": lambda args, user, db: add_comment(args["task_id"], CommentIn(**_without(args, "task_id")), user, db),
    "create_team": lambda args, user, db: create_team(TeamIn(**args), user, db),
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
    try:
        tool, args = read_confirmation(data.confirmation_token, user)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    handler = WRITE_TOOL_HANDLERS.get(tool)
    if not handler:
        raise HTTPException(400, "Unsupported action")
    if user.role not in TOOL_ROLES.get(tool, frozenset()):
        raise HTTPException(403, "Your role cannot perform this Copilot action.")
    if not _consume_confirmation_once(data.confirmation_token):
        raise HTTPException(409, "This Copilot proposal has already been used. Ask again for a new proposal.")
    before = _audit_before_state(tool, args, db)
    try:
        result = handler(args, user, db)
    except (TypeError, KeyError, ValidationError) as error:
        raise HTTPException(422, "This Copilot proposal is missing required details.") from error
    log(db, user.organization_id, user.id, "copilot", user.id, "confirmed",
        tool=tool, args=jsonable_encoder(args), before=jsonable_encoder(before),
        after=jsonable_encoder(result) if isinstance(result, dict) else None)
    db.commit()
    return result
