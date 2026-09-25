"""Account lifecycle: verification, password reset, invitations, onboarding.

Two rules shape every endpoint here.

*Nothing reveals whether an address has an account.* Forgot-password, resend
verification and signup all answer the same way for a known and an unknown address,
send mail in both cases, and do the same amount of work. An attacker with a list of
addresses learns nothing about which are customers.

*Nothing crosses an organization.* An invite fixes its organization and role when an
admin of that organization creates it; acceptance reads both from the stored row and
never from the request, so a token cannot be redeemed into another workspace or at a
higher role than was granted.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import email as mailer
from .admin import human_admin
from .auth import create_token, current_user, hash_password, set_session_cookie
from .database import get_db
from .models import AuthToken, Invite, Organization, Project, User
from .ratelimit import build_rate_limiter
from .services import ensure_hierarchy_config, log
from .tokens import (EMAIL_VERIFICATION, INVITE_TTL, PASSWORD_RESET, RESET_TTL,
                     VERIFICATION_TTL, find_live_invite, hash_token, invalidate_auth_tokens,
                     issue_auth_token, mint, redeem_auth_token)

router = APIRouter(tags=["accounts"])

INVITE_ROLES = ("admin", "manager", "member")

# Per-IP, not per-account. Keying these on the submitted email would let anyone lock
# a known user out of their own password reset by spending the budget for them.
_LOGIN_LIMITER = build_rate_limiter(int(os.getenv("AUTH_LOGIN_RATE_LIMIT_PER_MINUTE", "10")), 60, "auth-login")
_SIGNUP_LIMITER = build_rate_limiter(int(os.getenv("AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE", "5")), 60, "auth-signup")
_RECOVERY_LIMITER = build_rate_limiter(int(os.getenv("AUTH_RECOVERY_RATE_LIMIT_PER_MINUTE", "5")), 60, "auth-recovery")
_TOKEN_LIMITER = build_rate_limiter(int(os.getenv("AUTH_TOKEN_RATE_LIMIT_PER_MINUTE", "20")), 60, "auth-token")
# Invites send mail from our domain to any address an admin types, and anyone can
# become an admin by signing up -- without a cap this is a free spam relay that
# burns the sending domain's reputation. Admins who have not confirmed their own
# address yet get a much smaller allowance, enough to bring in a first few teammates.
# Per-address budget for failed sign-ins, on top of the per-IP limiter: credential
# stuffing spread across many IPs still hits one account at a time. Keyed by the
# address whether or not an account exists, so the limit itself reveals nothing.
# Deliberately generous and short so it cannot be used to lock a real user out for long.
_ACCOUNT_LOGIN_FAILURES = build_rate_limiter(int(os.getenv("AUTH_ACCOUNT_FAILURES_PER_15_MIN", "20")), 900, "auth-account-failures")


# The passwords credential-stuffing lists try first. Length alone (8+) lets all of
# these through; a short explicit list stops the worst without a dependency.
_COMMON_PASSWORDS = frozenset("""
password password1 password12 password123 passw0rd p@ssw0rd 12345678 123456789 1234567890 0123456789
87654321 11111111 00000000 12341234 11223344 qwertyui qwerty12 qwerty123 qwertyuiop 1q2w3e4r 1qaz2wsx
zaq12wsx asdfghjk asdf1234 abcd1234 abc12345 iloveyou letmein1 welcome1 welcome123 admin123 administrator
changeme trustno1 sunshine princess football baseball superman starwars dragon12 monkey12 master12
""".split())


def password_problem(password: str, email: str = "") -> str | None:
    lowered = password.lower()
    local = email.split("@", 1)[0].lower()
    if lowered in _COMMON_PASSWORDS or len(set(lowered)) <= 2:
        return "That password is too common. Choose something harder to guess."
    if local and len(local) >= 4 and local in lowered:
        return "Your password should not contain your email address."
    return None


def require_strong_password(password: str, email: str = "") -> None:
    problem = password_problem(password, email)
    if problem:
        raise HTTPException(422, problem)


def enforce_account_login_budget(address: str) -> None:
    """Called only after a failed attempt; each call spends one unit of the budget."""
    if not _ACCOUNT_LOGIN_FAILURES.allow(address):
        raise HTTPException(429, "Too many failed sign-in attempts for this account. Wait 15 minutes, or reset your password.")


_INVITE_LIMITER = build_rate_limiter(int(os.getenv("INVITE_RATE_LIMIT_PER_HOUR", "50")), 3600, "invite")
_UNVERIFIED_INVITE_LIMITER = build_rate_limiter(int(os.getenv("UNVERIFIED_INVITE_RATE_LIMIT_PER_HOUR", "5")), 3600, "invite-unverified")

GENERIC_TOKEN_ERROR = "This link is invalid or has expired. Request a new one."
GENERIC_RECOVERY_REPLY = {"detail": "If that address has an account, we've sent it an email."}


def client_key(request: Request) -> str:
    """The rate-limit bucket for an unauthenticated caller.

    X-Forwarded-For is only consulted when the deployment says it sits behind a
    proxy it controls, and even then only the last hop is used -- the entry its own
    load balancer appended. Trusting the whole header by default would let a caller
    pick their own bucket by sending a header, which is an unlimited rate limit.
    """
    if os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def enforce(limiter, request: Request) -> None:
    if not limiter.allow(client_key(request)):
        raise HTTPException(429, "Too many attempts. Wait a minute and try again.")


def normalize(email: str) -> str:
    return email.strip().lower()


class EmailOnly(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    password: str = Field(min_length=8, max_length=200)


class TokenOnly(BaseModel):
    token: str = Field(min_length=20, max_length=500)


class InviteIn(BaseModel):
    email: EmailStr
    role: Literal["admin", "manager", "member"] = "member"


class AcceptInvite(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=8, max_length=200)
    accept_terms: bool = False


def record_terms_acceptance(user: User, accepted: bool) -> None:
    """Signup and invite acceptance are where people agree to the terms (the form
    requires the box whenever terms are published), so record it there, per version."""
    if accepted:
        from .privacy import CURRENT_TOS_VERSION
        user.tos_accepted_at = datetime.utcnow()
        user.tos_version = CURRENT_TOS_VERSION


def issue_verification(db: Session, user: User) -> str:
    """Returns the raw token for the caller to email *after* it commits. Sending
    first would hand out a link whose token a failed commit then rolls back."""
    return issue_auth_token(db, user.id, EMAIL_VERIFICATION, VERIFICATION_TTL)


@router.post("/api/auth/forgot-password", status_code=202)
def forgot_password(data: EmailOnly, request: Request, db: Session = Depends(get_db)):
    enforce(_RECOVERY_LIMITER, request)
    address = normalize(data.email)
    user = db.scalar(select(User).where(func.lower(User.email) == address, User.kind == "human"))
    if user and user.active:
        raw = issue_auth_token(db, user.id, PASSWORD_RESET, RESET_TTL)
        log(db, user.organization_id, user.id, "user", user.id, "password_reset_requested")
        db.commit()
        mailer.send_password_reset(user.email, user.name, raw)
    else:
        # Mail either way: a reply that only arrives for real accounts is the same
        # disclosure as an error that only appears for fake ones.
        mailer.send_password_reset_unknown(address)
    return GENERIC_RECOVERY_REPLY


@router.post("/api/auth/reset-password")
def reset_password(data: ResetPassword, request: Request, db: Session = Depends(get_db)):
    enforce(_TOKEN_LIMITER, request)
    token = redeem_auth_token(db, data.token, PASSWORD_RESET)
    if not token:
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    user = db.get(User, token.user_id)
    if not user or not user.active or user.kind != "human":
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    require_strong_password(data.password, user.email)
    user.password_hash = hash_password(data.password)
    # Whoever triggered this reset may be holding a stolen session; retiring the
    # epoch invalidates every token minted before now, including their own.
    user.session_epoch = (user.session_epoch or 0) + 1
    # Reaching the inbox proves the address, so a reset also settles verification.
    if user.email_verified_at is None:
        user.email_verified_at = datetime.utcnow()
    invalidate_auth_tokens(db, user.id, PASSWORD_RESET)
    log(db, user.organization_id, user.id, "user", user.id, "password_reset_completed")
    db.commit()
    return {"detail": "Your password has been changed. Sign in with your new password."}


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


@router.post("/api/me/password")
def change_password(data: ChangePassword, request: Request, response: Response,
                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Change the password while signed in. Every other session ends (as with a reset);
    this one continues on a fresh token, so the person is not signed out mid-task."""
    from .auth import verify_password
    if user.kind != "human":
        raise HTTPException(403, "Only people have passwords")
    if not verify_password(data.current_password, user.password_hash):
        enforce_account_login_budget(normalize(user.email))
        raise HTTPException(400, "Your current password is incorrect.")
    require_strong_password(data.new_password, user.email)
    user.password_hash = hash_password(data.new_password)
    user.session_epoch = (user.session_epoch or 0) + 1
    invalidate_auth_tokens(db, user.id, PASSWORD_RESET)
    log(db, user.organization_id, user.id, "user", user.id, "password_changed")
    db.commit()
    token = create_token(user)
    set_session_cookie(response, token)
    return {"detail": "Password changed. Other sessions have been signed out.", "token": token}


