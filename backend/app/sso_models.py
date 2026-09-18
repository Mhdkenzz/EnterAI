"""SSO / SCIM / Enterprise Identity models (Phase 14)."""
from datetime import datetime, timezone
from .database import Base
from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

def now() -> datetime:
    return datetime.now(timezone.utc)

class IdentityProvider(Base):
    __tablename__ = "identity_providers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    protocol: Mapped[str] = mapped_column(String(20), default="oidc")
    sso_url: Mapped[str] = mapped_column(String(500), nullable=True)
    acs_url: Mapped[str] = mapped_column(String(500), nullable=True)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=True)
    certificate_pem: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[str] = mapped_column(String(255), nullable=True)
    client_secret_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    # SCIM provisioning calls arrive from the IdP, not a browser session -- they
    # authenticate with this dedicated bearer token (hashed, like every other
    # token in tokens.py) rather than a normal user session JWT. Never populated
    # by client_secret; only /api/admin/sso/providers/{id}/scim-token mints one.
    scim_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    domain_binding: Mapped[str] = mapped_column(String(255), index=True, nullable=True)
    sso_only: Mapped[bool] = mapped_column(Boolean, default=False)
    role_mapping_json: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

class SCIMUserMapping(Base):
    __tablename__ = "scim_user_mappings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scim_external_id: Mapped[str] = mapped_column(String(255), index=True)
    scim_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
