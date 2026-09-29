"""SSO / SCIM authentication, validation, role mapping, and deactivation (Phase 14)."""
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import domain_verification
from .models import User
from .sso_models import IdentityProvider, SCIMUserMapping
from .services import log
from .tokens import hash_token, mint

# OIDC's own mapping namespace (sso_oidc._OIDC_EXTERNAL_ID_PREFIX). Duplicated
# as a literal because sso_oidc imports this module, so importing the constant
# back would be a cycle.
_RESERVED_OIDC_PREFIX = "oidc:"

# The least-privileged role. Used wherever a full replace must resolve an
# absent role claim to something deterministic, so a demotion can never be
# silently dropped in favour of "whatever the account already had".
_SAFE_DEFAULT_ROLE = "member"


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


def flush_scim(db: Session) -> None:
    """Flush, translating a unique violation into the same 409 commit_scim gives.

    This is the missing half of commit_scim. It wraps only db.commit(), but the
    INSERTs in scim_create_or_update are pushed with db.flush() first -- and
    flush() raises the very same unique violation. The loser of a provisioning
    race therefore never reached commit_scim at all and got a raw
    IntegrityError, surfacing as a 500. That is precisely the outcome
    commit_scim's own docstring says it exists to prevent.

    Scoped by table name so an unrelated integrity error is not mislabelled as
    a conflict, and it rolls back so the session is not left poisoned.
    """
    try:
        db.flush()
    except sa.exc.IntegrityError as exc:
        db.rollback()
        text = str(getattr(exc, "orig", exc)).lower()
        if "email" in text or "scim" in text or "identity_providers" in text:
            raise HTTPException(
                status_code=409,
                detail="This identity conflicts with one provisioned concurrently; retry the request",
            ) from exc
        raise


def commit_scim(db: Session) -> None:
    """Commit a SCIM write, turning the unique-violation race into a 409.

    The preflight checks in scim_create_or_update and the INSERT that follows
    it are not in the same transaction, so two concurrent provisions of the
    same address can both pass the preflight and then collide on commit --
    users.email is globally unique, and the ownership index is unique too.
    Without this, the loser of that race gets a raw IntegrityError surfacing as
    a 500. Retrying the whole operation is not safe here (it would re-run
    side effects such as the audit log), so the race is reported honestly as a
    409 and the caller retries.

    Scoped by table so an unrelated integrity error is not mislabelled as a
    conflict.
    """
    try:
        db.commit()
    except sa.exc.IntegrityError as exc:
        db.rollback()
        text = str(getattr(exc, "orig", exc)).lower()
        if "email" in text or "scim" in text or "identity_providers" in text:
            raise HTTPException(
                status_code=409,
                detail="This identity conflicts with one provisioned concurrently; retry the request",
            ) from exc
        raise


def scim_mapping_for_provider(
    db: Session,
    org_id: str,
    scim_external_id: str,
    identity_provider_id: str | None,
) -> SCIMUserMapping | None:
    """The single owner-scoped lookup for a SCIM/OIDC identity.

    `org + scim_external_id` alone is not a namespace: two providers in one
    organization issue their own external ids, so that lookup let a SCIM
    bearer token for provider A resolve an identity belonging to provider B
    (and let B's provisioning hijack A's user). Every read and every mutation
    goes through here so the boundary is defined once.

    An unowned (NULL) mapping -- a legacy row from before provider ownership
    existed -- matches nothing. It is quarantined rather than handed to an
    arbitrary provider; see 0021_scim_provider_ownership.py.

    This is the SHARED lookup, used by OIDC as well as SCIM, so it does not
    police the `oidc:` namespace -- OIDC legitimately looks up its own
    prefixed ids here. The reserved-namespace guard for SCIM callers lives in
    scim_mapping_for_scim(), which wraps this.
    """
    if identity_provider_id is None:
        return None
    return db.scalar(
        select(SCIMUserMapping)
        .where(SCIMUserMapping.organization_id == org_id,
               SCIMUserMapping.scim_external_id == scim_external_id,
               SCIMUserMapping.identity_provider_id == identity_provider_id)
    )


def scim_mapping_for_scim(
    db: Session,
    org_id: str,
    scim_external_id: str,
    identity_provider_id: str | None,
) -> SCIMUserMapping | None:
    """The SCIM-facing lookup: provider-scoped, and namespace-guarded.

    `oidc:` (sso_oidc._OIDC_EXTERNAL_ID_PREFIX) is reserved for OIDC-provisioned
    mappings, which live in the same table. Every SCIM route resolves through
    here rather than the shared lookup, so a SCIM bearer token cannot address
    an OIDC identity by its prefixed id and reach it through the SCIM surface.

    Refused with 400 before any query: a reserved id is a client error, and
    rejecting it early keeps the response identical for existing and
    non-existent ids alike.
    """
    if scim_external_id and scim_external_id.startswith(_RESERVED_OIDC_PREFIX):
        raise HTTPException(
            status_code=400,
            detail="External id is in a reserved namespace and cannot be addressed by SCIM",
        )
    return scim_mapping_for_provider(
        db, org_id, scim_external_id, identity_provider_id)


