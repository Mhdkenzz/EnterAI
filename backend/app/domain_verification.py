"""Verified, globally exclusive email-domain ownership.

Why this module exists
----------------------
A SCIM POST is synchronous: the caller must be told whether an account was
created or already exists. Any synchronous endpoint that provisions by email
therefore carries an account-existence oracle unless the *question* is removed.
Error-message wording cannot fix this -- the status code alone still answers it.

The fix is ownership: a provider may only provision an identity inside an email
domain it has proven it controls, by publishing a DNS TXT record. A caller
probing `victim@someothercorp.com` is refused before any `User.email` lookup
runs, so the response is identical whether or not that account exists. The
question is gone rather than the answer hidden.

Why DNS TXT
-----------
`IdentityProvider.domain_binding` was previously a free-text string an
administrator could simply type, which made it an assertion rather than a
proof. Domain ownership in the world is demonstrated by DNS, so DNS is what we
require: it is the real-world control check, it needs no outbound HTTP, and
`dnspython` is already a dependency.

Deliberate policy choices
-------------------------
* Exclusivity is **global**, not per-organization. A domain may belong to
  exactly one provider anywhere on the instance. Consequence, stated plainly:
  two providers inside ONE organization cannot both serve a multi-domain
  domain; that case is refused rather than weakening the invariant. It is
  enforced by a unique index, not application code, so it holds under races.
* Verification **expires**. A domain whose TXT record is removed must stop
  working, rather than staying wired up forever.
* Verification is *checked at use*, not cached at verify time, so revoking or
  deactivating a provider takes effect immediately.
* Resolver failures fail **closed**: a timeout is a refusal, never a success.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import dns.resolver
import sqlalchemy as sa

_timedelta = timedelta

# A verification older than this is treated as stale.
VERIFICATION_TTL = timedelta(days=90)

# The single canonical definition of "old enough that this claim is no longer
# live". Every caller computes freshness through this one function.
#
# It exists as a named threshold rather than being inlined because a partial
# UNIQUE index cannot call a non-immutable function such as now(): PostgreSQL
# rejects that outright, and SQLite would evaluate it once at index-build time
# rather than per row, which is a different (and wrong) answer. So the index
# cannot itself express expiry -- the database has no way to know which of its
# rows are stale.
#
# The consequence is that an EXPIRED row still occupies the exclusive slot and
# no legitimate re-claim can ever win it. The fix is not a cleverer index; it
# is to revoke expired claims explicitly before contending for the domain. See
# revoke_expired_claims() and sso_routes.domain_verification_verify.
def fresh_threshold(now: datetime | None = None) -> datetime:
    """The oldest verification timestamp that still counts as a live claim."""
    return (now or utcnow()) - VERIFICATION_TTL


TXT_PREFIX = "enterai-domain-verification="
# Google Workspace publishes a randomized selector; look there first, then bare.
TXT_SELECTORS = ("_enterai-verification.", "")


class DNSResolutionError(RuntimeError):
    """The TXT record could not be read. Always treated as 'not verified'."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ago(**kwargs) -> datetime:
    """A timestamp in the past, e.g. ago(days=400) for a stale verification."""
    return utcnow() - _timedelta(**kwargs)


def normalize_domain(domain: str | None) -> str:
    """One canonical spelling per domain.

    Case, surrounding whitespace and a trailing DNS root dot must all collapse
    to the same form, otherwise two providers could claim one domain by
    spelling it differently and the uniqueness index would not fire.
    """
    if domain is None:
        raise ValueError("domain is required")
    candidate = domain.strip().rstrip(".").lower()
    if not candidate:
        raise ValueError("domain is required")
    if " " in candidate or "\t" in candidate or "\n" in candidate:
        raise ValueError("domain must not contain whitespace")
    if candidate.count("@") or "@" in candidate:
        raise ValueError("domain must not contain '@'")
    if "." not in candidate or candidate.startswith(".") or candidate.endswith("."):
        raise ValueError("not a valid domain: " + domain)
    labels = candidate.split(".")
    if any(not label for label in labels):
        raise ValueError("not a valid domain: " + domain)
    if any(len(label) > 63 for label in labels):
        raise ValueError("not a valid domain: " + domain)
    if len(candidate) > 253:
        raise ValueError("not a valid domain: " + domain)
    if any(not (label[0].isalnum() and label[-1].isalnum()) or
           any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in label)
           for label in labels):
        raise ValueError("not a valid domain: " + domain)
    return candidate


