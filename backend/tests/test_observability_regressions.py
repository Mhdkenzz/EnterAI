"""Phase 7 review regressions: durable attribution and private logging."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.database import SessionLocal
from app.models import User, AuditEvent, ProviderCall, ExecutionRun
from test_observability import workspace, identity


def test_hierarchy_configuration_rejects_admin_agent():
    with TestClient(app) as c:
        h, data = workspace(c)
        _, agent_headers = identity(data['organization']['id'], 'admin', 'agent')
        before = c.get('/api/hierarchy-config', headers=h).json()
        assert c.patch('/api/hierarchy-config', headers=agent_headers, json={'vp_count': 1}).status_code == 403
        assert c.get('/api/hierarchy-config', headers=h).json() == before


def test_issued_admin_token_loses_access_after_demotion():
    with TestClient(app) as c:
        h, data = workspace(c)
        admin_id, issued = identity(data['organization']['id'], 'admin')
        assert c.get('/api/admin/settings', headers=issued).status_code == 200
        assert c.patch(f'/api/admin/users/{admin_id}/role', headers=h, json={'role': 'member'}).status_code == 200
        for endpoint in ('settings', 'users', 'usage', 'audit'):
            assert c.get('/api/admin/'+endpoint, headers=issued).status_code == 403


def test_failed_provider_calls_keep_distinct_initiators_after_rollback():
    from app.copilot import WorkspaceTools, CopilotProviderError
    from app.observability import audit_context, provider_call
    def fail(): raise RuntimeError('PRIVATE-ERROR')
    with TestClient(app) as c:
        h, data = workspace(c)
        org, first = data['organization']['id'], data['user']['id']
        second, _ = identity(org)
        agent_id, _ = identity(org, 'admin', 'agent')
        for initiator in (first, second):
            with SessionLocal() as db:
                with audit_context('agent', agent_id, initiator), pytest.raises(CopilotProviderError):
                    provider_call(WorkspaceTools(db, db.get(User, agent_id)), 'tool_calling', fail)
                db.rollback()
        with SessionLocal() as db:
            calls = db.scalars(select(ProviderCall).where(ProviderCall.agent_id == agent_id)).all()
            assert len(calls) == 2
            assert {call.initiator_id for call in calls} == {first, second}
            assert all(call.source == 'agent' and call.actor_id == agent_id and call.failed and call.duration_ms >= 0 for call in calls)
            events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org, AuditEvent.entity_type == 'provider_call')).all()
            assert {e.initiator_id for e in events} == {first, second}
            assert {e.entity_id for e in events} == {call.id for call in calls}
            assert all(e.action == 'call_failed' and e.actor_id == agent_id and e.detail['outcome'] == 'failed' for e in events)
            assert 'PRIVATE-' not in str([e.detail for e in events])


def test_failed_chat_keeps_human_message_audit(monkeypatch):
    from app.copilot import DeterministicCopilotProvider
    from app.models import AgentMessage
    def fail(*args): raise RuntimeError('PRIVATE-ERROR')
    with TestClient(app) as c:
        h, data = workspace(c)
        org, uid = data['organization']['id'], data['user']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        monkeypatch.setattr(DeterministicCopilotProvider, 'answer', fail)
        assert c.post(f'/api/agents/{agent_id}/messages', headers=h, json={'message': 'PRIVATE-PROMPT'}).status_code == 503
        with SessionLocal() as db:
            assert db.scalar(select(AgentMessage).where(AgentMessage.agent_id == agent_id)).author_id == uid
            events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org, AuditEvent.action == 'message_sent')).all()
            assert len(events) == 1 and events[0].actor_id == events[0].initiator_id == uid
            assert events[0].source == 'human'


def test_failed_plan_without_provider_call_is_audited(monkeypatch):
    from app.copilot import CopilotService, CopilotProviderError
    def fail(*args): raise CopilotProviderError('PRIVATE-ERROR')
    with TestClient(app) as c:
        h, data = workspace(c)
        monkeypatch.setattr(CopilotService, '_plan_deterministic', fail)
        assert c.post('/api/ai/plan', headers=h, json={'message': 'PRIVATE-PROMPT'}).status_code == 503
        rows = c.get('/api/admin/audit', headers=h, params={'entity_type': 'copilot', 'action': 'plan_failed'}).json()['items']
        assert len(rows) == 1 and rows[0]['initiator_id'] == data['user']['id']
        assert rows[0]['source'] == 'copilot' and rows[0]['detail']['outcome'] == 'failed'


def test_failed_read_attempt_survives_rollback():
    from app.copilot import WorkspaceTools, execute_read_tool
    with TestClient(app) as c:
        h, data = workspace(c)
        with SessionLocal() as db:
            with pytest.raises(ValueError):
                execute_read_tool('get_comments', {'task_id': 'PRIVATE-MISSING'}, WorkspaceTools(db, db.get(User, data['user']['id'])))
            db.rollback()
        rows = c.get('/api/admin/audit', headers=h, params={'entity_type': 'provider_read'}).json()['items']
        assert len(rows) == 1 and rows[0]['detail']['outcome'] == 'failed'
        assert rows[0]['source'] == 'copilot' and rows[0]['initiator_id'] == data['user']['id']
        assert 'PRIVATE-' not in str(rows)


@pytest.mark.parametrize('outcome', ['narrated', 'proposed', 'completed', 'inconclusive'])
def test_scheduler_execution_outcomes_are_durable_and_correlated(outcome, monkeypatch):
    from app import execution
    from app.copilot import ProviderTurn
    from app.models import Task
    from test_agent_execution import _ScriptedProvider, _service_with
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        project = c.post('/api/projects', headers=h, json={'name': 'P', 'code': 'P'}).json()
        task = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'PRIVATE-TASK'}).json()
        c.post(f"/api/tasks/{task['id']}/assign-agent", headers=h, json={'agent_id': agent_id})
        calls = {'narrated': [], 'proposed': [{'id': 'w', 'name': 'add_comment', 'args': {'task_id': task['id'], 'body': 'PRIVATE-BODY'}}],
                 'completed': [{'id': 'w', 'name': 'mark_task_complete', 'args': {}}],
                 'inconclusive': [{'id': 'r', 'name': 'get_projects', 'args': {}}]}
        turns = [ProviderTurn('PRIVATE-NARRATION', calls[outcome])] * (4 if outcome == 'inconclusive' else 1)
        with SessionLocal() as db:
            monkeypatch.setattr(execution, '_eligible_agents', lambda db: [db.get(User, agent_id)])
            assert execution.run_execution_tick(db, _service_with(_ScriptedProvider(turns))) == 1
            db.rollback()
        with SessionLocal() as db:
            runs = db.scalars(select(ExecutionRun).where(ExecutionRun.agent_id == agent_id)).all()
            assert len(runs) == 1 and runs[0].outcome == outcome and not runs[0].failed
            calls = db.scalars(select(ProviderCall).where(ProviderCall.agent_id == agent_id)).all()
            assert len(calls) == len(turns) and all(call.run_id == runs[0].id for call in calls)
            assert all(call.source == 'scheduler' and call.actor_id == agent_id and call.initiator_id is None for call in calls)
            events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org, AuditEvent.entity_type == 'execution_run')).all()
            assert {e.detail['outcome'] for e in events} == {'started', outcome}
            assert all(e.entity_id == runs[0].id and e.source == 'scheduler' and e.actor_id == agent_id and e.initiator_id is None for e in events)
            assert 'PRIVATE-' not in str([e.detail for e in events])
        c.patch(f'/api/admin/agents/{agent_id}/execution', headers=h, json={'execution_enabled': False})


def test_startup_configures_private_structured_logging_idempotently(caplog):
    import json
    import logging
    from app.copilot import WorkspaceTools, CopilotProviderError
    from app.observability import logger, provider_call
    with TestClient(app) as c:
        h, data = workspace(c)
        handlers = list(logger.handlers)
        assert handlers, 'startup must install an explicit structured handler'
        with TestClient(app):
            assert logger.handlers == handlers
        caplog.set_level(logging.INFO, logger=logger.name)
        with SessionLocal() as db:
            tools = WorkspaceTools(db, db.get(User, data['user']['id']))
            provider_call(tools, 'deterministic', lambda: 'PRIVATE-PROMPT')
            def fail(): raise RuntimeError('PRIVATE-ERROR')
            with pytest.raises(CopilotProviderError):
                provider_call(tools, 'deterministic', fail)
        records = [r for r in caplog.records if r.name == logger.name]
        serialized = [json.loads(handlers[0].format(r)) for r in records]
        calls = [r for r in serialized if r['event'] == 'provider_call']
        assert {r['outcome'] for r in calls} == {'succeeded', 'failed'}
        assert all(r['provider_mode'] == 'deterministic' and r['source'] == 'copilot' and r['correlation_id'] for r in calls)
        assert any(r['event'] == 'audit_event' and r.get('outcome') == 'failed' for r in serialized)
        assert 'PRIVATE-' not in str(serialized) and 'PRIVATE-' not in caplog.text


def test_access_log_filter_removes_query_strings():
    import logging
    with TestClient(app):
        access = logging.getLogger('uvicorn.access')
        record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %d',
                                   ('127.0.0.1', 'GET', '/api/admin/audit?q=PRIVATE-QUERY&offset=1', '1.1', 200), None)
        assert access.filter(record)
        assert 'PRIVATE-' not in record.getMessage() and '/api/admin/audit' in record.getMessage()
        assert '?' not in record.getMessage()


def test_unexpected_agent_tick_error_is_durably_attributed(monkeypatch, caplog):
    from app import execution
    from app.copilot import CopilotService
    from app.models import Task
    with TestClient(app) as c:
        h, data = workspace(c)
        org = data['organization']['id']
        agent_id, _ = identity(org, 'admin', 'agent')
        project = c.post('/api/projects', headers=h, json={'name': 'P', 'code': 'P'}).json()
        task = c.post('/api/tasks', headers=h, json={'project_id': project['id'], 'title': 'T'}).json()
        c.post(f"/api/tasks/{task['id']}/assign-agent", headers=h, json={'agent_id': agent_id})
        def fail(*args): raise RuntimeError('PRIVATE-ERROR')
        monkeypatch.setattr(CopilotService, '_run_execution_step', fail)
        service = CopilotService(); service.mode = 'tool_calling'
        with SessionLocal() as db:
            monkeypatch.setattr(execution, '_eligible_agents', lambda db: [db.get(User, agent_id)])
            assert execution.run_execution_tick(db, service) == 1
            db.rollback()
        with SessionLocal() as db:
            run = db.scalar(select(ExecutionRun).where(ExecutionRun.agent_id == agent_id))
            assert run.failed and run.outcome == 'failed'
            events = db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org, AuditEvent.action == 'scheduler_failed')).all()
            assert len(events) == 1 and events[0].actor_id == agent_id and events[0].initiator_id is None
            assert events[0].source == 'scheduler' and events[0].detail == {'outcome': 'failed'}
        records = [r for r in caplog.records if getattr(r, 'event', None) == 'scheduler_error']
        assert records and records[0].source == 'scheduler' and records[0].outcome == 'failed'
        assert 'PRIVATE-' not in caplog.text
        c.patch(f'/api/admin/agents/{agent_id}/execution', headers=h, json={'execution_enabled': False})


def test_scheduler_loop_failure_is_logged_and_audited(monkeypatch, caplog):
    import asyncio
    from app import execution
    def fail(): raise RuntimeError('PRIVATE-ERROR')
    async def stop(_): raise asyncio.CancelledError()
    with TestClient(app):
        monkeypatch.setattr(execution, 'run_execution_tick', fail)
        monkeypatch.setattr(execution.asyncio, 'sleep', stop)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(execution._loop())
        with SessionLocal() as db:
            events = db.scalars(select(AuditEvent).where(AuditEvent.entity_type == 'scheduler', AuditEvent.actor_id.is_(None))).all()
            assert events and events[-1].source == 'scheduler' and events[-1].detail == {'outcome': 'failed'}
        assert any(getattr(r, 'event', None) == 'scheduler_error' for r in caplog.records)
        assert 'PRIVATE-' not in caplog.text

