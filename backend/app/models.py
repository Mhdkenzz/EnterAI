from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from .database import Base

def uid() -> str: return str(uuid4())
def now() -> datetime: return datetime.now(timezone.utc)
# Naive UTC for plain DateTime columns that are compared in queries: an aware
# value would be shifted by the server's zone on PostgreSQL sessions not set to UTC.
def utc_naive() -> datetime: return datetime.now(timezone.utc).replace(tzinfo=None)

class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    # Not unique: two unrelated customers may share a name, and a uniqueness error
    # would tell anyone signing up which companies are already customers.
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    ceo_full_access_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    agent_workspace_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

AGENT_HIERARCHY_LEVELS = ("ceo", "vp", "director", "senior_manager", "worker")

class User(Base):
    """A workspace member. `kind="agent"` rows are AI agents in the org chart -- they
    share this table (and its FKs from Task/Comment/Attachment) so an agent can be
    assigned work exactly like a human once that's wired up, rather than needing a
    parallel identity system. Agent-only columns are nullable/unused for humans."""
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="member")
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(12), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    kind: Mapped[str] = mapped_column(String(10), default="human")
    hierarchy_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parent_agent_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    current_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", name="fk_users_current_task_id_tasks", use_alter=True), nullable=True)
    last_completed_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", name="fk_users_last_completed_task_id_tasks", use_alter=True), nullable=True)
    # Autonomous execution (Phase 6): consecutive_task_failures is a circuit breaker --
    # repeated provider failures or narration without recorded progress block the
    # current task until a human intervenes. last_execution_at drives the "agent is
    # working" status shown in the hierarchy tree/inspector.
    consecutive_task_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_execution_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    daily_limit_reached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Bumped whenever credentials change. Session tokens carry the epoch they were
    # minted under, so a password reset silently invalidates every token issued
    # before it -- otherwise whoever prompted the reset keeps their stolen session.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Retention: retiring an agent (headcount shrink or an explicit admin decision)
    # never deletes it -- identity and all historical tasks/messages/comments/audit
    # rows must survive. `retired_at` is agent-only and orthogonal to `active`
    # (human account disable) and `execution_enabled` (scheduler on/off switch);
    # a retired agent is additionally forced execution_enabled=False, but the two
    # columns answer different questions and both are checked independently.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Human counterpart to `retired_at`: a deleted human account is anonymized in
    # place (name/email/password_hash scrubbed) rather than removed, for the same
    # reason -- tasks, comments, and audit rows that reference this user's id must
    # keep working and stay attributable to *someone*, not go orphaned or silently
    # reassign to a different real person's identity.
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tos_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(180))
    code: Mapped[str] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    health: Mapped[str] = mapped_column(String(20), default="on_track")
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", name="fk_projects_owner_id_users", use_alter=True), nullable=True)
    color: Mapped[str] = mapped_column(String(20), default="#7c3aed")
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ProjectDocument(Base):
    __tablename__ = "project_documents"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentAccessGrant(Base):
    """Explicit, revocable least-privilege access for one agent."""
    __tablename__ = "agent_access_grants"
    __table_args__ = (
        UniqueConstraint("organization_id", "agent_id", "resource_type", "resource_id", "permission", name="uq_agent_access_grant"),
        Index("ix_agent_access_grants_org_agent", "organization_id", "agent_id"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    resource_type: Mapped[str] = mapped_column(String(20))  # project | document | tool
    resource_id: Mapped[str] = mapped_column(String(255))
    permission: Mapped[str] = mapped_column(String(20))  # read | write | use
    granted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    revoked_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", name="fk_tasks_project_id_projects"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="todo")
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reporter_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    qa_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    review_rubric: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    qa_evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    qa_checks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    qa_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    peer_review_status: Mapped[str] = mapped_column(String(30), default="not_requested", server_default="not_requested")
    peer_review_agent_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", name="fk_tasks_peer_review_agent_id_users"), nullable=True)
    peer_review_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", name="fk_tasks_peer_review_task_id_tasks"), nullable=True)
    peer_review_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    peer_review_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

class TaskDependency(Base):
    """A task may begin only after its prerequisite task is complete."""
    __tablename__ = "task_dependencies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    depends_on_task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency_pair"),)

class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Decision(Base):
    """A durable, explicitly recorded project decision linked to its source discussion."""
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True)
    source_comment_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    __table_args__ = (UniqueConstraint("source_comment_id", name="uq_decisions_source_comment_id"),)

class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String(100))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, index=True)
    agent_id: Mapped[str] = mapped_column(String, index=True)
    outcome: Mapped[str] = mapped_column(String(20), default="started")
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, index=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    initiator_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String(20))
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    provider_mode: Mapped[str] = mapped_column(String(30))
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rounds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adapter_used: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(60), nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class AuditEvent(Base):
    """Append-only ledger; identity strings deliberately have no deletion FKs."""
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(String, index=True)
    actor_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    initiator_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[str] = mapped_column(String)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(220))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    href: Mapped[str | None] = mapped_column(String(255), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class InboundMessage(Base):
    """A durable source message awaiting human review or downstream planning.

    Provider IDs are unique within an organization and source, making retries from
    future polling/webhook adapters idempotent without leaking identity across orgs.
    The message body is retained as source material and must be treated as untrusted
    input by any agent that later summarizes or plans from it.
    """
    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("organization_id", "source", "external_id", name="uq_inbound_message_source_id"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    external_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sender: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive, index=True)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    triage_status: Mapped[str] = mapped_column(String(30), default="not_started", index=True)
    triage_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    triage_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_concerns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    triage_attempts: Mapped[int] = mapped_column(Integer, default=0)
    triage_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive)