def email_domain(email_or_domain: str | None) -> str:
    """The domain part of an address, normalized.

    Accepts a bare domain too, because the same gate is asked both questions
    ("is this address mine?" when provisioning, "is this domain mine?" when
    routing a login) and a bare domain has no local part to strip.
    """
    if not email_or_domain:
        raise ValueError("an email address or domain is required")
    candidate = email_or_domain.strip()
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    return normalize_domain(candidate)


def email_matches_domain(email: str | None, domain: str) -> bool:
    """True when `email` sits inside `domain`.

    This previously called `normalize_domain(email)` on a FULL address.
    normalize_domain rejects any '@', so that call raised for every real
    address and the function could only ever return False. It was dead code
    with no caller, so nothing caught it. Comparing the two halves separately
    is what comparing an address to a domain actually means.
    """
    try:
        normalized = normalize_provisioning_email(email)
        return normalized.rsplit("@", 1)[1] == normalize_domain(domain)
    except ValueError:
        return False


def assert_provisioning_address_allowed(provider, email: str | None) -> str:
    """The single gate for every provisioning/mutation write path.

    Runs, in this order, and raises ValueError on any failure:

      1. the strict address validator, then
      2. the verified-provider domain check.

    Both happen BEFORE the caller touches a User or a mapping, which is the
    whole point: the refusal must not depend on whether the address is already
    taken, or it becomes an account-existence oracle.

    One error string for every rejection. Distinguishable refusals are
    distinguishable information, and the caller maps all of them to a single
    stable 4xx anyway.

    Returns the normalized address on success, so callers store exactly what
    was validated rather than re-deriving it.
    """
    normalized = normalize_provisioning_email(email)
    if not provider_may_use_domain(provider, normalized):
        raise ValueError(
            "provisioning is limited to the verified email domain of this "
            "identity provider")
    return normalized


def normalize_email(email: str | None) -> str:
    if not email or "@" not in email:
        raise ValueError("not an email address")
    local, domain = email.rsplit("@", 1)
    return f"{local.strip()}@{normalize_domain(domain)}"


# Characters permitted unescaped in an address local part (RFC 5322 atext).
# Dot-atom only: a quoted string is legal SMTP but never appears in a directory
# userName, and accepting one would make the stored address ambiguous.
_LOCAL_ATEXT = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!#$%&'*+-/=?^_`{|}~."
)


