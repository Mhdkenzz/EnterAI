"""Shared authorization tests on a disposable local database."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.main import WRITE_TOOL_HANDLERS
from app.models import Organization, User, Project, Task


@pytest.fixture
def workspace(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'assignment.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        org = Organization(name="Local", slug="assignment-local")
        db.add(org); db.flush()
        people = [User(organization_id=org.id, name=role, email=f"{role}@local.test",
                       password_hash="unused", role=role, kind="human", active=True)
                  for role in ("admin", "manager", "member", "viewer")]
        agent = User(organization_id=org.id, name="Agent", email="agent@local.test",
                     password_hash="unused", role="admin", kind="agent", active=True)
        db.add_all([*people, agent]); db.flush()
        project = Project(organization_id=org.id, name="Local project", code="LOCAL")
        db.add(project); db.flush()
        task = Task(project_id=project.id, title="Local task")
        db.add(task); db.commit()
        yield db, {u.role: u for u in people}, agent, project, task
    engine.dispose()


@pytest.mark.parametrize("role", ["manager", "member", "viewer"])
@pytest.mark.parametrize("tool", ["create_task", "update_task", "delegate_task"])
def test_shared_handlers_reject_nonadmin_agent_assignment(workspace, role, tool):
    db, people, agent, project, task = workspace
    args = {"create_task": {"project_id": project.id, "title": "Denied", "assignee_id": agent.id},
            "update_task": {"task_id": task.id, "assignee_id": agent.id},
            "delegate_task": {"task_id": task.id, "agent_id": agent.id}}[tool]
    with pytest.raises(HTTPException) as exc:
        WRITE_TOOL_HANDLERS[tool](args, people[role], db)
    assert exc.value.status_code == 403
    db.refresh(task); db.refresh(agent)
    assert task.assignee_id is None
    assert agent.current_task_id is None
    assert len(db.scalars(select(Task)).all()) == 1


@pytest.mark.parametrize("replacement", ["clear", "human"])
def test_nonadmin_cannot_remove_agent_assignment(workspace, replacement):
    db, people, agent, _, task = workspace
    task.assignee_id = agent.id
    agent.current_task_id = task.id
    db.commit()
    target = None if replacement == "clear" else people["member"].id
    with pytest.raises(HTTPException) as exc:
        WRITE_TOOL_HANDLERS["update_task"](
            {"task_id": task.id, "assignee_id": target}, people["member"], db)
    assert exc.value.status_code == 403
    db.refresh(task); db.refresh(agent)
    assert task.assignee_id == agent.id
    assert agent.current_task_id == task.id


@pytest.mark.parametrize("tool", ["create_task", "update_task", "delegate_task"])
def test_human_admin_can_assign_agent(workspace, tool):
    db, people, agent, project, task = workspace
    args = {"create_task": {"project_id": project.id, "title": "Allowed", "assignee_id": agent.id},
            "update_task": {"task_id": task.id, "assignee_id": agent.id},
            "delegate_task": {"task_id": task.id, "agent_id": agent.id}}[tool]
    result = WRITE_TOOL_HANDLERS[tool](args, people["admin"], db)
    assert result["assignee"]["id"] == agent.id


def test_member_can_assign_human_and_edit_agent_task_without_reassignment(workspace):
    db, people, agent, _, task = workspace
    result = WRITE_TOOL_HANDLERS["update_task"](
        {"task_id": task.id, "assignee_id": people["member"].id}, people["member"], db)
    assert result["assignee"]["id"] == people["member"].id
    task.assignee_id = agent.id
    agent.current_task_id = task.id
    db.commit()
    result = WRITE_TOOL_HANDLERS["update_task"](
        {"task_id": task.id, "title": "Edited without delegation"}, people["member"], db)
    assert result["title"] == "Edited without delegation"
    assert result["assignee"]["id"] == agent.id


@pytest.mark.parametrize("replacement", ["clear", "human"])
def test_admin_can_remove_agent_assignment(workspace, replacement):
    db, people, agent, _, task = workspace
    task.assignee_id = agent.id
    agent.current_task_id = task.id
    db.commit()
    target = None if replacement == "clear" else people["member"].id
    WRITE_TOOL_HANDLERS["update_task"](
        {"task_id": task.id, "assignee_id": target}, people["admin"], db)
    db.refresh(task); db.refresh(agent)
    assert task.assignee_id == target
    assert agent.current_task_id is None
