from datetime import datetime, timedelta
import json
import re
from pathlib import Path
from zipfile import ZipFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import hash_password
from .models import Activity, Notification, Organization, Project, Task, Team, User

def log(db: Session, org_id: str, actor_id: str | None, entity_type: str, entity_id: str, action: str, **detail):
    db.add(Activity(organization_id=org_id, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, action=action, detail=detail))

def seed(db: Session):
    existing = db.scalar(select(Organization))
    if existing:
        if existing.name in {"Demo Workspace", "North Star Labs"}:
            existing.name = "Enter AI"
        for user in db.scalars(select(User).where(User.organization_id == existing.id)).all():
            if user.name in {"Alex Morgan", "Workspace Admin", "Engineering Lead", "Product Designer"}:
                user.name, user.title, user.avatar = "Enter AI Admin", "Administrator", "EA"
            if user.email == "admin@demo.enterai.local":
                user.email = "admin@demo.enterai.com"
        db.commit()
        return
    org = Organization(name="Enter AI", slug="enter-ai")
    db.add(org); db.flush()
    users = [
        User(organization_id=org.id, name="Enter AI Admin", email="admin@demo.enterai.com", password_hash=hash_password("enterai-demo"), role="admin", title="Administrator", avatar="EA"),
    ]
    db.add_all(users); db.flush()
    product, platform = Team(organization_id=org.id, name="Product", description="Product direction and delivery"), Team(organization_id=org.id, name="Operations", description="Planning and delivery support")
    db.add_all([product, platform]); db.flush()
    projects = [
      Project(organization_id=org.id, team_id=product.id, name="Product Launch", code="LAUNCH", description="Prepare the next product release.", status="active", health="at_risk", owner_id=users[0].id, color="#22c55e", due_date=datetime.utcnow()+timedelta(days=19)),
      Project(organization_id=org.id, team_id=platform.id, name="AI Workspace", code="AIW", description="Safe and observable AI actions across the product.", status="active", health="on_track", owner_id=users[0].id, color="#38bdf8", due_date=datetime.utcnow()+timedelta(days=41)),
      Project(organization_id=org.id, team_id=product.id, name="Mobile Experience", code="MOB", description="Core workflows for the field team.", status="planning", health="on_track", owner_id=users[0].id, color="#f59e0b", due_date=datetime.utcnow()+timedelta(days=63)),
    ]
    db.add_all(projects); db.flush()
    task_data = [
      (projects[0], "Finalize launch narrative", "in_progress", "high", users[0], 2), (projects[0], "Complete accessibility QA", "todo", "high", users[0], 5), (projects[0], "Prepare customer migration", "review", "medium", users[0], 8),
      (projects[1], "Design tool confirmation policy", "done", "high", users[0], -1), (projects[1], "Build provider abstraction", "in_progress", "high", users[0], 4), (projects[1], "Add audit-event export", "todo", "medium", users[0], 15),
      (projects[2], "Validate offline mode", "todo", "medium", users[0], 20), (projects[2], "Map field workflows", "backlog", "low", users[0], 28),
    ]
    for pos, (project, title, status, priority, assignee, days) in enumerate(task_data):
        db.add(Task(project_id=project.id, title=title, status=status, priority=priority, assignee_id=assignee.id, reporter_id=users[0].id, due_date=datetime.utcnow()+timedelta(days=days), position=pos, labels=["launch"] if project == projects[0] else ["ai"]))
    db.add_all([
      Notification(user_id=users[0].id, title="Product Launch needs attention", body="Two high priority tasks are due this week.", href=f"project:{projects[0].id}"),
      Notification(user_id=users[0].id, title="A task was completed", body="Design tool confirmation policy is complete.", href=f"project:{projects[1].id}"),
    ])
    log(db, org.id, users[0].id, "project", projects[0].id, "created", name=projects[0].name)
    db.commit()

class AIProvider:
    """Provider boundary: replace DeterministicProvider with OpenAI/Anthropic/local implementation."""
    def plan(self, message: str, projects: list[Project]) -> dict:
        target = next((p for p in projects if p.name.lower() in message.lower()), projects[0] if projects else None)
        if any(x in message.lower() for x in ["create", "add", "make a task"]):
            title = message.replace("create", "").replace("add", "").strip().capitalize()[:140] or "New task"
            return {"reply": f"I can create **{title}** in {target.name if target else 'your workspace'}. Review it before I write anything.", "actions": [{"tool":"create_task", "label":f"Create task: {title}", "args":{"project_id":target.id if target else None,"title":title,"priority":"medium"}, "requires_confirmation":True}]}
        risks = [p.name for p in projects if p.health == "at_risk"]
        return {"reply": f"I found {len(risks)} project risk{'s' if len(risks)!=1 else ''}: {', '.join(risks) or 'none'}. Ask me to create a task, update work, or dig into a project.", "actions": []}

    def project_draft(self, filename: str, content: str) -> dict:
        """Turn a user document into an editable project draft without creating data."""
        text = re.sub(r"\s+", " ", content).strip()
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), Path(filename).stem.replace("_", " "))
        name = re.sub(r"^(project|brief|proposal|plan)\s*[:\-]?\s*", "", first_line, flags=re.I)[:80] or "New project"
        code = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", name)[:5]).upper() or "PRJ"
        summary = text[:900]
        return {
            "name": name.title() if name.islower() else name,
            "code": code[:16],
            "description": summary or f"Project context imported from {filename}.",
            "health": "on_track",
            "summary": f"Read {filename}. Review the editable project details before creating it.",
        }


def extract_document_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return content.decode("utf-8", errors="replace")
    if suffix == ".docx":
        with ZipFile(__import__("io").BytesIO(content)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml)).strip()
    if suffix == ".pdf":
        # Optional dependency-free fallback keeps uploads safe even without a PDF parser.
        printable = content.decode("latin-1", errors="ignore")
        return " ".join(re.findall(r"[A-Za-z][A-Za-z0-9 ,.;:()/'-]{8,}", printable))
    raise ValueError("Supported documents are PDF, DOCX, TXT, MD, CSV, and JSON.")
