"""The real database migration chain must round trip, not just upgrade.

SQLite never enforced foreign keys, so only a metadata-sort check (and, in CI,
real PostgreSQL) can catch cyclic-FK schema defects.
"""
import warnings

from sqlalchemy import exc


def test_model_metadata_is_drop_sortable():
    from app.database import Base
    from app import models  # noqa: F401

    # CircularDependencyError here is exactly what broke `alembic downgrade base`
    # on PostgreSQL: the DROP plan needs named, use_alter FKs on the cycle. An
    # unresolvable-cycle SAWarning would mean the same latent defect, so fail on it.
    with warnings.catch_warnings():
        warnings.simplefilter("error", exc.SAWarning)
        ordered = Base.metadata.sorted_tables
    assert {table.name for table in ordered} >= {"users", "organizations", "projects", "tasks"}


def test_cycle_edges_are_named_and_use_alter():
    from app.database import Base
    from app import models  # noqa: F401

    # Exactly the users<->tasks<->projects cycle: every constraint involved in it
    # needs an explicit name, and the nullable edges must be created via ALTER
    # (after all tables exist) so PostgreSQL can sort both CREATE and DROP.
    for table_name, column_name, needs_alter in [
        ("projects", "owner_id", True),
        ("tasks", "project_id", False),
        ("users", "current_task_id", True),
        ("users", "last_completed_task_id", True),
    ]:
        (fk,) = Base.metadata.tables[table_name].columns[column_name].foreign_keys
        assert fk.constraint is not None
        assert fk.constraint.name, f"{table_name}.{column_name} FK needs an explicit name"
        if needs_alter:
            assert fk.constraint.use_alter, f"{table_name}.{column_name} must use_alter to break the create/drop cycle"
