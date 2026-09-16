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
SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")

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
