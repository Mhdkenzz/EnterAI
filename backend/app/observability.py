"""Privacy-minimal governance ledger, separate from legacy product activity.

Do not put prompts, response bodies, filenames, emails or raw exceptions here.
Legacy Activity is intentionally retained for product/API compatibility.
Append-only protection is an ORM instance guard, not a database-level boundary;
bulk SQL and privileged database operators must be governed separately.
Usage currently loads the time-window rows and scans per agent; aggregate in SQL
before scaling to large ledgers. Global scheduler faults use the reserved
organization namespace '__system__' (no tenant or agent is yet known).
"""
import asyncio
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone
import logging
import json
import re
import time
from sqlalchemy import event, func, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session
from .models import ProviderCall, ExecutionRun, Organization
from .models import AuditEvent, User, uid

# Scheduler faults with no known tenant use this sentinel (see
# scheduler_failure below) -- it is never a real organizations.id, so the
# aggregation writer must skip it rather than violate the FK on
# enterprise_audit_aggregations.organization_id.
_NO_TENANT = '__system__'

_context = ContextVar('audit_context', default=None)
_run_id: ContextVar[str | None] = ContextVar('execution_run_id', default=None)
logger = logging.getLogger('enterai.observability')


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        # Never serialize msg/args, exception text, or arbitrary extra fields.
        enums = {'event': {'audit_event', 'provider_call', 'scheduler_error'},
                 'source': {'human', 'agent', 'copilot', 'scheduler', 'system'},
                 'outcome': {'started', 'succeeded', 'failed', 'narrated', 'proposed', 'completed', 'inconclusive'},
                 'provider_mode': {'deterministic', 'tool_calling'}}
        fields = {key: value for key, allowed in enums.items()
                  if isinstance(value := getattr(record, key, None), str) and value in allowed}
        if isinstance(getattr(record, 'failed', None), bool):
            fields['failed'] = record.failed
        for key in ('run_id', 'correlation_id'):
            value = getattr(record, key, None)
            if isinstance(value, str) and re.fullmatch(r'[0-9a-f-]{36}', value):
                fields[key] = value
        return json.dumps(fields, separators=(',', ':'))


class AccessQueryFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn formats (client, method, full_path, version, status).
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            args[2] = str(args[2]).split('?', 1)[0]
            record.args = tuple(args)
        else:
            record.msg = re.sub(r'\?[^\s"\']*', '', record.getMessage())
            record.args = ()
        return True


def configure_logging():
    # Preserve operator-provided handlers/levels; configure only the default case.
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, AccessQueryFilter) for f in access.filters):
        access.addFilter(AccessQueryFilter())


@contextmanager
def audit_context(source, actor_id, initiator_id=None):
    token = _context.set((source, actor_id, initiator_id))
    try:
        yield
    finally:
        _context.reset(token)


def legacy_detail(detail):
    """Preserve structured args/before/after shape without duplicating content/PII."""
    enums = {'status': {'active', 'planning', 'archived', 'todo', 'backlog', 'in_progress', 'review', 'done'},
             'health': {'on_track', 'at_risk', 'off_track'}, 'priority': {'low', 'medium', 'high'},
             'role': {'admin', 'manager', 'member'}}
    result = {}
    for key, value in detail.items():
        if isinstance(value, dict):
            result[key] = legacy_detail(value)
        elif key in enums:
            result[key] = value if value in enums[key] else '[redacted]'
        elif key == 'tool' and value in {'create_task', 'update_task', 'create_project', 'update_project', 'add_comment', 'create_team', 'delegate_task'}:
            result[key] = value
        elif value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        else:
            result[key] = '[redacted]'
    return result


def safe_detail(detail):
    # Keys and values are both allowlisted; arbitrary strings cannot sneak into logs.
    result = {}
    for key, value in detail.items():
        if key in {'execution_enabled', 'calls', 'failures', 'duration_ms', 'executions', 'execution_failures'} and isinstance(value, (bool, int)):
            result[key] = value
        elif key == 'outcome' and value in ('succeeded', 'failed', 'narrated', 'proposed', 'completed', 'inconclusive', 'started'):
            result[key] = value
        elif key == 'provider_mode' and value in ('deterministic', 'tool_calling'):
            result[key] = value
        elif key in {'before', 'after', 'from_status', 'to_status'} and value in ('admin', 'manager', 'member', 'todo', 'backlog', 'in_progress', 'review', 'done', None):
            result[key] = value
    return result


def audit(db, org_id, actor_id, entity_type, entity_id, action, **detail):
    context = _context.get()
    actor = db.get(User, actor_id) if actor_id else None
    source, effective_actor, initiator = context or (
        'agent' if actor and actor.kind == 'agent' else 'human' if actor else 'system',
        actor_id, actor_id if actor and actor.kind == 'human' else None,
    )
    row = AuditEvent(id=uid(), organization_id=org_id, actor_id=effective_actor, initiator_id=initiator,
                     source=source, entity_type=entity_type, entity_id=entity_id, action=action,
                     detail=safe_detail(detail))
    db.add(row)
    logger.info('audit_event', extra={'event': 'audit_event', 'source': source,
        'outcome': row.detail.get('outcome'), 'provider_mode': row.detail.get('provider_mode'),
        'run_id': _run_id.get(), 'correlation_id': row.id})
    return row


