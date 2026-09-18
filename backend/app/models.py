from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from .database import Base

def uid() -> str: return str(uuid4())
def now() -> datetime: return datetime.now(timezone.utc)

class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
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
    # an agent stops being polled for its current task once this hits the configured
    # threshold, until a human intervenes (message, reassignment). last_execution_at
    # drives the "agent is working" status shown in the hierarchy tree/inspector.
    consecutive_task_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_execution_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", name="fk_tasks_project_id_projects"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="todo")
    priority: Mapped[str] = mapped_column(String(20), default="medium")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reporter_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    labels: Mapped[list] = mapped_column(JSON, default=list)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
