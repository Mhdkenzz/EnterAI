"""Minting and redeeming the single-use tokens that arrive by email.

The raw token is returned to the caller once, to be emailed, and never stored. What
goes in the database is its SHA-256, so the table is useless to anyone who reads it:
there is nothing in it that can be put into a reset link.

SHA-256 without a salt or a work factor is deliberate. These are 256 bits of
`secrets` output, not user-chosen passwords -- they are not guessable, so the only
property needed is a fast, deterministic lookup key.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuthToken, Invite

PASSWORD_RESET = "password_reset"
EMAIL_VERIFICATION = "email_verification"

RESET_TTL = timedelta(minutes=60)
VERIFICATION_TTL = timedelta(hours=24)
INVITE_TTL = timedelta(days=7)


def mint() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_auth_token(db: Session, user_id: str, purpose: str, ttl: timedelta) -> str:
    """Invalidates any outstanding token of the same purpose before issuing a new
    one, so requesting a second reset link retires the first rather than leaving
    two live paths into the account."""
    invalidate_auth_tokens(db, user_id, purpose)
    raw, token_hash = mint()
    db.add(AuthToken(user_id=user_id, purpose=purpose, token_hash=token_hash,
                     expires_at=datetime.utcnow() + ttl))
    return raw


def invalidate_auth_tokens(db: Session, user_id: str, purpose: str) -> None:
    for token in db.scalars(select(AuthToken).where(
            AuthToken.user_id == user_id, AuthToken.purpose == purpose,
            AuthToken.used_at.is_(None))).all():
        token.used_at = datetime.utcnow()


def redeem_auth_token(db: Session, raw: str, purpose: str) -> AuthToken | None:
    """Returns the token only if it matches, is for this purpose, has not been used
    and has not expired -- and marks it used in the same breath, so a replay of the
    same link finds it spent. The caller commits."""
    token = db.scalar(select(AuthToken).where(AuthToken.token_hash == hash_token(raw)))
    if not token or token.purpose != purpose or token.used_at is not None:
        return None
    if token.expires_at < datetime.utcnow():
        return None
    token.used_at = datetime.utcnow()
    return token


def find_live_invite(db: Session, raw: str) -> Invite | None:
    invite = db.scalar(select(Invite).where(Invite.token_hash == hash_token(raw)))
    if not invite or invite.accepted_at is not None or invite.revoked_at is not None:
        return None
    if invite.expires_at < datetime.utcnow():
        return None
    return invite