@router.post("/api/auth/verify-email")
def verify_email(data: TokenOnly, request: Request, db: Session = Depends(get_db)):
    enforce(_TOKEN_LIMITER, request)
    token = redeem_auth_token(db, data.token, EMAIL_VERIFICATION)
    if not token:
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    user = db.get(User, token.user_id)
    if not user or not user.active:
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    if user.email_verified_at is None:
        user.email_verified_at = datetime.utcnow()
        log(db, user.organization_id, user.id, "user", user.id, "email_verified")
    db.commit()
    return {"detail": "Your email address is confirmed.", "email": user.email}


@router.post("/api/auth/resend-verification", status_code=202)
def resend_verification(data: EmailOnly, request: Request, db: Session = Depends(get_db)):
    enforce(_RECOVERY_LIMITER, request)
    user = db.scalar(select(User).where(func.lower(User.email) == normalize(data.email), User.kind == "human"))
    if user and user.active and user.email_verified_at is None:
        raw = issue_verification(db, user)
        db.commit()
        mailer.send_verification(user.email, user.name, raw)
    return GENERIC_RECOVERY_REPLY


@router.post("/api/auth/invites", status_code=201)
def create_invite(data: InviteIn, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    address = normalize(data.email)
    limiter = _INVITE_LIMITER if user.email_verified_at else _UNVERIFIED_INVITE_LIMITER
    from .billing_service import BillingService
    seat_problem = BillingService(db).seat_problem(user.organization_id, reinviting=address)
    if seat_problem:
        raise HTTPException(402, seat_problem)
    if not limiter.allow(user.organization_id):
        detail = ("Too many invitations this hour. Try again later." if user.email_verified_at
                  else "Confirm your email address to send more invitations.")
        raise HTTPException(429, detail)
    existing = db.scalar(select(User).where(func.lower(User.email) == address))
    if existing:
        # Not an enumeration concern: the caller is an authenticated admin, and an
        # admin inviting someone needs to know they are already here.
        raise HTTPException(409, "Someone with that email address already has an account.")
    for pending in db.scalars(select(Invite).where(
            Invite.organization_id == user.organization_id, func.lower(Invite.email) == address,
            Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))).all():
        pending.revoked_at = datetime.utcnow()  # re-inviting replaces the outstanding link
    raw, token_hash = mint()
    invite = Invite(organization_id=user.organization_id, email=address, role=data.role,
                    token_hash=token_hash, invited_by=user.id,
                    expires_at=datetime.utcnow() + INVITE_TTL)
    db.add(invite)
    db.flush()
    organization = db.get(Organization, user.organization_id)
    log(db, user.organization_id, user.id, "invite", invite.id, "created")
    db.commit()
    mailer.send_invite(address, organization.name, user.name, raw)
    return invite_out(invite)


