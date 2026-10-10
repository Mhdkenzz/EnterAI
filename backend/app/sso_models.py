"""SSO / SCIM / Enterprise Identity models (Phase 14)."""
from datetime import datetime, timezone
from .database import Base
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

def now() -> datetime:
    return datetime.now(timezone.utc)

class IdentityProvider(Base):
    __tablename__ = "identity_providers"
    # At most one *verified* provider may hold a given email domain, platform-wide.
    # Unverified claims are unconstrained on purpose: anyone can type any domain into
    # domain_binding, and that must neither fail nor block the real owner. The partial
    # index (migration 0021) is what turns "first to verify wins" into a database
    # guarantee, so two orgs racing to verify the same domain cannot both succeed.
    __table_args__ = (
        Index("uq_identity_providers_verified_domain", "domain_binding", unique=True,
              postgresql_where=text("domain_verified_at IS NOT NULL"),
              sqlite_where=text("domain_verified_at IS NOT NULL")),
    )
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
    # Real, recoverable at-rest encryption (app/secrets_store.py) -- OIDC's
    # token exchange must resend the raw secret to the IdP, which a one-way
    # hash (client_secret_hash, above) cannot support.
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SCIM provisioning calls arrive from the IdP, not a browser session -- they
    # authenticate with this dedicated bearer token (hashed, like every other
    # token in tokens.py) rather than a normal user session JWT. Never populated
    # by client_secret; only /api/admin/sso/providers/{id}/scim-token mints one.
    scim_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # OIDC login (see sso_oidc.py). Endpoints are resolved from the issuer via
    # discovery, not hand-entered -- discovery_json/fetched_at cache that
    # document on the row so every replica shares one fetch.
    oidc_issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    oidc_discovery_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    oidc_discovery_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Normalized (lowercase, no trailing dot) by sso_auth.normalize_domain. A binding
    # only routes logins, and only vouches for SCIM/JIT email addresses, once the org
    # has proven it controls the domain: domain_verified_at is set by
    # POST /api/admin/sso/providers/{id}/verify-domain after the TXT record
    # _enterai-verification.<domain> = enterai-domain-verification=<token> is found.
    # Any change to domain_binding mints a new token and clears domain_verified_at.
    domain_binding: Mapped[str] = mapped_column(String(255), index=True, nullable=True)
    domain_verification_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
