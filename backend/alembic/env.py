from app.database import Base, engine
from app import models  # noqa
from alembic import context
from sqlalchemy import inspect, text

target_metadata = Base.metadata

def run_migrations_online():
    # Alembic's default version table uses VARCHAR(32), but historical revision
    # identifiers in this project exceed that length. This must run before the
    # migration chain: a new revision cannot repair a table if Alembic cannot
    # record the earlier long revision needed to reach it.
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            inspector = inspect(connection)
            if not inspector.has_table("alembic_version"):
                connection.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(128) NOT NULL PRIMARY KEY)"))
            else:
                version_column = next(column for column in inspector.get_columns("alembic_version") if column["name"] == "version_num")
                length = getattr(version_column["type"], "length", None)
                if length is not None and length < 128:
                    connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_online()
