"""SSO / SCIM authentication, validation, role mapping, and deactivation (Phase 14)."""
import os
import re
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .deployment_config import is_deployment_environment
from .models import Organization, User
from .sso_models import IdentityProvider, SCIMUserMapping
from .services import log
from .tokens import hash_token, mint

# --- Domain ownership -------------------------------------------------------------
#
# domain_binding decides two security-relevant things: which provider a login for
# alice@corp.example is routed to, and which email addresses an IdP may vouch for
# (SCIM userName, OIDC `email`). Both are only safe if the org that configured the
# binding actually controls the domain, so a binding counts for nothing until the
# org publishes a random token in DNS and verify_provider_domain sees it.

VERIFICATION_LABEL = "_enterai-verification"
VERIFICATION_VALUE_PREFIX = "enterai-domain-verification="
# At least two labels of plain LDH hostname characters. Internationalized names must
# be sent as punycode; a single label ("com") is rejected so a binding can never be
# a whole TLD, which subdomain matching would turn into "every address under it".
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class DomainLookupError(Exception):
    """DNS could not be queried (timeout, SERVFAIL) -- distinct from "no such record"."""


def normalize_domain(value: str | None) -> str | None:
    """Canonical form stored in domain_binding: stripped, lowercase, no trailing dot.
    Everything that compares or indexes a domain goes through this, so "Corp.Example."
    and "corp.example" cannot be two different claims. Blank means "no binding"."""
    if value is None:
        return None
    return value.strip().lower().rstrip(".") or None


def is_valid_domain(domain: str) -> bool:
    return bool(_DOMAIN_RE.match(domain))


def new_domain_verification_token() -> str:
    return secrets.token_urlsafe(24)


def verification_record(domain: str, token: str) -> dict:
    """The exact TXT record an admin must publish."""
    return {"type": "TXT", "name": f"{VERIFICATION_LABEL}.{domain}", "value": f"{VERIFICATION_VALUE_PREFIX}{token}"}


def lookup_txt_records(name: str) -> list[str]:
    """TXT strings published at `name` (multi-string records are joined, as DNS
    clients conventionally do). The one place this module touches the network, kept
    tiny so tests monkeypatch it instead of faking DNS. A name with no TXT data is an
    empty list; only a failure to ask at all (timeout, SERVFAIL) raises."""
    import dns.exception
    import dns.resolver  # imported lazily: only the verify-domain route needs it
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=5.0)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as error:
        raise DomainLookupError(str(error)) from error
    return [b"".join(record.strings).decode("utf-8", "replace") for record in answers]


def domain_verification_skipped() -> bool:
    """Local development and CI have no DNS to publish into. Honoured only outside
    staging/production: in a deployment the variable is ignored, so a copy-pasted
    .env cannot switch the control off where it matters."""
    return (not is_deployment_environment()
            and os.getenv("SSO_SKIP_DOMAIN_VERIFICATION", "").strip().lower() in {"1", "true", "yes", "on"})


