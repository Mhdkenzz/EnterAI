"""Human administrator governance. Never exposed as provider tools."""
from typing import Literal
from datetime import datetime, timezone
from sqlalchemy import select, func, or_
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, model_validator
from sqlalchemy.orm import Session
from .auth import current_user
from .database import get_db
from .hierarchy import retire_agent
from .models import AgentAccessGrant, AuditEvent, Organization, OrganizationAISettings, ProviderCall, ExecutionRun, Project, ProjectDocument, Task, User
from .services import log
from .observability import audit
from .agent_workspace import repository_root
from . import secrets_store
from .ai.config import AISettings
from .agent_access import BASE_AGENT_TOOLS, VALID_PERMISSIONS, allowed_agent_tools, grants_for, tool_catalog
from .knowledge_access import document_is_visible, visible_project_ids
from .time_utils import utcnow_naive

router = APIRouter(prefix='/api/admin', tags=['admin'])


def human_admin(user: User = Depends(current_user)):
    if user.kind != 'human' or user.role != 'admin' or not user.active:
        raise HTTPException(403, 'Human administrator required')
    return user


def member_out(u):
    return {'id': u.id, 'name': u.name, 'email': u.email, 'role': u.role, 'active': u.active}


def time_window(stmt, column, start, end):
    def naive(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    start, end = naive(start), naive(end)
    if start and end and start > end:
        raise HTTPException(422, 'start must not exceed end')
    if start: stmt = stmt.where(column >= start)
    if end: stmt = stmt.where(column <= end)
    return stmt


@router.get('/audit')
def audit_search(q: str | None = None, action: str | None = None, source: str | None = None,
                 actor_id: str | None = None, entity_type: str | None = None,
                 start: datetime | None = None, end: datetime | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                 user: User = Depends(human_admin), db: Session = Depends(get_db)):
    stmt = select(AuditEvent).where(AuditEvent.organization_id == user.organization_id)
    for column, value in [(AuditEvent.action, action), (AuditEvent.source, source),
                          (AuditEvent.actor_id, actor_id), (AuditEvent.entity_type, entity_type)]:
        if value is not None: stmt = stmt.where(column == value)
    if q:
        stmt = stmt.where(or_(*[column.contains(q, autoescape=True) for column in
            [AuditEvent.id, AuditEvent.action, AuditEvent.entity_id, AuditEvent.entity_type, AuditEvent.actor_id]]))
    stmt = time_window(stmt, AuditEvent.created_at, start, end)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).offset(offset).limit(limit))
    keys = ['id', 'actor_id', 'initiator_id', 'source', 'action', 'entity_type', 'entity_id', 'detail', 'created_at']
    items = [{key: getattr(row, key) for key in keys} for row in rows]
    # Names for the people involved, so the log reads without cross-referencing IDs.
    ids = {i[k] for i in items for k in ('actor_id', 'initiator_id') if i[k]}
    people = {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids), User.organization_id == user.organization_id))} if ids else {}
    label = lambda uid: (f"{people[uid].name} ({people[uid].email})" if people[uid].kind == 'human' else f"{people[uid].name} (agent)") if uid in people else None
    for item in items:
        item['actor_name'], item['initiator_name'] = label(item['actor_id']), label(item['initiator_id'])
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit}


