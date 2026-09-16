from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import hash_password
from .models import Activity, Notification, Organization, Project, Task, Team, User

def log(db: Session, org_id: str, actor_id: str | None, entity_type: str, entity_id: str, action: str, **detail):
    db.add(Activity(organization_id=org_id, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, action=action, detail=detail))

def seed(db: Session):
    if db.scalar(select(func.count()).select_from(Organization)): return
    org = Organization(name="Demo Workspace", slug="demo-workspace")
    db.add(org); db.flush()
    users = [
        User(organization_id=org.id, name="Workspace Admin", email="admin@demo.enterai.local", password_hash=hash_password("enterai-demo"), role="admin", title="Product Lead", avatar="WA"),
        User(organization_id=org.id, name="Engineering Lead", email="engineering@demo.enterai.local", password_hash=hash_password("enterai-demo"), role="manager", title="Engineering Lead", avatar="EL"),
        User(organization_id=org.id, name="Product Designer", email="design@demo.enterai.local", password_hash=hash_password("enterai-demo"), role="member", title="Product Designer", avatar="PD"),
    ]
    db.add_all(users); db.flush()
    product, platform = Team(organization_id=org.id, name="Product", description="Customer outcomes and design"), Team(organization_id=org.id, name="Platform", description="Infrastructure and intelligence")
    db.add_all([product, platform]); db.flush()
    projects = [
      Project(organization_id=org.id, team_id=product.id, name="Atlas Launch", code="ATL", description="Launch the unified customer workspace.", status="active", health="at_risk", owner_id=users[0].id, color="#8b5cf6", due_date=datetime.utcnow()+timedelta(days=19)),
      Project(organization_id=org.id, team_id=platform.id, name="AI Control Plane", code="AICP", description="Safe and observable AI actions across the product.", status="active", health="on_track", owner_id=users[1].id, color="#14b8a6", due_date=datetime.utcnow()+timedelta(days=41)),
      Project(organization_id=org.id, team_id=product.id, name="Mobile Experience", code="MOB", description="Core workflows for the field team.", status="planning", health="on_track", owner_id=users[2].id, color="#f59e0b", due_date=datetime.utcnow()+timedelta(days=63)),
    ]
    db.add_all(projects); db.flush()
    task_data = [
      (projects[0], "Finalize launch narrative", "in_progress", "high", users[0], 2), (projects[0], "Complete accessibility QA", "todo", "high", users[2], 5), (projects[0], "Prepare customer migration", "review", "medium", users[1], 8),
      (projects[1], "Design tool confirmation policy", "done", "high", users[1], -1), (projects[1], "Build provider abstraction", "in_progress", "high", users[1], 4), (projects[1], "Add audit-event export", "todo", "medium", users[0], 15),
      (projects[2], "Validate offline mode", "todo", "medium", users[2], 20), (projects[2], "Map field workflows", "backlog", "low", users[2], 28),
    ]
    for pos, (project, title, status, priority, assignee, days) in enumerate(task_data):
        db.add(Task(project_id=project.id, title=title, status=status, priority=priority, assignee_id=assignee.id, reporter_id=users[0].id, due_date=datetime.utcnow()+timedelta(days=days), position=pos, labels=["launch"] if project == projects[0] else ["ai"]))
    db.add_all([
      Notification(user_id=users[0].id, title="Atlas Launch needs attention", body="Two high priority tasks are due this week.", href="/projects/atlas-launch"),
      Notification(user_id=users[0].id, title="A task was completed", body="Design tool confirmation policy is complete.", href="/projects/ai-control-plane"),
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
