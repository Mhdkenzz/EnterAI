"""SSO / SCIM / Enterprise Identity models (Phase 14)."""
from datetime import datetime, timezone
from .database import Base
from sqlalchemy import (Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index,
                        JSON, String, Text, text)
from sqlalchemy.orm import Mapped, mapped_column

USERS_TABLE = "users"
PROVIDERS_TABLE = "identity_providers"

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
    domain_binding: Mapped[str] = mapped_column(String(255), index=True, nullable=True)
    # When domain_binding was last proven via a DNS TXT record
    # (app/domain_verification.py). NULL means never proven, and a binding
    # without a fresh timestamp admits neither OIDC login nor SCIM
    # provisioning. Global domain exclusivity is enforced by the unique index
    # on domain_binding below, not by application code, so it holds under
    # concurrent claims.
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sso_only: Mapped[bool] = mapped_column(Boolean, default=False)
    role_mapping_json: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    # A verified email domain belongs to exactly one ACTIVE, VERIFIED provider
    # on the instance. Enforced here rather than in application code so two
    # concurrent claims cannot both win: the database refuses the second.
    #
    # The predicate is load-bearing and was previously wrong. A bare
    # `domain_binding IS NOT NULL` reserved the domain for whoever TYPED it
    # first, verified or not, forever: any administrator could lock
    # `gmail.com` for every tenant on the instance by typing it, and deleting
    # the row elsewhere would not release it. Restricting to active + verified
    # rows means a claim only holds while it is actually in use -- which is
    # what "exclusivity" is supposed to mean -- and it matches
    # is_domain_verified()'s definition of a live claim exactly, so the index
    # and the application gate can never disagree.
    #
    # NULL bindings (the majority -- most providers never bind a domain) are
    # exempt, matching the SCIM mapping index above.
    # (id, organization_id) unique, so scim_user_mappings' composite
    # cross-organization FK can legally reference it. `id` is already the
    # primary key, so this asserts nothing new data-wise; it makes the
    # reference valid. Declared as an Index rather than a UniqueConstraint
    # because SQLite only honours a PRIMARY KEY or UNIQUE INDEX as a foreign
    # key parent. See
    # alembic/versions/0023_mapping_organization_alignment.py.
    __table_args__ = (
        Index("uq_identity_providers_id_organization",
              "id", "organization_id", unique=True),
        Index(
            "uq_identity_providers_domain_binding",
            "domain_binding",
            unique=True,
            sqlite_where=text(
                "domain_binding IS NOT NULL AND active "
                "AND domain_verified_at IS NOT NULL"),
            postgresql_where=text(
                "domain_binding IS NOT NULL AND active "
                "AND domain_verified_at IS NOT NULL"),
        ),
    )

class SCIMUserMapping(Base):
    __tablename__ = "scim_user_mappings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: __import__('uuid').uuid4().hex)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    scim_external_id: Mapped[str] = mapped_column(String(255), index=True)
    # Which identity provider owns this identity. Without it, org + external id
    # was the whole namespace, so two providers in one organization shared it:
    # a SCIM bearer token for provider A resolved, read, updated, deactivated
    # and listed identities belonging to provider B. OIDC provisions through
    # this same table, so this column is the owner for both protocols.
    #
    # Nullable on purpose. A legacy row predating provider ownership cannot be
    # attributed to a provider without guessing, and guessing would hand a
    # tenant's existing identities to whichever provider happened to be
    # created first. NULL instead means "unowned": every lookup requires an
    # exact owner match, so such a row fails closed and is re-provisioned by
    # its own IdP rather than silently exposed. See
    # alembic/versions/0021_scim_provider_ownership.py.
    identity_provider_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity_providers.id"), nullable=True, index=True)
    # A provider may not own the same external id twice. Enforced in the schema
    # rather than only in the query layer, so a second write path cannot create
    # the duplicate that would make `scim_mapping_for_provider` (which returns a
    # single row via db.scalar) silently pick one of the two.
    #
    # The index is partial on IS NOT NULL, which is what lets legacy NULL-owner
    # rows coexist: the backfill-free 0021 migration leaves them unowned, and
    # more than one of them sharing an external id is the expected end state,
    # not corruption. SQLite and PostgreSQL both honour this predicate.
    __table_args__ = (
        Index(
            "uq_scim_user_mappings_provider_external_id",
            "identity_provider_id",
            "scim_external_id",
            unique=True,
            sqlite_where=text("identity_provider_id IS NOT NULL"),
            postgresql_where=text("identity_provider_id IS NOT NULL"),
        ),
        # Durable cross-organization tenancy. `organization_id` had its own FK
        # to organizations, and user_id/identity_provider_id had their own
        # single-column FKs -- but nothing tied the three together, so a mapping
        # could name an organization while pointing at a user (or provider) in a
        # different one. Every read path re-checked by hand, which is a
        # correctness burden replicated per call site and exactly the check the
        # next write path forgets.
        #
        # A composite FK requires the parent to be unique on (id,
        # organization_id); those constraints are declared in
        # alembic/versions/0023_mapping_organization_alignment.py, which also
        # quarantines any historical row that would violate these.
        ForeignKeyConstraint(
            ["user_id", "organization_id"], [f"{USERS_TABLE}.id", f"{USERS_TABLE}.organization_id"],
            name="fk_scim_mappings_user_org",
        ),
        ForeignKeyConstraint(
            ["identity_provider_id", "organization_id"],
            [f"{PROVIDERS_TABLE}.id", f"{PROVIDERS_TABLE}.organization_id"],
            name="fk_scim_mappings_provider_org",
        ),
    )
    scim_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