@router.get('/usage')
def usage(agent_id: str | None = None, start: datetime | None = None, end: datetime | None = None,
          user: User = Depends(human_admin), db: Session = Depends(get_db)):
    from .copilot import CopilotService
    from .execution import MAX_CONSECUTIVE_FAILURES
    stmt = select(ProviderCall).where(ProviderCall.organization_id == user.organization_id)
    agents_stmt = select(User).where(User.organization_id == user.organization_id, User.kind == 'agent')
    if agent_id:
        stmt = stmt.where(ProviderCall.agent_id == agent_id)
        agents_stmt = agents_stmt.where(User.id == agent_id)
    runs_stmt = select(ExecutionRun).where(ExecutionRun.organization_id == user.organization_id)
    if agent_id: runs_stmt = runs_stmt.where(ExecutionRun.agent_id == agent_id)
    runs = db.scalars(time_window(runs_stmt, ExecutionRun.created_at, start, end)).all()
    calls = db.scalars(time_window(stmt, ProviderCall.created_at, start, end)).all()
    totals = {'calls': len(calls), 'failures': sum(c.failed for c in calls),
              'duration_ms': sum(c.duration_ms for c in calls), 'executions': len(runs), 'execution_failures': sum(r.failed for r in runs)}
    enabled = db.get(Organization, user.organization_id).execution_enabled
    agents = []
    for a in db.scalars(agents_stmt.order_by(User.created_at, User.id)):
        own = [c for c in calls if c.agent_id == a.id]
        status = 'retired' if a.retired_at else 'disabled' if not enabled or not a.execution_enabled else 'blocked' if a.consecutive_task_failures >= MAX_CONSECUTIVE_FAILURES else 'working' if a.current_task_id else 'idle'
        agents.append({'id': a.id, 'name': a.name, 'execution_enabled': a.execution_enabled, 'retired_at': a.retired_at,
            'status': status, 'last_execution_at': a.last_execution_at, 'consecutive_task_failures': a.consecutive_task_failures,
            'calls': len(own), 'failures': sum(c.failed for c in own), 'executions': sum(r.agent_id == a.id for r in runs), 'execution_failures': sum(r.failed and r.agent_id == a.id for r in runs)})
    # Same already-fetched `calls` list, grouped by (provider, model) rather than
    # by agent -- answers "which model actually handled this org's calls" without
    # a second query or exposing any individual call's prompt/reply (see
    # ProviderCall's own privacy-minimal column set).
    by_model: dict[tuple[str | None, str | None], dict] = {}
    for c in calls:
        bucket = by_model.setdefault((c.provider, c.model), {
            'provider': c.provider, 'model': c.model, 'calls': 0, 'failures': 0,
            'duration_ms': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'reasoning_tokens': 0,
        })
        bucket['calls'] += 1
        bucket['failures'] += int(c.failed)
        bucket['duration_ms'] += c.duration_ms
        bucket['prompt_tokens'] += c.prompt_tokens or 0
        bucket['completion_tokens'] += c.completion_tokens or 0
        bucket['reasoning_tokens'] += c.reasoning_tokens or 0
    return {'totals': totals, 'agents': agents, 'provider_mode': CopilotService(db, user.organization_id).mode, 'by_model': list(by_model.values())}


