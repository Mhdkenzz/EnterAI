from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.database import SessionLocal
from app.models import User, Organization
from app.auth import create_token


def workspace(client):
    key = uuid4().hex
    response = client.post('/api/auth/register', json={'organization_name': key, 'name': 'Admin', 'email': f'{key}@example.com', 'password': 'test-password'})
    assert response.status_code == 200
    data = response.json()
    return {'Authorization': 'Bearer ' + data['token']}, data


def identity(org, role='member', kind='human'):
    with SessionLocal() as db:
        user = User(organization_id=org, name='Test', email=uuid4().hex+'@example.com', password_hash='unused', role=role, kind=kind)
        db.add(user); db.commit()
        return user.id, {'Authorization': 'Bearer '+create_token(user)}


def test_kill_switch_blocks_chat_confirmation_runtime_and_stale_inflight():
    from app.copilot import WorkspaceTools, ProviderTurn, CopilotProviderError, create_agent_confirmation
    from app.models import Task, Project, Comment
    from app.execution import run_execution_tick
    from test_agent_execution import _ScriptedProvider, _service_with
    import pytest
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        project = c.post('/api/projects', headers=h, json={'name': 'P', 'code': 'P'}).json()
        task = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'T'}).json()
        c.post(f"/api/tasks/{task['id']}/assign-agent", headers=h, json={'agent_id': agent_id})
        with SessionLocal() as db:
            agent = db.get(User, agent_id)
            task_row = db.get(Task, task['id'])
            token = create_agent_confirmation(agent, 'add_comment', {'task_id': task['id'], 'body': 'no'})
            c.patch(f'/api/admin/agents/{agent_id}/execution', headers=h, json={'execution_enabled': False})
            assert c.post(f'/api/agents/{agent_id}/messages', headers=h, json={'message': 'hello'}).status_code == 403
            assert c.post('/api/ai/confirm', headers=h, json={'confirmation_token': token}).status_code == 403
            service = _service_with(_ScriptedProvider([ProviderTurn('done', [{'id': 'a', 'name': 'mark_task_complete', 'args': {}}])]))
            with pytest.raises(CopilotProviderError):
                service.run_execution_step(WorkspaceTools(db, agent), task_row)
            assert service.provider.calls == 0
            assert run_execution_tick(db, service) == 0
            c.patch(f'/api/admin/agents/{agent_id}/execution', headers=h, json={'execution_enabled': True})
            class StopDuringCall(_ScriptedProvider):
                def respond(self, *args, **kwargs):
                    with SessionLocal() as other:
                        other.get(Organization, org).execution_enabled = False
                        other.commit()
                    return ProviderTurn('done', [{'id': 'a', 'name': 'log_progress', 'args': {'note': 'must not write'}}, {'id': 'b', 'name': 'mark_task_complete', 'args': {}}])
            with pytest.raises(CopilotProviderError):
                _service_with(StopDuringCall([])).run_execution_step(WorkspaceTools(db, agent), task_row)
            db.commit()
            assert db.get(Task, task['id']).status == 'todo'
            assert not db.scalars(select(Comment).where(Comment.task_id == task['id'])).all()
        assert c.post(f'/api/agents/{agent_id}/messages', headers=h, json={'message': 'hello'}).status_code == 403
        usage = c.get('/api/admin/usage', headers=h, params={'agent_id': agent_id}).json()
        assert usage['totals']['calls'] == 1
        assert usage['totals']['executions'] == 1
        assert usage['totals']['execution_failures'] == 1
        assert usage['agents'][0]['status'] == 'disabled'


