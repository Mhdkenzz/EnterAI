"""Shared authorization tests on a disposable local database."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.database import Base
from app.main import WRITE_TOOL_HANDLERS, TaskUpdate, task_accountability, update_task
from app.models import AuditEvent, Organization, User, Project, Task


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


def test_assigning_additional_work_does_not_preempt_an_active_agent_task(workspace):
    db, people, agent, project, task = workspace
    task.assignee_id = agent.id
    task.status = "in_progress"
    agent.current_task_id = task.id
    queued = Task(project_id=project.id, title="Queued task")
    db.add(queued)
    db.commit()
    WRITE_TOOL_HANDLERS["update_task"]({"task_id": queued.id, "assignee_id": agent.id}, people["admin"], db)
    db.refresh(agent)
    db.refresh(queued)
    assert queued.assignee_id == agent.id
    assert agent.current_task_id == task.id


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


def test_admin_cannot_remove_the_only_task_owner(workspace):
    db, people, agent, _, task = workspace
    task.assignee_id = agent.id
    agent.current_task_id = task.id
    db.commit()
    with pytest.raises(HTTPException) as exc:
        WRITE_TOOL_HANDLERS["update_task"](
            {"task_id": task.id, "assignee_id": None}, people["admin"], db)
    assert exc.value.status_code == 422
    db.refresh(task); db.refresh(agent)
    assert task.assignee_id == agent.id
    assert agent.current_task_id == task.id


def test_agent_cannot_start_a_second_task_while_one_is_active(workspace):
    db, people, agent, project, task = workspace
    task.assignee_id = agent.id
    task.status = "in_progress"
    active = Task(project_id=project.id, title="Active task", assignee_id=agent.id, status="in_progress")
    queued = Task(project_id=project.id, title="Queued task", assignee_id=agent.id, status="todo")
    db.add_all([active, queued]); db.commit()
    with pytest.raises(HTTPException) as exc:
        WRITE_TOOL_HANDLERS["update_task"](
            {"task_id": queued.id, "status": "in_progress"}, people["admin"], db)
    assert exc.value.status_code == 409
    db.refresh(queued)
    assert queued.status == "todo"


def test_active_agent_task_cannot_be_reassigned_mid_execution(workspace):
    db, people, agent, _, task = workspace
    task.assignee_id = agent.id
    task.status = "in_progress"
    agent.current_task_id = task.id
    db.commit()
    with pytest.raises(HTTPException) as exc:
        WRITE_TOOL_HANDLERS["update_task"](
            {"task_id": task.id, "assignee_id": people["member"].id}, people["admin"], db)
    assert exc.value.status_code == 409
    db.refresh(task)
    assert task.assignee_id == agent.id


def test_repeated_agent_assignment_is_idempotent(workspace):
    db, people, agent, _, task = workspace
    WRITE_TOOL_HANDLERS["delegate_task"](
        {"task_id": task.id, "agent_id": agent.id}, people["admin"], db)
    before = len(db.scalars(select(AuditEvent).where(
        AuditEvent.entity_type == "task", AuditEvent.entity_id == task.id,
    )).all())
    WRITE_TOOL_HANDLERS["delegate_task"](
        {"task_id": task.id, "agent_id": agent.id}, people["admin"], db)
    after = len(db.scalars(select(AuditEvent).where(
        AuditEvent.entity_type == "task", AuditEvent.entity_id == task.id,
    )).all())
    assert after == before


def test_task_accountability_tracks_requester_owner_assignment_execution_and_review(workspace):
    db, people, _, _, task = workspace
    requester, owner = people["member"], people["manager"]
    task.reporter_id = requester.id
    task.assignee_id = requester.id
    db.commit()
    update_task(task.id, TaskUpdate(assignee_id=owner.id), people["admin"], db)
    update_task(task.id, TaskUpdate(status="in_progress"), owner, db)
    update_task(task.id, TaskUpdate(status="review"), owner, db)

    result = task_accountability(task.id, people["admin"], db)
    assert result["requested_by"]["id"] == requester.id
    assert result["owner"]["id"] == owner.id
    assert result["assigned_by"]["id"] == people["admin"].id
    assert result["executed_by"]["id"] == owner.id
    assert result["reviewed_by"]["id"] == owner.id
    assert result["approved_by"] is None
    assert {event["action"] for event in result["events"]} >= {
        "task_reassigned", "task_started", "task_reviewed",
    }


def test_worker_accountability_is_limited_to_its_assigned_task(workspace):
    db, people, _, project, task = workspace
    worker = User(organization_id=people["admin"].organization_id, name="Worker", email="worker@local.test",
                  password_hash="unused", role="member", kind="agent", hierarchy_level="worker", active=True)
    db.add(worker)
    db.flush()
    task.assignee_id = worker.id
    other = Task(project_id=project.id, title="Private to another owner", assignee_id=people["member"].id)
    db.add(other)
    db.commit()
    assert task_accountability(task.id, worker, db)["owner"]["id"] == worker.id
    with pytest.raises(HTTPException) as exc:
        task_accountability(other.id, worker, db)
    assert exc.value.status_code == 404