def verify_provider_domain(db: Session, idp: IdentityProvider) -> str:
    """Mark idp.domain_binding as proven (flushed, not committed). Returns "dns" or
    "skipped". Raises HTTPException: 400 (nothing bound / record not found / DNS
    unreachable), 409 (another provider already holds this domain).

    The conflict check runs *after* the DNS proof on purpose: someone who cannot
    publish the record learns only "not found", never whether a domain is taken.
    The partial unique index (migration 0021) backs the check for the race where two
    organizations prove the same domain at once."""
    domain = idp.domain_binding
    if not domain:
        raise HTTPException(400, "Set a domain_binding on this provider before verifying it")
    if not idp.domain_verification_token:
        # Providers bound before verification existed have no token yet.
        idp.domain_verification_token = new_domain_verification_token()
        db.commit()
        record = verification_record(domain, idp.domain_verification_token)
        raise HTTPException(400, f"Publish the TXT record {record['name']} = {record['value']}, then verify again")
    method = "skipped"
    if not domain_verification_skipped():
        record = verification_record(domain, idp.domain_verification_token)
        try:
            published = lookup_txt_records(record["name"])
        except DomainLookupError:
            # 400, not 503: this is "we could not confirm the record", which the admin
            # fixes by retrying -- and a 5xx here would page on-call for a resolver blip
            # (and trips CI's "no server errors" API fuzzing).
            raise HTTPException(400, "Could not query DNS right now, so the TXT record could not be checked. "
                                     "Try again in a few minutes")
        if record["value"] not in {value.strip() for value in published}:
            raise HTTPException(400, f"TXT record not found: publish {record['value']} at {record['name']} "
                                     "and retry once DNS has propagated")
        method = "dns"
    held = db.scalar(select(IdentityProvider.id).where(
        IdentityProvider.domain_binding == domain, IdentityProvider.domain_verified_at.is_not(None),
        IdentityProvider.id != idp.id))
    if held:
        raise HTTPException(409, "This domain is already verified by another identity provider")
    idp.domain_verified_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This domain is already verified by another identity provider")
    return method


def email_in_domain(email: str, domain: str | None) -> bool:
    """True if the address is at `domain` or any subdomain of it (alice@eng.corp.example
    qualifies for corp.example; alice@evilcorp.example does not -- the dot is part of the
    match). Subdomains are included because the org proved it controls the apex, and
    large companies routinely mail from regional/departmental subdomains."""
    if not domain or "@" not in email:
        return False
    host = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def require_scim_email_allowed(idp: IdentityProvider, email: str) -> None:
    """SCIM identities become local accounts keyed by email, and users.email is
    globally unique with login resolving by address alone. A provider may therefore
    only create or rename users within a domain its org has proven it owns -- else an
    org admin could mint (or rename onto) an address at someone else's company."""
    if not idp.domain_binding or idp.domain_verified_at is None:
        raise HTTPException(400, "SCIM provisioning requires this identity provider's domain to be verified first")
    if not email_in_domain(email, idp.domain_binding):
        raise HTTPException(400, "userName must be an email address within this identity provider's verified domain")


def is_last_active_admin(db: Session, user: User) -> bool:
    """True if `user` is an active human admin and no *other* active human admin is
    left in the organization -- the same invariant admin.py's update_role/set_active
    enforce for the admin panel ("An active human administrator is required"). IdP
    syncs must honour it too, or one bad group mapping locks the whole org out.

    Callers skip the change and audit it instead of failing, so one protected admin
    never aborts the rest of an IdP's sync. On PostgreSQL the org row is locked first
    (as admin.py does) so two concurrent syncs cannot each see the other admin as
    "still there"; SQLite serializes writers anyway."""
    if user.kind != "human" or user.role != "admin" or not user.active:
        return False
    db.flush()  # sessions here run autoflush=False; count what this request already changed
    if db.get_bind().dialect.name != "sqlite":
        db.scalar(select(Organization.id).where(Organization.id == user.organization_id).with_for_update())
    others = db.scalar(select(func.count()).select_from(User).where(
        User.organization_id == user.organization_id, User.kind == "human", User.active.is_(True),
        User.role == "admin", User.id != user.id))
    return not others


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


def apply_scim_role_change(db: Session, user: User, new_role: str, *, mapping: SCIMUserMapping,
                           actor_id: str | None, source_provider_id: str | None) -> bool:
    """Set user.role from an IdP-supplied mapping (SCIM create/PUT/PATCH). Returns
    True if the role changed. A demotion that would leave the org with zero active
    human admins is skipped and audited as scim_last_admin_protected instead -- see
    is_last_active_admin."""
    if user.role == new_role:
        return False
    before = user.role
    if before == "admin" and is_last_active_admin(db, user):
        log(db, user.organization_id, actor_id, "user", user.id, "scim_last_admin_protected",
            scim_mapping_id=mapping.id, scim_identity_provider_id=source_provider_id,
            before=before, attempted="role_change")
        return False
    user.role = new_role
    log(db, user.organization_id, actor_id, "user", user.id, "scim_role_updated",
        scim_mapping_id=mapping.id, scim_identity_provider_id=source_provider_id,
        before=before, after=new_role)
    return True


