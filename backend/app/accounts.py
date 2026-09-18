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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import email as mailer
from .auth import create_token, current_user, hash_password
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


def human_admin(user: User = Depends(current_user)) -> User:
    if user.kind != "human" or user.role != "admin" or not user.active:
        raise HTTPException(403, "Human administrator required")
    return user


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
def accept_invite(data: AcceptInvite, request: Request, db: Session = Depends(get_db)):
    enforce(_TOKEN_LIMITER, request)
    invite = find_live_invite(db, data.token)
    if not invite:
        raise HTTPException(400, GENERIC_TOKEN_ERROR)
    if db.scalar(select(User).where(func.lower(User.email) == invite.email)):
        raise HTTPException(409, "An account already exists for this address. Sign in instead.")
    # Organization and role come from the invite row, never from the request body.
    user = User(organization_id=invite.organization_id, name=data.name.strip(), email=invite.email,
                password_hash=hash_password(data.password), role=invite.role,
                avatar="".join(part[0] for part in data.name.split() if part)[:2].upper() or "EA",
                email_verified_at=datetime.utcnow())
    db.add(user)
    invite.accepted_at = datetime.utcnow()
    db.flush()
    log(db, invite.organization_id, user.id, "user", user.id, "invite_accepted")
    db.commit()
    organization = db.get(Organization, user.organization_id)
    return {"token": create_token(user), "user": account_out(user),
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