def invite_out(invite: Invite) -> dict:
    return {"id": invite.id, "email": invite.email, "role": invite.role,
            "expires_at": invite.expires_at, "accepted_at": invite.accepted_at,
            "created_at": invite.created_at}


@router.get("/api/auth/invites")
def list_invites(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(Invite).where(
        Invite.organization_id == user.organization_id, Invite.accepted_at.is_(None),
        Invite.revoked_at.is_(None)).order_by(Invite.created_at.desc())).all()
    return [invite_out(invite) for invite in rows]


@router.delete("/api/auth/invites/{invite_id}", status_code=204)
def revoke_invite(invite_id: str, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    invite = db.get(Invite, invite_id)
    # Tenancy before existence: an admin of another workspace gets the same 404 as
    # for an id that does not exist.
    if not invite or invite.organization_id != user.organization_id:
        raise HTTPException(404, "Invite not found")
    if invite.accepted_at is not None:
        raise HTTPException(409, "That invitation has already been accepted.")
    invite.revoked_at = datetime.utcnow()
    log(db, user.organization_id, user.id, "invite", invite.id, "revoked")
    db.commit()


@router.get("/api/auth/invites/{token}/preview")
def preview_invite(token: str, request: Request, db: Session = Depends(get_db)):
    """Lets the accept screen show who invited you before you type a password. Only
    someone holding the token sees this, and it returns nothing they were not
    already told in the email."""
    enforce(_TOKEN_LIMITER, request)
    invite = find_live_invite(db, token)
    if not invite:
        raise HTTPException(404, GENERIC_TOKEN_ERROR)
    organization = db.get(Organization, invite.organization_id)
    return {"email": invite.email, "role": invite.role,
            "organization": organization.name if organization else "",
            "expires_at": invite.expires_at}


@router.post("/api/auth/accept-invite")
def accept_invite(data: AcceptInvite, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce(_TOKEN_LIMITER, request)
    invite = find_live_invite(db, data.token)
    if not invite:
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    if db.scalar(select(User).where(func.lower(User.email) == invite.email)):
        raise HTTPException(409, "An account already exists for this address. Sign in instead.")
    require_strong_password(data.password, invite.email)
    # Organization and role come from the invite row, never from the request body.
    user = User(organization_id=invite.organization_id, name=data.name.strip(), email=invite.email,
                password_hash=hash_password(data.password), role=invite.role,
                avatar="".join(part[0] for part in data.name.split() if part)[:2].upper() or "EA",
                email_verified_at=datetime.utcnow())
    record_terms_acceptance(user, data.accept_terms)
    db.add(user)
    invite.accepted_at = datetime.utcnow()
    db.flush()
    log(db, invite.organization_id, user.id, "user", user.id, "invite_accepted")
    db.commit()
    organization = db.get(Organization, user.organization_id)
    token = create_token(user)
    set_session_cookie(response, token)
    return {"token": token, "user": account_out(user),
            "organization": {"id": organization.id, "name": organization.name}}


def account_out(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "kind": user.kind, "title": user.title, "avatar": user.avatar,
            "email_verified": user.email_verified_at is not None}


@router.get("/api/onboarding")
def onboarding(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """What the first admin still has to do. Steps are derived from real workspace
    state rather than stored flags, so they cannot drift out of step with it."""
    organization = db.get(Organization, user.organization_id)
    projects = db.scalar(select(func.count()).select_from(Project).where(
        Project.organization_id == user.organization_id)) or 0
    teammates = db.scalar(select(func.count()).select_from(User).where(
        User.organization_id == user.organization_id, User.kind == "human",
        User.id != user.id)) or 0
    invited = db.scalar(select(func.count()).select_from(Invite).where(
        Invite.organization_id == user.organization_id)) or 0
    steps = [
        {"key": "verify_email", "label": "Confirm your email address",
         "done": user.email_verified_at is not None},
        {"key": "create_project", "label": "Create your first project", "done": projects > 0},
        {"key": "invite_team", "label": "Invite a teammate", "done": teammates > 0 or invited > 0},
    ]
    return {"steps": steps, "complete": organization.onboarded_at is not None,
            "dismissed": organization.onboarded_at is not None,
            "can_invite": user.kind == "human" and user.role == "admin",
            "organization": organization.name}


@router.post("/api/onboarding/complete")
def complete_onboarding(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    organization = db.get(Organization, user.organization_id)
    if organization.onboarded_at is None:
        organization.onboarded_at = datetime.utcnow()
        log(db, organization.id, user.id, "organization", organization.id, "onboarded")
        db.commit()
    return {"complete": True}