def deactivate_scim_user(db: Session, scim_mapping: SCIMUserMapping, actor_id: str | None = None,
                          source_provider_id: str | None = None) -> bool:
    """Returns True if the user was deactivated, False if the last-admin rule held
    the deactivation back (audited as scim_last_admin_protected; the mapping and the
    user are left exactly as they were, so SCIM reads still report them active)."""
    user = db.get(User, scim_mapping.user_id)
    if user and user.organization_id == scim_mapping.organization_id and user.active \
            and is_last_active_admin(db, user):
        # Mirrors admin.py set_active. An IdP deprovisioning the only admin (a
        # mistaken offboarding, a group wiped upstream) must not leave the org with
        # nobody who can administer it -- skipped and recorded, not an error.
        log(db, user.organization_id, actor_id, "user", user.id, "scim_last_admin_protected",
            scim_mapping_id=scim_mapping.id, scim_identity_provider_id=source_provider_id,
            scim_external_id=scim_mapping.scim_external_id, attempted="deactivate")
        return False
    # Immediate deactivation: set scim_active=False and deactivate local user.
    scim_mapping.scim_active = False
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
    return True


def scim_create_or_update(
    db: Session,
    org_id: str,
    scim_external_id: str,
    email: str | None,
    name: str | None,
    role_claim: str | None = None,
    actor_id: str | None = None,
    source_provider_id: str | None = None,
    *,
    idp: IdentityProvider,
) -> User:
    """`idp` is the provider whose SCIM token authenticated this call, and it alone
    supplies the role mapping and the email domain a new user must belong to.
    (This used to pick "the first active IdP in the org", so an org with two
    providers applied one's role mapping to the other's users.)"""
    if idp.organization_id != org_id:
        raise HTTPException(403, "Cross-organization access denied")
    source_provider_id = source_provider_id or idp.id
    # Strict org isolation: lookup by org + scim_external_id only.
    mapping = db.scalar(
        select(SCIMUserMapping)
        .where(SCIMUserMapping.organization_id == org_id,
               SCIMUserMapping.scim_external_id == scim_external_id)
    )
    if not mapping:
        if not email:
            raise HTTPException(400, "userName (an email address) is required to create a user")
        email = email.strip()
        # Only addresses the org has proven it owns; see require_scim_email_allowed.
        require_scim_email_allowed(idp, email)
        # users.email is globally unique (login resolves by address alone), so a
        # collision with *any* org's account is a clean 409, not an IntegrityError 500.
        if db.scalar(select(User.id).where(func.lower(User.email) == email.lower())):
            raise HTTPException(409, "Another account already uses this email")
        user = User(
            organization_id=org_id,
            name=name or email,
            email=email,
            role=resolve_role_mapping(idp, role_claim),
            active=True,
            kind="human",
        )
        # Minimal safe default: no raw password; SSO auth path handles login.
        user.password_hash = ""
        db.add(user)
        try:
            db.flush()
        except IntegrityError:  # lost a race with a concurrent create of the same address
            db.rollback()
            raise HTTPException(409, "Another account already uses this email")
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
            if role_claim is not None:
                apply_scim_role_change(db, user, resolve_role_mapping(idp, role_claim), mapping=mapping,
                                       actor_id=actor_id, source_provider_id=source_provider_id)
        else:
            # Cross-org mapping corruption guard
            raise HTTPException(status_code=409, detail="SCIM mapping conflicts with user organization")
    user_obj = db.get(User, mapping.user_id) if mapping else None
    if user_obj is None:
        raise HTTPException(status_code=500, detail="SCIM mapping creation failed")
    return user_obj