@router.get('/users')
def users(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    return [member_out(u) for u in db.scalars(select(User).where(User.organization_id == user.organization_id, User.kind == 'human').order_by(User.name, User.id))]


class RoleSetting(BaseModel):
    role: Literal['admin', 'manager', 'member']


@router.patch('/users/{user_id}/role')
def update_role(user_id: str, data: RoleSetting, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    # Authentication has already opened a read transaction. Start a new serialized
    # governance transaction, then revalidate the caller (it may have been demoted
    # while waiting). SQLite ignores FOR UPDATE, so reserve its writer lock instead.
    actor_id, org_id = user.id, user.organization_id
    db.rollback()
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    else:
        db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    fresh_user = db.get(User, actor_id)
    if not fresh_user or fresh_user.organization_id != org_id:
        raise HTTPException(403, 'Human administrator required')
    human_admin(fresh_user)
    user = fresh_user
    target = db.get(User, user_id)
    if not target or target.organization_id != user.organization_id or target.kind != 'human':
        raise HTTPException(404, 'User not found')
    if data.role != 'admin' and target.role == 'admin':
        if target.id == user.id:
            raise HTTPException(409, 'Cannot demote yourself')
        remaining = db.scalars(select(User).where(User.organization_id == user.organization_id, User.kind == 'human', User.active.is_(True), User.role == 'admin', User.id != target.id)).all()
        if not remaining:
            raise HTTPException(409, 'An active human administrator is required')
    before = target.role
    target.role = data.role
    log(db, user.organization_id, user.id, 'user', target.id, 'role_updated', before=before, after=data.role)
    db.commit()
    return member_out(target)


class ActiveSetting(BaseModel):
    active: StrictBool


@router.patch('/users/{user_id}/active')
def set_active(user_id: str, data: ActiveSetting, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    """Remove someone from the workspace (or bring them back) without deleting their
    history: their tasks, comments and audit trail stay, but they can no longer sign
    in and every session they hold ends now. Same serialized governance transaction
    and last-administrator rule as a role change."""
    actor_id, org_id = user.id, user.organization_id
    db.rollback()
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    else:
        db.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    fresh_user = db.get(User, actor_id)
    if not fresh_user or fresh_user.organization_id != org_id:
        raise HTTPException(403, 'Human administrator required')
    human_admin(fresh_user)
    target = db.get(User, user_id)
    if not target or target.organization_id != org_id or target.kind != 'human':
        raise HTTPException(404, 'User not found')
    if not data.active:
        if target.id == fresh_user.id:
            raise HTTPException(409, 'You cannot deactivate yourself')
        if target.role == 'admin':
            others = db.scalars(select(User).where(User.organization_id == org_id, User.kind == 'human', User.active.is_(True),
                                                   User.role == 'admin', User.id != target.id)).all()
            if not others:
                raise HTTPException(409, 'An active human administrator is required')
        target.session_epoch = (target.session_epoch or 0) + 1  # end every session now
    target.active = data.active
    log(db, org_id, fresh_user.id, 'user', target.id, 'reactivated' if data.active else 'deactivated')
    db.commit()
    return member_out(target)


class ExecutionSetting(BaseModel):
    execution_enabled: StrictBool


class OrganizationSettingsUpdate(BaseModel):
    execution_enabled: StrictBool | None = None
    ceo_full_access_enabled: StrictBool | None = None
    agent_workspace_enabled: StrictBool | None = None

    @model_validator(mode="after")
    def require_setting(self):
        if self.execution_enabled is None and self.ceo_full_access_enabled is None and self.agent_workspace_enabled is None:
            raise ValueError("Provide at least one organization setting.")
        return self


@router.patch('/agents/{agent_id}/execution')
def update_agent_execution(agent_id: str, data: ExecutionSetting, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    target = db.get(User, agent_id)
    if not target or target.organization_id != user.organization_id or target.kind != 'agent':
        raise HTTPException(404, 'Agent not found')
    if target.retired_at is not None and data.execution_enabled:
        raise HTTPException(409, 'Retired agents cannot be re-enabled for execution')
    target.execution_enabled = data.execution_enabled
    log(db, user.organization_id, user.id, 'agent', target.id, 'execution_updated', execution_enabled=data.execution_enabled)
    db.commit()
    return {'id': target.id, 'execution_enabled': target.execution_enabled}


@router.post('/agents/{agent_id}/retire')
def retire_agent_route(agent_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    """Retire an agent: it keeps its identity and all historical tasks, messages,
    comments, and audit rows, but can never again receive new work, execute, or
    show up as active. See hierarchy.retire_agent for what this does and does not
    touch."""
    target = db.get(User, agent_id)
    if not target or target.organization_id != user.organization_id or target.kind != 'agent':
        raise HTTPException(404, 'Agent not found')
    if target.retired_at is not None:
        raise HTTPException(409, 'Agent is already retired')
    retire_agent(db, target, actor_id=user.id)
    db.commit()
    return {'id': target.id, 'retired': True, 'retired_at': target.retired_at}


class AgentAccessGrantIn(BaseModel):
    resource_type: Literal['project', 'document', 'tool']
    resource_id: str = Field(min_length=1, max_length=255)
    permission: Literal['read', 'write', 'use']
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode='after')
    def permission_matches_resource(self):
        if self.permission not in VALID_PERMISSIONS[self.resource_type]:
            raise ValueError('Permission is not valid for this resource type.')
        return self


def _agent_in_org(db: Session, org_id: str, agent_id: str) -> User:
    agent = db.scalar(select(User).where(User.id == agent_id, User.organization_id == org_id, User.kind == 'agent'))
    if not agent:
        raise HTTPException(404, 'Agent not found')
    return agent


def _access_grant_out(grant: AgentAccessGrant) -> dict:
    return {'id': grant.id, 'resource_type': grant.resource_type, 'resource_id': grant.resource_id,
            'permission': grant.permission, 'reason': grant.reason, 'granted_by': grant.granted_by,
            'created_at': grant.created_at, 'revoked_by': grant.revoked_by, 'revoked_at': grant.revoked_at,
            'active': grant.revoked_at is None}


@router.get('/agents/{agent_id}/access')
def inspect_agent_access(agent_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    agent = _agent_in_org(db, user.organization_id, agent_id)
    projects = db.scalars(select(Project).where(Project.organization_id == user.organization_id).order_by(Project.name, Project.id)).all()
    documents = db.scalars(select(ProjectDocument).where(ProjectDocument.organization_id == user.organization_id).order_by(ProjectDocument.file_name, ProjectDocument.id)).all()
    grants = grants_for(db, agent)
    assigned = db.scalars(select(Task.id).where(Task.assignee_id == agent.id)).all()
    effective_project_ids = db.scalars(visible_project_ids(db, agent)).all()
    effective_document_ids = db.scalars(select(ProjectDocument.id).where(document_is_visible(db, agent))).all()
    return {
        'agent_id': agent.id,
        'inherited': {'assigned_task_ids': assigned, 'baseline_tools': sorted(BASE_AGENT_TOOLS)},
        'effective_tools': sorted(allowed_agent_tools(db, agent)),
        'effective_project_ids': effective_project_ids,
        'effective_document_ids': effective_document_ids,
        'projects': [{'id': p.id, 'name': p.name, 'code': p.code} for p in projects],
        'documents': [{'id': d.id, 'file_name': d.file_name, 'project_id': d.project_id} for d in documents],
        'tools': sorted(tool_catalog()),
        'grants': [_access_grant_out(grant) for grant in grants],
    }


@router.post('/agents/{agent_id}/access', status_code=201)
def grant_agent_access(agent_id: str, data: AgentAccessGrantIn, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    agent = _agent_in_org(db, user.organization_id, agent_id)
    if not agent.active or agent.retired_at is not None:
        raise HTTPException(409, 'Inactive or retired agents cannot receive new access grants')
    if data.resource_type == 'project':
        exists = db.scalar(select(Project.id).where(Project.id == data.resource_id, Project.organization_id == user.organization_id))
    elif data.resource_type == 'document':
        exists = db.scalar(select(ProjectDocument.id).where(ProjectDocument.id == data.resource_id, ProjectDocument.organization_id == user.organization_id))
    else:
        exists = data.resource_id in tool_catalog()
    if not exists:
        raise HTTPException(404, 'Access resource not found')
    grant = db.scalar(select(AgentAccessGrant).where(
        AgentAccessGrant.organization_id == user.organization_id, AgentAccessGrant.agent_id == agent.id,
        AgentAccessGrant.resource_type == data.resource_type, AgentAccessGrant.resource_id == data.resource_id,
        AgentAccessGrant.permission == data.permission,
    ))
    if grant is None:
        grant = AgentAccessGrant(organization_id=user.organization_id, agent_id=agent.id,
            resource_type=data.resource_type, resource_id=data.resource_id, permission=data.permission,
            granted_by=user.id, reason=data.reason)
        db.add(grant)
        action = 'agent_access_granted'
    elif grant.revoked_at is not None:
        grant.granted_by, grant.created_at = user.id, utcnow_naive()
        grant.reason, grant.revoked_by, grant.revoked_at = data.reason, None, None
        action = 'agent_access_granted'
    else:
        grant.reason = data.reason
        action = 'agent_access_updated'
    audit(db, user.organization_id, user.id, 'agent', agent.id, action,
          resource_type=data.resource_type, permission=data.permission,
          resource_id=data.resource_id if data.resource_type in {'project', 'document'} else None,
          tool=data.resource_id if data.resource_type == 'tool' else None)
    db.commit()
    db.refresh(grant)
    return _access_grant_out(grant)


@router.delete('/agents/{agent_id}/access/{grant_id}')
def revoke_agent_access(agent_id: str, grant_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    agent = _agent_in_org(db, user.organization_id, agent_id)
    grant = db.scalar(select(AgentAccessGrant).where(
        AgentAccessGrant.id == grant_id, AgentAccessGrant.organization_id == user.organization_id,
        AgentAccessGrant.agent_id == agent.id,
    ))
    if not grant:
        raise HTTPException(404, 'Access grant not found')
    if grant.revoked_at is not None:
        raise HTTPException(409, 'Access grant is already revoked')
    grant.revoked_by, grant.revoked_at = user.id, utcnow_naive()
    audit(db, user.organization_id, user.id, 'agent', agent.id, 'agent_access_revoked',
          resource_type=grant.resource_type, permission=grant.permission,
          resource_id=grant.resource_id if grant.resource_type in {'project', 'document'} else None,
          tool=grant.resource_id if grant.resource_type == 'tool' else None)
    db.commit()
    return _access_grant_out(grant)


@router.get('/settings')
def settings(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    return {'execution_enabled': org.execution_enabled, 'ceo_full_access_enabled': org.ceo_full_access_enabled,
            'agent_workspace_enabled': org.agent_workspace_enabled, 'agent_workspace_available': repository_root(org.id, db) is not None,
            'agent_workspace_id': org.id}


@router.patch('/settings')
def update_settings(data: OrganizationSettingsUpdate, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    if data.execution_enabled is not None:
        org.execution_enabled = data.execution_enabled
        log(db, org.id, user.id, 'organization', org.id, 'execution_updated', execution_enabled=data.execution_enabled)
    if data.ceo_full_access_enabled is not None:
        org.ceo_full_access_enabled = data.ceo_full_access_enabled
        log(db, org.id, user.id, 'organization', org.id, 'ceo_full_access_updated', ceo_full_access_enabled=data.ceo_full_access_enabled)
    if data.agent_workspace_enabled is not None:
        if data.agent_workspace_enabled and repository_root(org.id, db) is None:
            raise HTTPException(409, 'Configure this organization\'s isolated Git checkout under AGENT_WORKSPACE_ROOT before enabling agent repository tools.')
        org.agent_workspace_enabled = data.agent_workspace_enabled
        log(db, org.id, user.id, 'organization', org.id, 'agent_workspace_updated', agent_workspace_enabled=data.agent_workspace_enabled)
    db.commit()
    return {'execution_enabled': org.execution_enabled, 'ceo_full_access_enabled': org.ceo_full_access_enabled,
            'agent_workspace_enabled': org.agent_workspace_enabled, 'agent_workspace_available': repository_root(org.id, db) is not None,
            'agent_workspace_id': org.id}


class AIProviderSettingsUpdate(BaseModel):
    provider: Literal["ifm", "openrouter"] | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=512)
    clear_api_key: StrictBool = False
    model: str | None = Field(default=None, min_length=1, max_length=200)
    requests_per_minute: int | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def validate_update(self):
        if self.api_key is not None and not self.api_key.strip():
            raise ValueError("API key cannot be blank")
        if self.api_key is not None and self.clear_api_key:
            raise ValueError("Set a new API key or clear the saved key, not both")
        if self.provider is None and self.api_key is None and self.requests_per_minute is None and self.model is None and not self.clear_api_key:
            raise ValueError("Provide an AI provider setting to update")
        return self


def ai_provider_settings_out(db: Session, organization_id: str) -> dict:
    saved = db.scalar(select(OrganizationAISettings).where(
        OrganizationAISettings.organization_id == organization_id,
    ))
    env = AISettings.from_env()
    configured_provider = saved.provider if saved else env.provider
    has_key = bool(saved and saved.api_key_encrypted)
    return {
        "provider": configured_provider if has_key else env.provider,
        "configured_provider": configured_provider,
        "model": saved.model if saved else env.model,
        "api_key_configured": has_key,
        "requests_per_minute": saved.requests_per_minute if saved else 20,
        "maximum_requests_per_minute": 120,
    }


@router.get('/ai-provider')
def get_ai_provider_settings(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    return ai_provider_settings_out(db, user.organization_id)


@router.patch('/ai-provider')
async def update_ai_provider_settings(
    data: AIProviderSettingsUpdate, user: User = Depends(human_admin), db: Session = Depends(get_db),
):
    saved = db.scalar(select(OrganizationAISettings).where(
        OrganizationAISettings.organization_id == user.organization_id,
    ))
    if not saved:
        saved = OrganizationAISettings(organization_id=user.organization_id, provider="ifm", model="IFM/K2-Horizon-375B-A23B")
        db.add(saved)
    if data.provider is not None and data.provider != saved.provider and data.api_key is None and not data.clear_api_key:
        raise HTTPException(422, "Enter a new API key when changing providers")
    if data.provider is not None:
        saved.provider = data.provider
    if data.model is not None:
        saved.model = "IFM/K2-Horizon-375B-A23B" if saved.provider == "ifm" else data.model.strip()
    elif data.provider == "ifm":
        saved.model = "IFM/K2-Horizon-375B-A23B"
    if data.requests_per_minute is not None:
        saved.requests_per_minute = data.requests_per_minute
    if data.clear_api_key:
        saved.api_key_encrypted = None
    elif data.api_key is not None:
        try:
            saved.api_key_encrypted = secrets_store.encrypt(data.api_key.strip())
        except RuntimeError as error:
            raise HTTPException(503, "Configure SECRETS_ENCRYPTION_KEY on the hosted server before saving provider credentials") from error
    db.flush()  # Materialize the new settings ID before it is recorded in the audit event.
    audit(db, user.organization_id, user.id, 'ai_provider_settings', saved.id, 'updated',
          provider=saved.provider, model=saved.model, requests_per_minute=saved.requests_per_minute,
          api_key_changed=data.api_key is not None or data.clear_api_key)
    db.commit()
    from . import execution
    execution.start_background_loop()
    return ai_provider_settings_out(db, user.organization_id)