def normalize_provisioning_email(email: str | None) -> str:
    """The ONE strict validator for provisioning and mutation (SCIM POST/PUT/
    PATCH userName, OIDC claims).

    Deliberately separate from `email_domain()` above. That function parses a
    login-ROUTING hint, where a bare domain is the expected input and
    leniency is harmless -- nobody is being created from it. Provisioning is
    the opposite: this string becomes `users.email`, the value every login
    resolves by. Reusing the routing parser there meant an empty `userName`
    passed the `if email and ...` domain gate entirely, skipped straight to a
    synthetic `scim-<id>@enterai.local` row.

    Rejects, with ValueError: None, empty, whitespace-only, a bare domain
    (no local part), an address with no domain, more than one '@', an empty
    local part, an empty domain, whitespace anywhere, a local part that is not
    a dot-atom (leading/trailing/doubled dot), and any domain
    `normalize_domain()` refuses.

    Returns the normalized address. Callers turn the ValueError into one stable
    4xx -- never a per-case message, because a distinguishable refusal is
    distinguishable information.
    """
    if email is None:
        raise ValueError("an email address is required")
    if not isinstance(email, str):
        raise ValueError("an email address is required")
    candidate = email.strip()
    if not candidate:
        raise ValueError("an email address is required")
    if candidate.count("@") != 1:
        # 0 means a bare local part or a bare domain; 2+ is malformed. Both are
        # refused with the same error so the refusal says nothing about which.
        raise ValueError("a single '@' is required in an email address")
    local, domain_part = candidate.rsplit("@", 1)
    if not local:
        raise ValueError("an email address requires a local part")
    if not domain_part:
        raise ValueError("an email address requires a domain")
    if any(ch.isspace() for ch in candidate):
        raise ValueError("an email address must not contain whitespace")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        raise ValueError("malformed local part in email address")
    if not all(ch in _LOCAL_ATEXT for ch in local):
        raise ValueError("malformed local part in email address")
    if len(local) > 64:
        raise ValueError("local part of an email address is too long")
    # A trailing root dot is a legal DNS spelling but not a legal address
    # domain here, and normalize_domain would strip it -- producing an address
    # that does not match the string the client sent.
    if domain_part.endswith("."):
        raise ValueError("a fully qualified domain must not end with a dot")
    return f"{local}@{normalize_domain(domain_part)}"


def is_valid_provisioning_email(email: str | None) -> bool:
    """Boolean form, for call sites that only need the yes/no."""
    try:
        normalize_provisioning_email(email)
    except ValueError:
        return False
    return True


def expected_txt_value(domain: str) -> str:
    """The full TXT value the administrator must publish, prefix included.

    Includes the `enterai-domain-verification=` prefix deliberately: this is the
    complete string to paste into the DNS record, and it is what verify_domain
    compares against. Callers must not prepend the prefix again.
    """
    normalized = normalize_domain(domain)
    digest = hashlib.sha256(f"enterai:{normalized}".encode()).hexdigest()
    return f"enterai-domain-verification={digest}"


def txt_record_name(domain: str) -> str:
    return f"_enterai-verification.{normalize_domain(domain)}"


def lookup_txt(name: str) -> list[str]:
    """Read TXT records. Overridden in tests; no test does real DNS.

    Raises DNSResolutionError on any resolver problem so callers can fail
    closed rather than reading a transport failure as an empty (and therefore
    negative-but-unverified) result.
    """
    try:
        answers = dns.resolver.resolve(name, "TXT")
    except Exception as exc:  # NXDOMAIN, NoAnswer, timeout, network error
        raise DNSResolutionError(f"TXT lookup failed for {name}: {exc}") from exc
    values = []
    for record in answers:
        try:
            values.append(b"".join(record.strings).decode("utf-8"))
        except Exception as exc:
            raise DNSResolutionError(f"undecodable TXT record for {name}: {exc}") from exc
    return values


def verify_domain(domain: str, resolver=None) -> bool:
    """True only when the expected TXT value is published. Any failure -> False."""
    normalized = normalize_domain(domain)
    expected = expected_txt_value(normalized)
    read = resolver or lookup_txt
    for selector in TXT_SELECTORS:
        name = f"{selector}{normalized}"
        try:
            values = read(name)
        except DNSResolutionError:
            continue
        except Exception:
            # An unexpected resolver error must not be read as success.
            continue
        for value in values or []:
            if value.strip() == expected:
                return True
    return False