def test_mutation_audit_coverage_attribution_and_safe_errors(monkeypatch, caplog):
    from app.models import Notification, AuditEvent, Activity
    from app.copilot import CopilotProviderError, create_confirmation
    from app.services import log
    import pytest
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        uid = data['user']['id']
        project = c.post('/api/projects', headers=h, json={'name': 'P', 'code': 'P'}).json()
        task = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'PRIVATE-TITLE'}).json()
        c.post(f"/api/tasks/{task['id']}/attachments", headers=h, files={'file': ('PRIVATE.txt', b'PRIVATE-BODY')})
        c.delete(f"/api/tasks/{task['id']}", headers=h)
        with SessionLocal() as db:
            n = Notification(user_id=uid, title='PRIVATE-NOTIFICATION'); db.add(n); db.commit(); nid=n.id
            token = create_confirmation(db.get(User, uid), 'create_team', {'name': 'PRIVATE-TEAM'})
        c.patch(f'/api/notifications/{nid}/read', headers=h)
        c.post('/api/ai/confirm', headers=h, json={'confirmation_token': token})
        agent_id, _ = identity(org, 'admin', 'agent')
        c.post(f'/api/agents/{agent_id}/messages', headers=h, json={'message': 'hello PRIVATE-PROMPT'})
        from app.copilot import DeterministicCopilotProvider
        def fail(*args): raise CopilotProviderError('PRIVATE-ERROR')
        monkeypatch.setattr(DeterministicCopilotProvider, 'answer', fail)
        response = c.post('/api/ai/plan', headers=h, json={'message': 'hello'})
        assert response.status_code == 503 and 'PRIVATE-ERROR' not in response.text
        rows = c.get('/api/admin/audit', headers=h, params={'limit': 100}).json()['items']
        actions = {r['action'] for r in rows}
        assert {'registered', 'deleted', 'attachment_uploaded', 'read', 'message_sent', 'confirmed'} <= actions
        confirmed = next(r for r in rows if r['action'] == 'confirmed')
        assert confirmed['source'] == 'copilot' and confirmed['initiator_id'] == uid
        reply = next(r for r in rows if r['action'] == 'message_sent' and r['source'] == 'agent')
        assert reply['actor_id'] == agent_id and reply['initiator_id'] == uid
        assert 'PRIVATE-' not in str(rows) and 'PRIVATE-' not in caplog.text
        with SessionLocal() as db:
            event = db.scalar(select(AuditEvent).where(AuditEvent.organization_id == org))
            event.action = 'tampered'
            with pytest.raises(ValueError): db.commit()
            db.rollback()


def test_legacy_activity_redacts_free_text_but_keeps_before_after_contract():
    from app.models import Activity
    from app.copilot import create_confirmation
    with TestClient(app) as c:
        h, data = workspace(c)
        project = c.post('/api/projects', headers=h, json={'name': 'PRIVATE-NAME', 'code': 'P'}).json()
        with SessionLocal() as db:
            token = create_confirmation(db.get(User, data['user']['id']), 'update_project', {'project_id': project['id'], 'health': 'off_track', 'description': 'PRIVATE-DESCRIPTION'})
        assert c.post('/api/ai/confirm', headers=h, json={'confirmation_token': token}).status_code == 200
        with SessionLocal() as db:
            rows = db.scalars(select(Activity).where(Activity.organization_id == data['organization']['id'])).all()
            assert 'PRIVATE-' not in str([r.detail for r in rows])
            confirmed = next(r for r in rows if r.action == 'confirmed')
            assert confirmed.detail['args']['health'] == 'off_track'
            assert confirmed.detail['before']['health'] == 'on_track'
            assert confirmed.detail['after']['health'] == 'off_track'


