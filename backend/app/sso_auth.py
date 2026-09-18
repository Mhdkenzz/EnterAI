"""SSO / SCIM authentication, validation, role mapping, and deactivation (Phase 14)."""
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Organization, User
from .sso_models import IdentityProvider, SCIMUserMapping
from .services import log
from .tokens import hash_token, mint

# Secret resolution for IdP token signatures: derived from JWT_SECRET so rotation is consistent.
SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")


def _derive_idp_secret(name: str) -> str:
    return hashlib.sha256(f"enterai:idp:{name}:{SECRET}".encode()).hexdigest()


def validate_org_isolation(db: Session, user: User, target_org_id: str) -> None:
    if user.organization_id != target_org_id:
        raise HTTPException(status_code=403, detail="Cross-organization access denied")


def issue_scim_token(db: Session, idp: IdentityProvider) -> str:
    """(Re)issue the bearer token the IdP uses to call /scim/v2/*. Only the hash is
    stored -- the raw value is returned once for the admin to paste into the IdP's
    SCIM configuration, exactly like tokens.py's other one-time secrets. Issuing a
    new one invalidates whatever token the IdP was using before."""
    raw, digest = mint()
    idp.scim_token_hash = digest
    return raw


def resolve_scim_identity_provider(db: Session, bearer_token: str) -> IdentityProvider | None:
    """The only way a caller may reach /scim/v2/* -- a normal user session JWT
    never resolves here, by construction: session tokens are signed with a
    different key and carry no scim_token_hash to match against."""
    digest = hash_token(bearer_token)
    return db.scalar(
        select(IdentityProvider).where(
            IdentityProvider.scim_token_hash == digest, IdentityProvider.active == True
        )
    )


def resolve_role_mapping(idp: IdentityProvider, idp_role_claim: str | None) -> str:
    mapping = idp.role_mapping_json or {}
    mapped = mapping.get(str(idp_role_claim or "")) if idp_role_claim else None
    # Default safe fallback: never escalate beyond member without explicit mapping.
    # A mapped value that isn't one of these three (typo, stale config, wrong case)
    # must fall back to "member" too, not pass through unchanged.
    allowed = {"admin", "manager", "member"}
    return mapped if mapped in allowed else "member"


def deactivate_scim_user(db: Session, scim_mapping: SCIMUserMapping, actor_id: str | None = None,
                          source_provider_id: str | None = None) -> None:
    # Immediate deactivation: set scim_active=False and deactivate local user.
    scim_mapping.scim_active = False
    user = db.get(User, scim_mapping.user_id)
    if user and user.organization_id == scim_mapping.organization_id:
        if user.active:
            user.active = False
            user.session_epoch = (user.session_epoch or 0) + 1  # retire existing JWT sessions
            # Audit the identity/admin change. actor_id must be a real users.id or
            # None -- Activity.actor_id is a foreign key to users.id (enforced by
            # Postgres; silently ignored by SQLite, which is exactly why this bug
            # only surfaced once tested against real Postgres). A SCIM-originated
            # deactivation has no human actor, so it is None; which IdP/mapping did
            # it goes in the detail payload instead.
            log(db, user.organization_id, actor_id,
                "scim_user", user.id, "scim_deprovisioned",
                scim_mapping_id=scim_mapping.id, scim_identity_provider_id=source_provider_id,
                scim_external_id=scim_mapping.scim_external_id)
        scim_mapping.last_synced_at = datetime.now(timezone.utc)


def scim_create_or_update(
    db: Session,
    org_id: str,
    scim_external_id: str,
    email: str | None,
    name: str | None,
    role_claim: str | None = None,
    actor_id: str | None = None,
    source_provider_id: str | None = None,
) -> User:
    # Strict org isolation: lookup by org + scim_external_id only.
    mapping = db.scalar(
        select(SCIMUserMapping)
        .where(SCIMUserMapping.organization_id == org_id,
               SCIMUserMapping.scim_external_id == scim_external_id)
    )
    idp = db.scalars(
        select(IdentityProvider)
        .where(IdentityProvider.organization_id == org_id, IdentityProvider.active == True)
    ).first()
    if not mapping:
        # Create user with strict isolation checks
        # Email uniqueness must not cross org; enforce within org if needed.
        user = User(
            organization_id=org_id,
            name=name or email or scim_external_id,
            email=email or f"scim-{scim_external_id}@enterai.local",
            role=resolve_role_mapping(idp, role_claim) if idp else "member",
            active=True,
            kind="human",
        )
        # Minimal safe default: no raw password; SSO auth path handles login.
        user.password_hash = ""
        db.add(user)
        db.flush()
        mapping = SCIMUserMapping(
            organization_id=org_id,
            user_id=user.id,
            scim_external_id=scim_external_id,
            scim_active=True,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(mapping)
        db.flush()
        # See deactivate_scim_user's comment: actor_id must be a real users.id or
        # None, never mapping.id.
        log(db, org_id, actor_id, "scim_user", user.id, "scim_provisioned",
            scim_mapping_id=mapping.id, scim_identity_provider_id=source_provider_id,
            scim_external_id=scim_external_id, role=user.role)
    else:
        user = db.get(User, mapping.user_id)
        if user and user.organization_id == org_id:
            mapping.scim_active = True
            if user.active is False and mapping.scim_active is True:
                # Reactivation: only if SCIM says active and mapping says active
                user.active = True
                user.session_epoch = (user.session_epoch or 0) + 1
            mapping.last_synced_at = datetime.now(timezone.utc)
            if name and user.name != name:
                user.name = name
            if idp and role_claim is not None:
                new_role = resolve_role_mapping(idp, role_claim)
                if user.role != new_role:
                    before = user.role
                    user.role = new_role
                    log(db, org_id, actor_id, "user", user.id, "scim_role_updated",
                        scim_mapping_id=mapping.id, scim_identity_provider_id=source_provider_id,
                        before=before, after=new_role)
        else:
            # Cross-org mapping corruption guard
            raise HTTPException(status_code=409, detail="SCIM mapping conflicts with user organization")
    user_obj = db.get(User, mapping.user_id) if mapping else None
    if user_obj is None:
        raise HTTPException(status_code=500, detail="SCIM mapping creation failed")
    return user_obj