def execution_allowed(db, agent):
    # Independent connection, not identity-map refresh: also breaks a stale transaction
    # snapshot on repeatable-read databases. Never use User.active for this switch.
    with Session(bind=db.get_bind()) as fresh:
        return bool(fresh.scalar(select(User.id).join(Organization, Organization.id == User.organization_id).where(
            User.id == agent.id, User.kind == 'agent', User.organization_id == agent.organization_id,
            User.execution_enabled.is_(True), Organization.execution_enabled.is_(True))))


def require_execution(db, agent):
    from .copilot import CopilotProviderError
    if not execution_allowed(db, agent):
        raise CopilotProviderError('Agent execution is disabled')


@contextmanager
def execution_run(tools):
    run = ExecutionRun(id=uid(), organization_id=tools.user.organization_id,
                       agent_id=tools.user.id, outcome='started', failed=False)
    attribution = tool_attribution(tools)
    token = _run_id.set(run.id)
    try:
        with Session(bind=tools.db.get_bind()) as ledger, audit_context(*attribution):
            ledger.add(run)
            audit(ledger, run.organization_id, run.agent_id, 'execution_run', run.id, 'execution_started', outcome='started')
            ledger.commit()
            ledger.refresh(run)
            ledger.expunge(run)
        try:
            yield run
        except Exception:
            run.failed, run.outcome = True, 'failed'
            raise
        finally:
            with Session(bind=tools.db.get_bind()) as ledger, audit_context(*attribution):
                ledger.merge(run)
                audit(ledger, run.organization_id, run.agent_id, 'execution_run', run.id, 'execution_finished', outcome=run.outcome)
                ledger.commit()
    finally:
        _run_id.reset(token)


def scheduler_failure(bind, org_id='__system__', actor_id=None):
    correlation_id = uid()
    logger.error('scheduler_error', extra={'event': 'scheduler_error', 'source': 'scheduler',
        'outcome': 'failed', 'failed': True, 'correlation_id': correlation_id})
    try:
        with Session(bind=bind) as ledger, audit_context('scheduler', actor_id):
            audit(ledger, org_id, actor_id, 'scheduler', correlation_id, 'scheduler_failed', outcome='failed')
            ledger.commit()
    except Exception:
        # A ledger outage must not kill the scheduler or disclose driver errors.
        logger.error('scheduler_error', extra={'event': 'scheduler_error', 'source': 'scheduler',
            'outcome': 'failed', 'failed': True, 'correlation_id': correlation_id})


def tool_attribution(tools):
    user = tools.user
    return _context.get() or ('agent' if user.kind == 'agent' else 'copilot',
                              user.id, user.id if user.kind == 'human' else None)


def tool_audit(tools, entity_type, action, **detail):
    with Session(bind=tools.db.get_bind()) as ledger, audit_context(*tool_attribution(tools)):
        audit(ledger, tools.user.organization_id, tools.user.id, entity_type, tools.user.id, action, **detail)
        ledger.commit()


def provider_call(tools, mode, invoke):
    """Persist each actual invocation independently of business transaction rollback."""
    source, actor_id, initiator_id = tool_attribution(tools)
    started = time.monotonic()
    failed = False
    try:
        return invoke()
    except Exception as exc:
        failed = True
        from .copilot import CopilotProviderError
        raise CopilotProviderError("The configured AI provider is unavailable. Try again later.") from None
    finally:
        duration_ms = max(0, int((time.monotonic()-started)*1000))
        with Session(bind=tools.db.get_bind()) as ledger, audit_context(source, actor_id, initiator_id):
            call = ProviderCall(organization_id=tools.user.organization_id,
                actor_id=actor_id, initiator_id=initiator_id, source=source,
                agent_id=tools.user.id if tools.user.kind == 'agent' else None,
                provider_mode=mode, failed=failed, duration_ms=duration_ms, run_id=_run_id.get())
            ledger.add(call); ledger.flush()
            call_id = call.id
            audit(ledger, call.organization_id, actor_id, 'provider_call', call.id,
                  'call_failed' if failed else 'call_succeeded', outcome='failed' if failed else 'succeeded', provider_mode=mode)
            ledger.commit()
        logger.info('provider_call', extra={'event': 'provider_call', 'source': source,
            'provider_mode': mode, 'failed': failed, 'outcome': 'failed' if failed else 'succeeded',
            'run_id': _run_id.get(), 'correlation_id': call_id})


@event.listens_for(AuditEvent, 'before_update')
@event.listens_for(AuditEvent, 'before_delete')
def immutable_audit(mapper, connection, target):
    raise ValueError('Audit events are append-only')