def scim_create_or_update(
    db: Session,
    org_id: str,
    scim_external_id: str,
    email: str | None,
    name: str | None,
    role_claim: str | None = None,
    actor_id: str | None = None,
    source_provider_id: str | None = None,
    active: bool = True,
    full_replace: bool = False,
) -> User:
    # Strict org *and* provider isolation. The authenticated provider is the
    # only owner whose mapping this call may touch, so a PUT/POST for one IdP
    # can never resolve, mutate, or adopt another IdP's identity.
    #
    # The owner is validated before anything is written. A missing, foreign, or
    # deactivated source_provider_id used to be silently downgraded to "no role
    # mapping configured" and then persisted as SCIMUserMapping(identity_provider_id=None)
    # -- a row no provider can ever resolve, i.e. an orphan written by a current
    # write path rather than only by the 0021 migration's legacy handling. A
    # NULL owner is legitimate as migration data and nowhere else, so the only
    # correct answer here is to refuse.
    idp = None
    if source_provider_id is not None:
        idp = db.scalar(
            select(IdentityProvider).where(
                IdentityProvider.id == source_provider_id,
                IdentityProvider.organization_id == org_id,
                IdentityProvider.active == True)
        )
    if idp is None:
        # 403, not 400: the caller authenticated as a principal that is not a
        # live provider of this organization, which is an authorization failure.
        # Mirrors resolve_scim_identity_provider's 403-for-wrong-principal rule.
        raise HTTPException(
            status_code=403,
            detail="A live identity provider owned by this organization is required",
        )

    # The reserved `oidc:` namespace is checked FIRST, before the domain gate.
    # A client error should be reported as a client error: 'oidc:subject-1' is
    # not even a valid address, so the domain gate would otherwise reject it
    # as an out-of-domain email (403) and mask the actual reason. Ordering the
    # cheapest, most specific validation first keeps the response honest.
    if scim_external_id and scim_external_id.startswith(_RESERVED_OIDC_PREFIX):
        raise HTTPException(
            status_code=400,
            detail="External id is in a reserved namespace and cannot be addressed by SCIM",
        )

    # Account-existence oracle closure. This must run BEFORE
    # scim_mapping_for_scim and before any User.email lookup, because the
    # oracle is precisely the ability to tell "created" from "already exists"
    # for an arbitrary address. A provider may only provision inside an email
    # domain it has proven it owns (app/domain_verification.py), so a probe
    # like victim@othercorp.com is refused here with one fixed response --
    # identical whether or not that account exists -- and the probe never
    # reaches the email query that would differentiate the two cases.
    #
    # The update path is gated too: without this, an attacker holding provider
    # A's token could PUT to their own identity and re-point the address at a
    # foreign domain.
    #
    # `if email and ...` was the defect: an empty or malformed userName made
    # `email` falsy, so the whole gate was skipped and the caller fell through
    # to a synthetic `scim-<id>@enterai.local` row. The validator is now
    # unconditional, so there is no input that reaches the lookup ungated.
    try:
        email = domain_verification.assert_provisioning_address_allowed(idp, email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mapping = scim_mapping_for_scim(
        db, org_id, scim_external_id, source_provider_id)
    if not mapping:
        # Create user with strict isolation checks
        # Email uniqueness must not cross org; enforce within org if needed.
        # users.email is globally unique (login resolves by email alone with
        # no org disambiguation). Provider ownership means a POST from
        # provider B can no longer silently adopt provider A's mapping, so
        # this insert is reachable with an email another identity already
        # holds -- which would surface as a raw IntegrityError/500 from the
        # later commit. Fail with the same clean 4xx the PATCH username path
        # and sso_oidc.provision_or_update_user already use.
        if email and db.scalar(select(User).where(func.lower(User.email) == email.lower())):
            raise HTTPException(
                status_code=409,
                detail="Another account already uses this email and is not linked to this identity provider",
            )
        user = User(
            organization_id=org_id,
            name=name or email or scim_external_id,
            email=email or f"scim-{scim_external_id}@enterai.local",
            role=resolve_role_mapping(idp, role_claim) if idp else "member",
            # Honour the IdP's requested state on creation. This used to be
            # hardcoded True, so a POST carrying "active": false created a
            # sign-in-capable account for someone the IdP had already
            # suspended or pre-disabled.
            active=active,
            kind="human",
        )
        # Minimal safe default: no raw password; SSO auth path handles login.
        user.password_hash = ""
        db.add(user)
        flush_scim(db)
        mapping = SCIMUserMapping(
            organization_id=org_id,
            user_id=user.id,
            scim_external_id=scim_external_id,
            identity_provider_id=source_provider_id,
            scim_active=active,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(mapping)
        flush_scim(db)
        # See deactivate_scim_user's comment: actor_id must be a real users.id or
        # None, never mapping.id.
        log(db, org_id, actor_id, "scim_user", user.id, "scim_provisioned",
            scim_mapping_id=mapping.id, scim_identity_provider_id=source_provider_id,
            scim_external_id=scim_external_id, role=user.role)
    else:
        user = db.get(User, mapping.user_id)
        if user and user.organization_id == org_id:
            # Honour the requested state in BOTH directions. This block used to
            # set scim_active = True unconditionally, which meant a conforming
            # PUT carrying "active": false re-enabled a deprovisioned account
            # instead of leaving it off.
            if active:
                if not mapping.scim_active:
                    mapping.scim_active = True
                if user.active is False:
                    # Reactivation: only if SCIM says active and mapping says active
                    user.active = True
                    user.session_epoch = (user.session_epoch or 0) + 1
            elif mapping.scim_active or user.active:
                # Deprovisioning via POST/PUT. Reuse deactivate_scim_user so
                # the audit event, the session retirement, and the
                # organization check stay identical to the PATCH path rather
                # than being a second, subtly different implementation.
                deactivate_scim_user(db, mapping, actor_id=actor_id,
                                     source_provider_id=source_provider_id)
            mapping.last_synced_at = datetime.now(timezone.utc)
            # SCIM PUT is a full REPLACE: the submitted body is the resource,
            # so an attribute that was not submitted is CLEARED rather than left
            # behind. The v5 code guarded every write with a truthiness check
            # (`if name and ...`, `if role_claim is not None`), which meant an
            # explicit empty displayName was silently discarded and a PUT
            # carrying no roles kept the previous role -- a 200 that lied about
            # what it did. A demotion sent as "the user has no roles now" was
            # the concrete way that lost a revocation.
            #
            # `full_replace` is set by the PUT route only. POST/PATCH keep the
            # partial-update meaning they are supposed to have.
            if full_replace:
                if user.name != (name or ""):
                    before_name = user.name
                    user.name = name or ""
                    log(db, org_id, actor_id, "user", user.id,
                        "scim_display_name_updated",
                        scim_mapping_id=mapping.id,
                        scim_identity_provider_id=source_provider_id,
                        before=before_name, after=user.name)
                # Absent roles is a replace, so the least-privileged role is the
                # only defensible end state -- never "keep whatever it had".
                new_role = (resolve_role_mapping(idp, role_claim)
                            if role_claim is not None else _SAFE_DEFAULT_ROLE)
                if user.role != new_role:
                    before_role = user.role
                    user.role = new_role
                    log(db, org_id, actor_id, "user", user.id,
                        "scim_role_updated", scim_mapping_id=mapping.id,
                        scim_identity_provider_id=source_provider_id,
                        before=before_role, after=new_role)
            else:
                if name and user.name != name:
                    user.name = name
                if idp and role_claim is not None:
                    new_role = resolve_role_mapping(idp, role_claim)
                    if user.role != new_role:
                        before = user.role
                        user.role = new_role
                        log(db, org_id, actor_id, "user", user.id,
                            "scim_role_updated",
                            scim_mapping_id=mapping.id,
                            scim_identity_provider_id=source_provider_id,
                            before=before, after=new_role)
            # SCIM PUT is a full replace, so a submitted userName IS a request to
            # change the address. This used to be ignored outright, which left
            # an IdP unable to correct a renamed person's address. The address
            # has already passed the strict validator and the provider domain
            # gate above, so this is a rename within the provider's own domain
            # and nothing more. users.email is globally unique, so the
            # collision check is a real constraint, not a formality.
            if email and user.email != email:
                taken = db.scalar(select(User).where(
                    func.lower(User.email) == email.lower(), User.id != user.id))
                if taken is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Another account already uses this email and is "
                               "not linked to this identity provider")
                before = user.email
                user.email = email
                log(db, org_id, actor_id, "user", user.id, "scim_email_updated",
                    scim_mapping_id=mapping.id,
                    scim_identity_provider_id=source_provider_id,
                    before=before, after=email)
        else:
            # Cross-org mapping corruption guard
            raise HTTPException(status_code=409, detail="SCIM mapping conflicts with user organization")
    user_obj = db.get(User, mapping.user_id) if mapping else None
    if user_obj is None:
        raise HTTPException(status_code=500, detail="SCIM mapping creation failed")
    return user_obj
