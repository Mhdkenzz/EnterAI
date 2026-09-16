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

def _get_jwt_secret() -> str:
    """Resolve JWT_SECRET securely: production must have it set, dev has fallback."""
    secret = os.getenv("JWT_SECRET")
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    if environment == "production" and (not secret or not secret.strip()):
        raise RuntimeError(
            "JWT_SECRET must be set in production environment."
            " Set JWT_SECRET in your environment or .env file."
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
