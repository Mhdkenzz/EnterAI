import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./enterai.db")
# Limits are per worker: provision PostgreSQL for 15 connections per API worker,
# plus migration/administration capacity. Pre-ping handles stale checkouts, not
# interrupted transactions; do not automatically retry writes after disconnects.
_pool_options = {}
if make_url(DATABASE_URL).get_backend_name() == "postgresql":
    _pool_options = dict(pool_size=5, max_overflow=10, pool_timeout=30,
                         pool_recycle=1800)
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, **_pool_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