# --------------------------------------------------------------------------- #
# Enterprise audit aggregation (materialized per-day/per-action counts for
# enterprise_routes.audit_aggregation, the SIEM/export surface).
#
# Deliberately NOT written synchronously inside audit() above. That was tried
# first and reliably broke things on SQLite: an extra write on every audited
# request, whether inline in the caller's own transaction or on a second
# session, either corrupted the caller's transaction state (SAVEPOINT
# support needs event-listener workarounds this engine does not register for
# pysqlite) or self-deadlocked (SQLite allows only one writer for the whole
# file at a time; a second connection's write blocks on a lock only the
# still-running caller could release). Both were reproduced and are not
# theoretical. Postgres does not have this problem, but this app runs on
# both, and correctness cannot depend on which one is in front of it.
#
# Instead this recomputes from the real AuditEvent table on a periodic tick,
# the same shape as privacy_service.py's retention cleanup loop (advisory-
# locked for multi-replica safety, off by default, real GROUP BY count so a
# missed or repeated tick just reproduces the same correct totals -- not an
# incremental patch that could drift). The aggregation is eventually
# consistent (lagging by up to one tick), not real-time; SIEM/export use
# cases tolerate that far better than they would tolerate the audit trail
# itself becoming unreliable.
# --------------------------------------------------------------------------- #

def refresh_audit_aggregations(db: Session, *, organization_id: str | None = None) -> int:
    from .enterprise_models import EnterpriseAuditAggregation
    # func.date() renders as each dialect's native DATE()/::date equivalent
    # (verified against both SQLite and real Postgres) and returns the same
    # "YYYY-MM-DD" shape EnterpriseAuditAggregation.date_bucket already uses.
    bucket_expr = func.date(AuditEvent.created_at)
    stmt = select(AuditEvent.organization_id, AuditEvent.action, bucket_expr.label('bucket'), func.count().label('total')
                 ).group_by(AuditEvent.organization_id, AuditEvent.action, bucket_expr)
    if organization_id is not None:
        stmt = stmt.where(AuditEvent.organization_id == organization_id)
    touched = 0
    now = datetime.now(timezone.utc)
    for org_id, action, bucket, total in db.execute(stmt):
        if not org_id or org_id == _NO_TENANT or not bucket:
            continue  # not a real organizations.id -- the table FKs to it
        # func.date() returns a str on SQLite (no native DATE type) but a
        # real datetime.date on Postgres -- normalize to the "YYYY-MM-DD"
        # string date_bucket has always stored, on both dialects.
        bucket = bucket.isoformat() if hasattr(bucket, "isoformat") else bucket
        where = (
            EnterpriseAuditAggregation.organization_id == org_id,
            EnterpriseAuditAggregation.action == action,
            EnterpriseAuditAggregation.date_bucket == bucket,
        )
        result = db.execute(
            sa_update(EnterpriseAuditAggregation).where(*where).values(count=total, last_updated=now)
        )
        if not result.rowcount:
            db.add(EnterpriseAuditAggregation(organization_id=org_id, action=action,
                                              date_bucket=bucket, count=total, last_updated=now))
        touched += 1
    db.commit()
    return touched


AUDIT_AGGREGATION_INTERVAL_SECONDS = float(os.getenv("AUDIT_AGGREGATION_INTERVAL_SECONDS", "0"))
_AGGREGATION_LOCK_NAMESPACE = 0x454153  # distinct from execution.py's/privacy_service.py's own namespaces
_aggregation_background_task: asyncio.Task | None = None


def _with_aggregation_lock(engine, fn) -> None:
    """Same advisory-lock shape as execution.agent_step_lock /
    privacy_service._with_cleanup_lock: SQLite always grants (it cannot be
    serving the multi-replica deployment this guards)."""
    if engine.dialect.name != "postgresql":
        fn()
        return
    connection = engine.connect()
    acquired = False
    try:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:ns, 1)"), {"ns": _AGGREGATION_LOCK_NAMESPACE}).scalar())
        if acquired:
            fn()
    finally:
        if acquired:
            connection.execute(text("SELECT pg_advisory_unlock(:ns, 1)"), {"ns": _AGGREGATION_LOCK_NAMESPACE})
        connection.close()


def run_aggregation_tick() -> None:
    from .database import SessionLocal, engine
    def _tick():
        with SessionLocal() as db:
            refresh_audit_aggregations(db)
    _with_aggregation_lock(engine, _tick)


async def _aggregation_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_aggregation_tick)
        except Exception:
            pass  # a missed tick is retried next interval; never crash the app over it
        await asyncio.sleep(AUDIT_AGGREGATION_INTERVAL_SECONDS)


def start_aggregation_background_loop() -> None:
    global _aggregation_background_task
    if _aggregation_background_task is None and AUDIT_AGGREGATION_INTERVAL_SECONDS > 0:
        _aggregation_background_task = asyncio.ensure_future(_aggregation_loop())


def stop_aggregation_background_loop() -> None:
    global _aggregation_background_task
    if _aggregation_background_task is not None:
        _aggregation_background_task.cancel()
        _aggregation_background_task = None
