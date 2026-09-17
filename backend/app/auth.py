import hashlib
import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session
from .database import get_db
from .models import User

password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)

_INSECURE_JWT_SECRETS = {
    "dev-secret-change-me",
    "change-me-in-production",
    "replace-this-with-a-long-random-production-secret",
}


def _get_jwt_secret() -> str:
    """Resolve JWT_SECRET securely: production must have it set, dev has fallback.

    Production also rejects the exact placeholder strings published in this repo's
    own docker-compose.yml and .env.example, and anything shorter than 32 chars --
    otherwise a deployment that copies the example file verbatim would boot with a
    publicly known signing secret, letting anyone forge auth tokens for any user.
    """
    secret = os.getenv("JWT_SECRET")
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    if environment == "production":
        stripped = (secret or "").strip()
        if not stripped:
            raise RuntimeError(
                "JWT_SECRET must be set in production environment."
                " Set JWT_SECRET in your environment or .env file."
            )
        if stripped in _INSECURE_JWT_SECRETS or len(stripped) < 32:
            raise RuntimeError(
                "JWT_SECRET must be a unique, random value of at least 32 characters in"
                " production -- the placeholder from .env.example or docker-compose.yml"
                " is not safe to use. Generate one with e.g. `openssl rand -hex 32`."
            )
    return secret or "dev-secret-change-me"

SECRET = _get_jwt_secret()

# Session tokens and Copilot confirmation tokens are two different token families that
# happen to be handed to the same browser. They MUST NOT be interchangeable: a
# confirmation token is returned to any org member who chats with an agent, and its
# `sub` is the *agent's* id -- so if it also authenticated, any member could replay it
# as a bearer token and act as an admin-role agent identity.
#
# Two independent defences, both required:
#   1. A distinct signing key, derived from JWT_SECRET so it rotates with it (no extra
#      operator configuration, and a runtime env change can never rotate only one of
#      the two families).
#   2. An explicit `kind` claim that each verifier pins.
SESSION_TOKEN_KIND = "session"
CONFIRMATION_SECRET = hashlib.sha256(f"enterai:copilot-confirmation:v1:{SECRET}".encode()).hexdigest()


def hash_password(value: str): return password_hash.hash(value)
def verify_password(value: str, hashed: str): return password_hash.verify(value, hashed)
def create_token(user: User):
    return jwt.encode({"kind": SESSION_TOKEN_KIND, "sub": user.id, "exp": datetime.now(timezone.utc)+timedelta(days=7)}, SECRET, algorithm="HS256")
def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)):
    if not credentials: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    try: payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError: raise HTTPException(status_code=401, detail="Invalid session")
    if payload.get("kind") != SESSION_TOKEN_KIND: raise HTTPException(status_code=401, detail="Invalid session")
    user = db.scalar(select(User).where(User.id == payload.get("sub")))
    if not user or not user.active: raise HTTPException(status_code=401, detail="User unavailable")
    # Agents authenticate with session tokens too (their writes are still gated by
    # per-route role checks); only confirmation tokens are barred above via the
    # pinned `kind` claim and the separate confirmation signing key.
    return user
