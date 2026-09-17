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


def hash_password(value: str): return password_hash.hash(value)
def verify_password(value: str, hashed: str): return password_hash.verify(value, hashed)
def create_token(user: User):
    return jwt.encode({"sub": user.id, "exp": datetime.now(timezone.utc)+timedelta(days=7)}, SECRET, algorithm="HS256")
def current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)):
    if not credentials: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    try: payload = jwt.decode(credentials.credentials, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError: raise HTTPException(status_code=401, detail="Invalid session")
    user = db.scalar(select(User).where(User.id == payload.get("sub")))
    if not user or not user.active: raise HTTPException(status_code=401, detail="User unavailable")
    return user
