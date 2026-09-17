"""Human administrator governance. Never exposed as provider tools."""
from typing import Literal
from datetime import datetime, timezone
from sqlalchemy import select, func, or_
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool
from sqlalchemy.orm import Session
from .auth import current_user
from .database import get_db
from .models import AuditEvent, Organization, ProviderCall, ExecutionRun, User
from .services import log

router = APIRouter(prefix='/api/admin', tags=['admin'])


def human_admin(user: User = Depends(current_user)):
    if user.kind != 'human' or user.role != 'admin' or not user.active:
        raise HTTPException(403, 'Human administrator required')
    return user


def member_out(u):
    return {'id': u.id, 'name': u.name, 'role': u.role, 'active': u.active}


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
    return {'items': [{key: getattr(row, key) for key in keys} for row in rows], 'total': total, 'offset': offset, 'limit': limit}


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
        status = 'disabled' if not enabled or not a.execution_enabled else 'blocked' if a.consecutive_task_failures >= MAX_CONSECUTIVE_FAILURES else 'working' if a.current_task_id else 'idle'
        agents.append({'id': a.id, 'name': a.name, 'execution_enabled': a.execution_enabled,
            'status': status, 'last_execution_at': a.last_execution_at, 'consecutive_task_failures': a.consecutive_task_failures,
            'calls': len(own), 'failures': sum(c.failed for c in own), 'executions': sum(r.agent_id == a.id for r in runs), 'execution_failures': sum(r.failed and r.agent_id == a.id for r in runs)})
    return {'totals': totals, 'agents': agents, 'provider_mode': CopilotService().mode}


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


class ExecutionSetting(BaseModel):
    execution_enabled: StrictBool


@router.patch('/agents/{agent_id}/execution')
def update_agent_execution(agent_id: str, data: ExecutionSetting, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    target = db.get(User, agent_id)
    if not target or target.organization_id != user.organization_id or target.kind != 'agent':
        raise HTTPException(404, 'Agent not found')
    target.execution_enabled = data.execution_enabled
    log(db, user.organization_id, user.id, 'agent', target.id, 'execution_updated', execution_enabled=data.execution_enabled)
    db.commit()
    return {'id': target.id, 'execution_enabled': target.execution_enabled}


@router.get('/settings')
def settings(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    return {'execution_enabled': db.get(Organization, user.organization_id).execution_enabled}


@router.patch('/settings')
def update_settings(data: ExecutionSetting, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    org = db.get(Organization, user.organization_id)
    org.execution_enabled = data.execution_enabled
    log(db, org.id, user.id, 'organization', org.id, 'execution_updated', execution_enabled=data.execution_enabled)
    db.commit()
    return {'execution_enabled': org.execution_enabled}