class InboundPlan(Base):
    """Editable project/task proposal tied to its source request and approval."""
    __tablename__ = "inbound_plans"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    inbound_message_id: Mapped[str] = mapped_column(ForeignKey("inbound_messages.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    summary: Mapped[str] = mapped_column(Text)
    proposal: Mapped[dict] = mapped_column(JSON)
    planner_provider: Mapped[str] = mapped_column(String(40), default="deterministic")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IntegrationConnection(Base):
    """Org-scoped third-party account and encrypted credentials for background sync."""
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "external_account_id", name="uq_integration_org_provider_account"),
        Index(
            "uq_connected_slack_workspace", "external_account_id", unique=True,
            postgresql_where=text("provider = 'slack' AND status = 'connected'"),
            sqlite_where=text("provider = 'slack' AND status = 'connected'"),
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    provider: Mapped[str] = mapped_column(String(30))
    external_account_id: Mapped[str] = mapped_column(String(500))
    display_name: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="connected", index=True)
    granted_scopes: Mapped[list] = mapped_column(JSON, default=list)
    access_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    history_cursor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    connected_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive, onupdate=utc_naive)


class IntegrationOAuthState(Base):
    """Single-use short-lived OAuth state tied to one human and organization."""
    __tablename__ = "integration_oauth_states"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[str] = mapped_column(String(30))
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    code_verifier_ciphertext: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive)


class OrganizationAISettings(Base):
    """Organization-scoped hosted AI configuration. API keys are encrypted before
    persistence and are never returned by the settings API.
    """
    __tablename__ = "organization_ai_settings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(30), default="ifm")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(200), default="IFM/K2-Horizon-375B-A23B")
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_naive, onupdate=utc_naive)

class AgentMessage(Base):
    """Persistent per-agent conversation history, separate from the general Copilot's
    ephemeral per-request tool-calling transcript -- this is what backs the "chat with
    this agent" panel and survives across sessions."""
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    agent_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(10))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class HierarchyConfig(Base):
    """Per-organization shape of the agent org chart: how many agents exist at each
    level below the (single, implicit) CEO. Reconciling actual agent rows to match
    this config is Phase 4 work -- this table only stores the desired shape."""
    __tablename__ = "hierarchy_configs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), unique=True, index=True)
    vp_count: Mapped[int] = mapped_column(Integer, default=0)
    directors_per_vp: Mapped[int] = mapped_column(Integer, default=0)
    managers_per_director: Mapped[int] = mapped_column(Integer, default=0)
    workers_per_manager: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

class AuthToken(Base):
    """Single-use, short-lived tokens for password reset and email verification.

    Only a SHA-256 of the token is stored: the raw value exists in the recipient's
    inbox and nowhere else, so a database disclosure cannot be replayed into an
    account takeover. `used_at` makes redemption one-shot and `expires_at` bounds
    the window; both are checked at redemption, not at lookup.
    """
    __tablename__ = "auth_tokens"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(30), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class CopilotConfirmation(Base):
    """Durable issued-and-consumed state for Copilot write proposals.

    A token must be registered here when the server issues it, before it is
    returned to a client.  The unique hash then lets every API replica atomically
    move the same proposal from pending to consumed without retaining the raw
    signed token or its arguments.
    """
    __tablename__ = "copilot_confirmations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    subject_id: Mapped[str] = mapped_column(String, index=True)
    organization_id: Mapped[str] = mapped_column(String, index=True)
    tool: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Execution-time agent proposals remain reviewable after the worker tick ends.
    # The bearer token is encrypted at rest and only returned to an authorized
    # human in this organization while the proposal is pending.
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    label: Mapped[str | None] = mapped_column(String(240), nullable=True)
    details: Mapped[list | None] = mapped_column(JSON, nullable=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class Invite(Base):
    """An invitation to join one specific organization, at one specific role.

    The organization and role are fixed when the invite is created by an admin of
    that organization; acceptance never reads them from the request, which is what
    stops an invite for one workspace being redeemed into another or escalated.
    """
    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(30), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DocumentChunk(Base):
    """A text chunk from a ProjectDocument with its vector embedding for RAG retrieval.

    Each chunk belongs to exactly one document and inherits its organization/project
    ownership for tenancy enforcement. The embedding column uses pgvector for
    efficient cosine similarity search.
    """
    __tablename__ = "document_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("project_documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    # Provenance for the embedding column's own invariant ("every row shares one
    # dimension at a time") -- lets a future dimension change detect old/new rows
    # coexisting instead of only finding out when a pgvector query breaks.
    embedding_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class EmailOutbox(Base):
    """Mail that could not be delivered on the first attempt, awaiting retry.

    Only failures land here: a message that sends straight away is never stored,
    because bodies carry single-use links and the database otherwise keeps nothing
    but hashes of those. A row's body is cleared the moment it is delivered or
    given up on (the links inside have expired by then anyway)."""
    __tablename__ = "email_outbox"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent | abandoned
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: utc_naive())


class RevokedSession(Base):
    """Session tokens ended by sign-out before their natural expiry. A stolen copy of
    a signed-out token must stop working, not live on for the rest of its 7 days."""
    __tablename__ = "revoked_sessions"
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
