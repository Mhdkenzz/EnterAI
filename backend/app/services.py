from datetime import datetime, timedelta
import json
import re
import html
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import hash_password
from .models import Activity, HierarchyConfig, Notification, Organization, Project, Task, Team, User

def log(db: Session, org_id: str, actor_id: str | None, entity_type: str, entity_id: str, action: str, **detail):
    db.add(Activity(organization_id=org_id, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, action=action, detail=detail))

def ensure_hierarchy_config(db: Session, organization_id: str) -> HierarchyConfig:
    """Every organization gets exactly one HierarchyConfig row (all-zero until an
    admin sets counts in Phase 4). Idempotent so it's safe to call from seed() and
    from registration alike."""
    config = db.scalar(select(HierarchyConfig).where(HierarchyConfig.organization_id == organization_id))
    if not config:
        config = HierarchyConfig(organization_id=organization_id)
        db.add(config)
    return config

def seed(db: Session):
    existing = db.scalar(select(Organization))
    if existing:
        legacy_seed = existing.slug != "enter-ai"
        if legacy_seed:
            existing.name, existing.slug = "Enter AI", "enter-ai"
        for user in db.scalars(select(User).where(User.organization_id == existing.id)).all():
            if legacy_seed and user.role == "admin":
                user.name, user.title, user.avatar, user.email = "Enter AI Admin", "Administrator", "EA", "admin@demo.enterai.com"
        ensure_hierarchy_config(db, existing.id)
        db.commit()
        return
    org = Organization(name="Enter AI", slug="enter-ai")
    db.add(org); db.flush()
    ensure_hierarchy_config(db, org.id)
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

class ProjectDraftProvider:
    """Dependency-free document-to-draft helper; Copilot providers live in copilot.py."""
    def project_draft(self, filename: str, content: str) -> dict:
        """Turn a user document into an editable project draft without creating data."""
        text = re.sub(r"\s+", " ", content).strip()
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), Path(filename).stem.replace("_", " "))
        name = re.sub(r"^(project|brief|proposal|plan)\s*[:\-]?\s*", "", first_line, flags=re.I)[:80] or "New project"
        code = "".join(part[0] for part in re.findall(r"[A-Za-z0-9]+", name)[:5]).upper() or "PRJ"
        summary = text[:900]
        lower = text.lower()
        priority = "high" if any(word in lower for word in ("urgent", "launch", "deadline", "risk", "critical")) else "medium"
        suggested_team = "Operations" if any(word in lower for word in ("migration", "support", "rollout", "process")) else "Product"
        candidates = [re.sub(r"^[\-*\d.\s]+", "", line).strip() for line in content.splitlines()]
        suggested_tasks = [{"title": line[:140], "priority": priority} for line in candidates if len(line) > 8][:5]
        if not suggested_tasks:
            suggested_tasks = [{"title": "Review project brief", "priority": "medium"}, {"title": "Confirm milestones and owners", "priority": "medium"}]
        return {
            "name": name.title() if name.islower() else name,
            "code": code[:16],
            "description": summary or f"Project context imported from {filename}.",
            "health": "on_track",
            "priority": priority,
            "suggested_team": suggested_team,
            "suggested_tasks": suggested_tasks,
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
    if suffix == ".doc":
        printable = content.decode("latin-1", errors="ignore")
        return " ".join(re.findall(r"[A-Za-z][A-Za-z0-9 ,.;:()/'-]{8,}", printable))
    if suffix == ".pdf":
        # Optional dependency-free fallback keeps uploads safe even without a PDF parser.
        printable = content.decode("latin-1", errors="ignore")
        return " ".join(re.findall(r"[A-Za-z][A-Za-z0-9 ,.;:()/'-]{8,}", printable))
    if suffix == ".xlsx":
        with ZipFile(BytesIO(content)) as archive:
            shared = ""
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_xml = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                values = []
                for item in re.findall(r"<si>(.*?)</si>", shared_xml, flags=re.S):
                    values.append(html.unescape(re.sub(r"<[^>]+>", " ", item)).strip())
                shared = "\n".join(values)
            sheets = []
            for name in sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
                xml = archive.read(name).decode("utf-8", errors="replace")
                sheets.append(html.unescape(re.sub(r"<[^>]+>", " ", xml)))
            return "\n".join(x for x in [shared, *sheets] if x).strip()
    raise ValueError("Supported documents are PDF, DOC, DOCX, TXT, MD, XLSX, CSV, and JSON.")
