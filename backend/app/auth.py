import hashlib
import ipaddress
import os
from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException, Request, status
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
    # `epoch` pins the token to the credentials it was minted under. A password
    # reset bumps the user's epoch, which retires every token issued before it --
    # the only way to end a stolen session when sessions are stateless JWTs.
    now = datetime.now(timezone.utc)
    return jwt.encode({"kind": SESSION_TOKEN_KIND, "sub": user.id, "epoch": user.session_epoch or 0,
                       "iat": int(now.timestamp()), "exp": now + timedelta(days=7)}, SECRET, algorithm="HS256")


def _client_ip(request: Request) -> str | None:
    """Same trusted-proxy rule as accounts.client_key: only consulted behind a
    proxy the deployment says it controls, and only that proxy's own appended
    hop -- never the whole header, which a caller could otherwise set itself."""
    if os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else None


def _enforce_ip_allowlist(db: Session, user: User, request: Request) -> None:
    """Opt-in: an organization with no active allowlist entries is unrestricted,
    exactly like max_session_days being unset. Only once an admin adds at least
    one active entry does every other address start getting refused."""
    from .enterprise_models import IPAllowlist
    entries = db.scalars(select(IPAllowlist).where(
        IPAllowlist.organization_id == user.organization_id, IPAllowlist.active == True)).all()
    if not entries:
        return
    raw_ip = _client_ip(request)
    address = None
    if raw_ip:
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            address = None
    if address is None:
        raise HTTPException(status_code=403, detail="Request IP could not be determined")
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry.cidr, strict=False):
                return
        except ValueError:
            continue  # a malformed CIDR blocks nobody; it just never matches
    raise HTTPException(status_code=403, detail="Your network is not on this organization's IP allowlist")


def _enforce_session_policy(db: Session, user: User, payload: dict) -> None:
    """Session age is only checkable for tokens minted with an `iat` claim; a
    token from before this existed is treated as compliant, the same grandfathering
    `epoch` already gets, so deploying this does not sign out every open session."""
    from .enterprise_models import SessionPolicy
    policy = db.scalar(select(SessionPolicy).where(SessionPolicy.organization_id == user.organization_id))
    if not policy or not policy.max_session_days:
        return
    issued_at = payload.get("iat")
    if not issued_at:
        return
    age_seconds = datetime.now(timezone.utc).timestamp() - float(issued_at)
    if age_seconds > policy.max_session_days * 86400:
        raise HTTPException(status_code=401, detail="Session expired under organization policy; sign in again")


def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db), request: Request = None):
    if not credentials: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    try: payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError: raise HTTPException(status_code=401, detail="Invalid session")
    if payload.get("kind") != SESSION_TOKEN_KIND: raise HTTPException(status_code=401, detail="Invalid session")
    user = db.scalar(select(User).where(User.id == payload.get("sub")))
    if not user or not user.active: raise HTTPException(status_code=401, detail="User unavailable")
    # Tokens minted before this file existed carry no epoch; treating that as 0
    # matches the default column value, so upgrading does not sign everyone out.
    if int(payload.get("epoch") or 0) != int(user.session_epoch or 0):
        raise HTTPException(status_code=401, detail="Invalid session")
    _enforce_session_policy(db, user, payload)
    _enforce_ip_allowlist(db, user, request)
    # Agents authenticate with session tokens too (their writes are still gated by
    # per-route role checks); only confirmation tokens are barred above via the
    # pinned `kind` claim and the separate confirmation signing key.
    return user
