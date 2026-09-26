"""Regression checks for the demo-facing request path.

These checks deliberately avoid an external database: they protect the execution
model that keeps ordinary uploads and authenticated requests responsive when the
demo is backed by PostgreSQL.
"""
import inspect


def test_blocking_upload_handlers_run_in_the_threadpool():
    from app.main import add_attachment, add_project_document, assist_project_draft

    assert not inspect.iscoroutinefunction(assist_project_draft)
    assert not inspect.iscoroutinefunction(add_project_document)
    assert not inspect.iscoroutinefunction(add_attachment)


def test_postgres_pool_capacity_has_a_matching_request_budget():
    from app.database import POOL_CAPACITY

    # Startup reserves three threads for background loops. The public default must
    # leave at least one worker slot while never exceeding available connections.
    request_threads = POOL_CAPACITY - 3
    assert POOL_CAPACITY >= 4
    assert 1 <= request_threads < POOL_CAPACITY