def is_domain_verified(provider) -> bool:
    """Whether this provider currently holds a usable, fresh claim.

    Requires: an active provider, a domain binding, and a verification
    timestamp that has not expired.

    Exclusivity is NOT checked here and is not this function's job: it is
    enforced by the unique index on identity_providers.domain_binding, so a
    provider holding this row's domain is by construction the only one that
    can. Re-deriving that in Python would be a weaker duplicate of a database
    guarantee that holds under concurrent claims.

    Deliberately does NOT re-query DNS -- doing so on every request would make
    login depend on a network round trip. The TTL plus the explicit re-verify
    endpoint is the trade-off. The check is performed at use, so revocation,
    domain change, and deactivation all take effect at once.
    """
    if provider is None:
        return False
    if not getattr(provider, "active", False):
        return False
    if not getattr(provider, "domain_binding", None):
        return False
    verified_at = getattr(provider, "domain_verified_at", None)
    if verified_at is None:
        return False
    if verified_at.tzinfo is None:
        # The column is a naive DateTime, so a value read back from the
        # database has no tzinfo. Treat it as UTC -- the value was written by
        # utcnow() -- rather than letting the subtraction below raise or
        # silently compare a naive and an aware datetime.
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    if verified_at < fresh_threshold():
        return False
    if verified_at > utcnow() + _timedelta(days=1):
        # A timestamp in the future is not trustworthy; treat as unverified.
        return False
    return True


def revoke_expired_claims(db, canonical_domain: str) -> int:
    """Clear the proof on every EXPIRED claim for this canonical domain.

    The reason this is necessary rather than clever: the exclusivity index is
    partial on `domain_verified_at IS NOT NULL`, and a partial index cannot
    call a non-immutable function. So "verified" in the index means "at some
    point verified", never "still verified" -- and an administrator whose proof
    aged out keeps occupying the exclusive slot forever. There is no route that
    releases it, so the domain is permanently unclaimable by anyone including
    them. An expired claim holding a live domain hostage is an availability bug
    that looks like a security control.

    Clearing the timestamp is strictly safe: it can only REMOVE a claim, never
    grant one, and an unverified provider cannot provision. The row stays, so
    nothing about the tenant is lost -- re-verification re-derives the proof
    from DNS.

    Scoped to one canonical domain so an unrelated live claim is never
    disturbed, and it deliberately excludes the caller's own row.

    Returns the number of rows released.
    """
    # The binding is normalized first: a raw column value and the caller's
    # canonical form must be compared as the same domain, or this silently
    # matches nothing and the bug returns.
    try:
        canonical = normalize_domain(canonical_domain)
    except ValueError:
        return 0
    result = db.execute(
        sa.update(_provider_model())
        .where(
            _provider_model().domain_binding == canonical,
            _provider_model().domain_verified_at.isnot(None),
            _provider_model().domain_verified_at < fresh_threshold(),
        )
        .values(domain_verified_at=None)
    )
    return int(result.rowcount or 0)


def _provider_model():
    from .sso_models import IdentityProvider
    return IdentityProvider


def live_verified_domain_clause(column=None):
    """A SQL predicate for "this row is a live claim".

    Used by OIDC routing so it can select a usable provider in the query rather
    than fetching an arbitrary matching row and rejecting it afterwards. The
    latter is wrong: with a duplicate row present, the arbitrary row is often
    the unverified one, and a valid claimant is then denied login for the whole
    domain.

    Mirrors is_domain_verified() exactly -- active, bound, verified, not
    expired -- so routing and provisioning can never disagree about what a live
    claim is.
    """
    model = _provider_model()
    binding = model.domain_binding if column is None else column
    return sa.and_(
        model.active == True,  # noqa: E712 -- SQLAlchemy column comparison
        binding.isnot(None),
        model.domain_verified_at.isnot(None),
        model.domain_verified_at >= fresh_threshold(),
    )


def provider_may_use_domain(provider, email_or_domain: str) -> bool:
    """The single gate every provisioning and routing path consults.

    Accepts either a bare domain or a full address, because callers have both:
    provisioning passes the identity's email, routing passes a domain. Both
    reduce to one comparison of the normalized domain parts -- comparing whole
    strings would be wrong, since the local part of the address is irrelevant
    to which domain owns it.
    """
    if not is_domain_verified(provider):
        return False
    try:
        candidate = email_domain(email_or_domain)
        bound = normalize_domain(provider.domain_binding)
    except ValueError:
        return False
    return candidate == bound
