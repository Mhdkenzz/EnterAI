import os
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./enterai.db")
# Limits are per worker: provision PostgreSQL for 15 connections per API worker,
# plus migration/administration capacity. Pre-ping handles stale checkouts, not
# interrupted transactions; do not automatically retry writes after disconnects.
_pool_options = {}
# Connections a worker may hold at once. Request threads are capped to this in
# main.startup (REQUEST_THREADS), so a busy worker queues requests on its event loop
# instead of piling threads up behind an exhausted pool.
POOL_CAPACITY = 15
if make_url(DATABASE_URL).get_backend_name() == "postgresql":
    _pool_options = dict(pool_size=5, max_overflow=POOL_CAPACITY - 5, pool_timeout=30,
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
