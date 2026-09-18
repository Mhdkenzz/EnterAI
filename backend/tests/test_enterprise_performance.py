"""Phase 15 — Large-volume audit aggregation query performance.

Runs against the suite's real, externally-migrated database (see
test_sso_phase14.py's module docstring).
"""
import time
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.enterprise_models import EnterpriseAuditAggregation
from app.models import Organization


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_large_volume_aggregation_query_stays_fast(db_session):
    key = uuid4().hex[:12]
    org = Organization(name=f"Perf Org {key}", slug=f"perf-org-{key}")
    db_session.add(org)
    db_session.flush()
    org_id = org.id
    for i in range(500):
        db_session.add(EnterpriseAuditAggregation(
            organization_id=org_id, action="test_action", date_bucket="2025-09-18", count=i,
        ))
    db_session.commit()

    started = time.monotonic()
    rows = db_session.scalars(select(EnterpriseAuditAggregation)
                              .where(EnterpriseAuditAggregation.organization_id == org_id)).all()
    elapsed = time.monotonic() - started

    assert len(rows) == 500
    # A generous ceiling: the point is catching a missing index (ix_...organization_id
    # exists in 0012_sso_enterprise), not asserting a specific database's latency.
    assert elapsed < 2.0, f"org-scoped aggregation query took {elapsed:.2f}s for 500 rows"