def test_migration_upgrades_legacy_schema_and_preserves_agent_audit(tmp_path):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = Path(__file__).parents[1] / 'alembic/versions/0006_observability.py'
    assert migration.exists()
    spec = importlib.util.spec_from_file_location('migration6', migration)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    engine = create_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE organizations (id VARCHAR PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE users (id VARCHAR PRIMARY KEY)'))
        conn.execute(text("INSERT INTO organizations VALUES ('old')"))
        conn.execute(text("INSERT INTO users VALUES ('old')"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()  # idempotent, for legacy databases upgraded without Alembic bookkeeping
        assert {'audit_events', 'provider_calls', 'execution_runs'} <= set(inspect(conn).get_table_names())
        assert conn.scalar(text('SELECT execution_enabled FROM organizations')) == 1
        assert conn.scalar(text('SELECT execution_enabled FROM users')) == 1
        assert not inspect(conn).get_foreign_keys('audit_events')


def test_provider_reads_and_hierarchy_changes_are_audited_without_reenabling():
    from app.copilot import WorkspaceTools, ProviderTurn
    from test_agent_execution import _ScriptedProvider, _service_with
    from app.models import AuditEvent
    with TestClient(app) as c:
        h, data = workspace(c)
        c.patch('/api/hierarchy-config', headers=h, json={'vp_count': 1})
        agent = c.get('/api/agents', headers=h).json()[0]
        c.patch(f"/api/admin/agents/{agent['id']}/execution", headers=h, json={'execution_enabled': False})
        c.patch('/api/hierarchy-config', headers=h, json={'vp_count': 2})
        with SessionLocal() as db:
            assert db.get(User, agent['id']).execution_enabled is False
            _service_with(_ScriptedProvider([ProviderTurn(None, [{'id': 'r', 'name': 'get_users', 'args': {}}]), ProviderTurn('ok')])).plan('hello', WorkspaceTools(db, db.get(User, data['user']['id'])))
        rows = c.get('/api/admin/audit', headers=h, params={'action': 'get_users'}).json()['items']
        assert len(rows) == 1 and rows[0]['source'] == 'copilot'
        c.patch('/api/hierarchy-config', headers=h, json={'vp_count': 0})
        rows = c.get('/api/admin/audit', headers=h, params={'entity_type': 'agent', 'action': 'retired'}).json()['items']
        assert any(r['entity_id'] == agent['id'] for r in rows)
        assert all(r['initiator_id'] == data['user']['id'] for r in rows)
        with SessionLocal() as db:
            retired = db.get(User, agent['id'])
            # Retirement must never delete the row or quietly flip execution back on.
            assert retired is not None and retired.retired_at is not None and retired.execution_enabled is False


def test_document_draft_usage_and_unexpected_provider_errors_are_safe(monkeypatch):
    from app.copilot import DeterministicCopilotProvider
    with TestClient(app) as c:
        h, data = workspace(c)
        result = c.post('/api/project-drafts/assist', headers=h, files={'file': ('brief.txt', b'Project: Test brief')})
        assert result.status_code == 201
        assert c.get('/api/admin/usage', headers=h).json()['totals']['calls'] == 1
        assert c.get('/api/admin/audit', headers=h, params={'action': 'draft_created'}).json()['total'] == 1
        def fail(*args): raise RuntimeError('PRIVATE-ERROR')
        monkeypatch.setattr(DeterministicCopilotProvider, 'answer', fail)
        response = c.post('/api/ai/plan', headers=h, json={'message': 'hello'})
        assert response.status_code == 503 and 'PRIVATE-ERROR' not in response.text
        totals = c.get('/api/admin/usage', headers=h).json()['totals']
        assert totals['calls'] == 2 and totals['failures'] == 1


def test_historical_activity_is_redacted_at_read_boundaries():
    from app.models import Activity
    from app.copilot import WorkspaceTools
    with TestClient(app) as c:
        h, data = workspace(c)
        with SessionLocal() as db:
            db.add(Activity(organization_id=data['organization']['id'], actor_id=data['user']['id'], entity_type='task', entity_id='legacy', action='created', detail={'title': 'PRIVATE-OLD', 'args': {'body': 'PRIVATE-OLD'}}))
            db.commit()
            assert 'PRIVATE-OLD' not in str(WorkspaceTools(db, db.get(User, data['user']['id'])).get_activity())
        assert 'PRIVATE-OLD' not in c.get('/api/activity', headers=h).text


def test_usage_counts_real_multiturn_failure_and_deterministic_calls(monkeypatch):
    from app.copilot import CopilotService, WorkspaceTools, ProviderTurn, CopilotProviderError
    from test_agent_execution import _ScriptedProvider, _service_with
    import pytest
    with TestClient(app) as c:
        h, data = workspace(c)
        other, _ = workspace(c)
        assert c.post('/api/ai/plan', headers=h, json={'message': 'hello PRIVATE-PROMPT'}).status_code == 200
        with SessionLocal() as db:
            user = db.get(User, data['user']['id'])
            service = _service_with(_ScriptedProvider([ProviderTurn(None, [{'id': 'r', 'name': 'get_projects', 'args': {}}]), ProviderTurn('ok')]))
            service.plan('PRIVATE-PROMPT', WorkspaceTools(db, user))
            class Failing(_ScriptedProvider):
                def respond(self, *args, **kwargs):
                    raise CopilotProviderError('SECRET-ERROR')
            with pytest.raises(CopilotProviderError):
                _service_with(Failing([])).plan('PRIVATE-PROMPT', WorkspaceTools(db, user))
            db.rollback()  # actual calls must survive request rollback
        usage = c.get('/api/admin/usage', headers=h).json()
        assert usage['totals']['calls'] == 4
        assert usage['totals']['failures'] == 1
        assert usage['totals']['duration_ms'] >= 0
        assert usage['provider_mode'] == 'deterministic'
        assert usage['totals']['executions'] == 0
        assert c.get('/api/admin/usage', headers=other).json()['totals']['calls'] == 0
        assert c.get('/api/admin/usage', headers=h, params={'start': '2099-01-01T00:00:00'}).json()['totals']['calls'] == 0
        assert 'SECRET-ERROR' not in str(usage) and 'PRIVATE-PROMPT' not in str(usage)


def test_audit_search_privacy_pagination_and_tenant_isolation():
    with TestClient(app) as c:
        h, data = workspace(c)
        other, _ = workspace(c)
        assert c.post('/api/teams', headers=h, json={'name': 'PRIVATE-BODY'}).status_code == 201
        result = c.get('/api/admin/audit', headers=h, params={'action': 'created', 'entity_type': 'team', 'limit': 1}).json()
        assert result['total'] == 1 and result['offset'] == 0 and result['limit'] == 1
        item = result['items'][0]
        assert item['actor_id'] == item['initiator_id'] == data['user']['id']
        assert item['source'] == 'human'
        assert 'PRIVATE-BODY' not in str(result)
        assert c.get('/api/admin/audit', headers=other, params={'entity_type': 'team'}).json()['total'] == 0
        assert c.get('/api/admin/audit', headers=h, params={'q': item['id']}).json()['total'] == 1
        assert c.get('/api/admin/audit', headers=h, params={'actor_id': item['actor_id'], 'source': 'human', 'start': '2099-01-01T00:00:00'}).json()['total'] == 0
        assert c.get('/api/admin/audit', headers=h, params={'limit': 101}).status_code == 422
        assert c.get('/api/admin/audit', headers=h, params={'offset': -1}).status_code == 422


def test_role_governance_and_agent_execution_setting():
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        member, _ = identity(org)
        agent, _ = identity(org, 'admin', 'agent')
        foreign, _ = workspace(c)
        assert c.patch(f'/api/admin/users/{member}/role', headers=h, json={'role': 'manager'}).json()['role'] == 'manager'
        assert c.patch(f'/api/admin/users/{data["user"]["id"]}/role', headers=h, json={'role': 'member'}).status_code == 409
        assert c.patch(f'/api/admin/users/{agent}/role', headers=h, json={'role': 'member'}).status_code == 404
        assert c.patch(f'/api/admin/users/{member}/role', headers=foreign, json={'role': 'admin'}).status_code == 404
        assert c.patch(f'/api/admin/users/{member}/role', headers=h, json={'role': 'owner'}).status_code == 422
        assert {u['id'] for u in c.get('/api/admin/users', headers=h).json()} == {member, data['user']['id']}
        assert c.patch(f'/api/admin/agents/{agent}/execution', headers=h, json={'execution_enabled': False}).json() == {'id': agent, 'execution_enabled': False}
        assert c.patch(f'/api/admin/agents/{agent}/execution', headers=foreign, json={'execution_enabled': True}).status_code == 404
        with SessionLocal() as db:
            assert db.get(User, agent).execution_enabled is False


def test_admin_settings_are_human_admin_only_and_tenant_scoped():
    with TestClient(app) as c:
        h, data = workspace(c)
        other, _ = workspace(c)
        assert c.get('/api/admin/settings', headers=h).json() == {'execution_enabled': True}
        for role, kind in [('member', 'human'), ('manager', 'human'), ('admin', 'agent')]:
            _, denied = identity(data['organization']['id'], role, kind)
            assert c.get('/api/admin/settings', headers=denied).status_code == 403
            assert c.patch('/api/admin/settings', headers=denied, json={'execution_enabled': False}).status_code == 403
        assert c.get('/api/admin/settings').status_code == 401
        assert c.patch('/api/admin/settings', headers=h, json={'execution_enabled': False}).json() == {'execution_enabled': False}
        assert c.get('/api/admin/settings', headers=h).json() == {'execution_enabled': False}
        assert c.get('/api/admin/settings', headers=other).json() == {'execution_enabled': True}
