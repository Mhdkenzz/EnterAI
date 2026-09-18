"""Deleting a task has to resolve every row that points at it.

SQLite never enforced these foreign keys, so the endpoint appeared to work while
silently orphaning comments and attachments; PostgreSQL rejects the DELETE
outright. These tests pin the intended semantics on whichever backend runs them.
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Attachment, Comment, Task, User

from test_observability import identity, workspace


def test_deleting_a_task_removes_owned_rows_and_frees_its_references():
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        project = c.post('/api/projects', headers=h, json={'name': 'Del', 'code': 'DEL'}).json()
        parent = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'Parent'}).json()
        child = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'Child', 'parent_id': parent['id']}).json()
        c.post(f"/api/tasks/{parent['id']}/comments", headers=h, json={'body': 'a comment'})
        c.post(f"/api/tasks/{parent['id']}/attachments", headers=h, files={'file': ('note.txt', b'bytes', 'text/plain')})
        assign = c.post(f"/api/tasks/{parent['id']}/assign-agent", headers=h, json={'agent_id': agent_id})
        assert assign.status_code == 200

        with SessionLocal() as db:
            assert db.get(User, agent_id).current_task_id == parent['id']

        assert c.delete(f"/api/tasks/{parent['id']}", headers=h).status_code == 204

        with SessionLocal() as db:
            assert db.get(Task, parent['id']) is None
            # Owned rows go with the task.
            assert db.scalar(select(Comment).where(Comment.task_id == parent['id'])) is None
            assert db.scalar(select(Attachment).where(Attachment.task_id == parent['id'])) is None
            # A subtask is promoted, not destroyed along with its parent.
            surviving = db.get(Task, child['id'])
            assert surviving is not None and surviving.parent_id is None
            # The agent outlives the task it was working on.
            agent = db.get(User, agent_id)
            assert agent is not None and agent.current_task_id is None


def test_deleting_a_completed_task_clears_the_agents_last_completed_pointer():
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        project = c.post('/api/projects', headers=h, json={'name': 'Done', 'code': 'DONE'}).json()
        task = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'Finished'}).json()
        c.post(f"/api/tasks/{task['id']}/assign-agent", headers=h, json={'agent_id': agent_id})
        assert c.patch(f"/api/tasks/{task['id']}", headers=h, json={'status': 'done'}).status_code == 200
        with SessionLocal() as db:
            assert db.get(User, agent_id).last_completed_task_id == task['id']

        assert c.delete(f"/api/tasks/{task['id']}", headers=h).status_code == 204

        with SessionLocal() as db:
            agent = db.get(User, agent_id)
            assert agent is not None and agent.last_completed_task_id is None
