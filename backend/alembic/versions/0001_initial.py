"""initial Enter AI schema"""
from alembic import op
from sqlalchemy import inspect
from app.database import Base
from app import models  # noqa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # Named, use_alter FKs (models.py) break the users->tasks->projects->users
    # cycle so PostgreSQL can both create and drop this schema.
    bind = op.get_bind()
    # On PostgreSQL, document_chunks (pgvector) is left for 0009, which enables
    # the vector extension first and adds its own ivfflat/tenancy indexes via
    # raw SQL -- creating it here via ORM metadata would need the extension
    # before it exists on a fresh database. SQLite has no extension to wait
    # for, so it keeps creating document_chunks here as before (0009 then
    # no-ops on SQLite because the table already exists).
    if bind.dialect.name == "postgresql":
        tables = [t for t in Base.metadata.sorted_tables if t.name != "document_chunks"]
        Base.metadata.create_all(bind=bind, tables=tables)
    else:
        Base.metadata.create_all(bind=bind)

def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())
    # 0004's downgrade removed the users task-pointer columns, and PostgreSQL drops
    # a constraint with its column -- so the cycle constraints may already be gone.
    # Drop only the ones still present, then remove tables in dependency order.
    if bind.dialect.name != "sqlite":
        for table, constraint in [
            ("users", "fk_users_current_task_id_tasks"),
            ("users", "fk_users_last_completed_task_id_tasks"),
            ("projects", "fk_projects_owner_id_users"),
        ]:
            if table not in existing:
                continue
            names = {fk["name"] for fk in inspector.get_foreign_keys(table)}
            if constraint in names:
                op.drop_constraint(constraint, table, type_="foreignkey")
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in existing:
            op.drop_table(table.name)
