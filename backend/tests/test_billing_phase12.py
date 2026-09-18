import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db_session():
    from app.database import Base
    import app.models  # noqa: F401 -- registers every table on Base.metadata before create_all
    import app.billing_models  # noqa: F401
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture
def org(db_session):
    from app.models import Organization
    organization = Organization(name="Org", slug="org")
    db_session.add(organization)
    db_session.flush()
    return organization


def _subscribe(db_session, org, *, status="active", ai_usage_limit=None, seat_limit=None):
    from app.billing_models import BillingPlan, OrganizationSubscription
    plan = BillingPlan(name="Pro", stripe_plan_id=f"plan_{org.id}",
                        ai_usage_limit=ai_usage_limit, seat_limit=seat_limit)
    db_session.add(plan)
    db_session.flush()
    sub = OrganizationSubscription(organization_id=org.id, plan_id=plan.id, status=status)
    db_session.add(sub)
    db_session.flush()
    return sub


# Usage metering tied to existing ProviderCall / ExecutionRun metrics
def test_usage_tied_to_provider_call():
    from app.models import ProviderCall, ExecutionRun
    # ProviderCall table exists with fields: provider_mode, failed, duration_ms, organization_id, agent_id
    fields = {col.name for col in ProviderCall.__table__.columns}
    assert "provider_mode" in fields
    assert "failed" in fields
    assert "duration_ms" in fields
    assert "organization_id" in fields

# Webhook replay/idempotency
def test_webhook_idempotency_model_exists():
    from app.billing_models import BillingWebHookEvent
    fields = {col.name for col in BillingWebHookEvent.__table__.columns}
    assert "stripe_event_id" in fields
    assert "event_type" in fields
    assert "processed_at" in fields
    assert "payload" in fields

# Plan limits enforced server-side
def test_plan_limit_model_exists():
    from app.billing_models import BillingPlan, OrganizationSubscription
    fields_plan = {col.name for col in BillingPlan.__table__.columns}
    assert "stripe_plan_id" in fields_plan
    assert "seat_limit" in fields_plan
    assert "ai_usage_limit" in fields_plan
    fields_sub = {col.name for col in OrganizationSubscription.__table__.columns}
    assert "stripe_subscription_id" in fields_sub
    assert "status" in fields_sub
    assert "trial_ends_at" in fields_sub

# No raw card data stored
def test_no_raw_card_fields_in_billing_models():
    from app.billing_models import BillingPlan, OrganizationSubscription, UsageRecord, BillingWebHookEvent
    for model in [BillingPlan, OrganizationSubscription, UsageRecord, BillingWebHookEvent]:
        for col in model.__table__.columns:
            assert "card" not in col.name.lower(), f"Potential card storage in {model.__tablename__}.{col.name}"
            assert "number" not in col.name.lower() or "limit" in col.name.lower(), f"Unexpected number field in billing: {col.name}"

# Webhook signature verification framework exists
def test_webhook_service_has_signature_check():
    from app.billing_service import BillingService
    import inspect
    source = inspect.getsource(BillingService.process_webhook)
    assert "signature" in source or "sig_header" in source or "stripe" in source
    assert "idempotency" not in source.lower() or "already" in source  # basic check


# --- Real behavior, not just schema shape -----------------------------------

def test_enforce_plan_limit_denies_without_a_subscription(db_session, org):
    from app.billing_service import BillingService
    service = BillingService(db_session)
    assert service.enforce_plan_limit(org.id, "ai_calls", 1) is False


def test_enforce_plan_limit_denies_usage_that_would_exceed_the_plan(db_session, org):
    from app.billing_service import BillingService
    from app.billing_models import UsageRecord
    _subscribe(db_session, org, ai_usage_limit=10)
    db_session.add(UsageRecord(organization_id=org.id, metric="ai_calls", quantity=9))
    db_session.flush()
    service = BillingService(db_session)
    assert service.enforce_plan_limit(org.id, "ai_calls", 1) is True   # 9 + 1 == 10, at the limit
    assert service.enforce_plan_limit(org.id, "ai_calls", 2) is False  # 9 + 2 > 10


def test_enforce_plan_limit_allows_a_metric_the_plan_does_not_cap(db_session, org):
    from app.billing_service import BillingService
    _subscribe(db_session, org, seat_limit=5, ai_usage_limit=None)
    service = BillingService(db_session)
    assert service.enforce_plan_limit(org.id, "ai_calls", 1_000_000) is True


def test_enforce_plan_limit_scopes_usage_to_the_requesting_org(db_session, org):
    from app.billing_service import BillingService
    from app.billing_models import UsageRecord
    from app.models import Organization
    other = Organization(name="Other", slug="other")
    db_session.add(other)
    db_session.flush()
    _subscribe(db_session, org, ai_usage_limit=1)
    _subscribe(db_session, other, ai_usage_limit=1)
    db_session.add(UsageRecord(organization_id=other.id, metric="ai_calls", quantity=100))
    db_session.flush()
    service = BillingService(db_session)
    # other org's usage must not count against this org's limit
    assert service.enforce_plan_limit(org.id, "ai_calls", 1) is True


def _signed_request(payload: dict, secret: str):
    # Built by hand, matching Stripe's documented signing scheme, rather than via
    # stripe.WebhookSignature.generate_signature_header -- that helper does not
    # exist in the pinned stripe==12.3.0 (it was added in a later SDK version).
    import hashlib
    import hmac as hmac_lib
    body = json.dumps(payload).encode("utf-8")
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    signature = hmac_lib.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return body, f"t={timestamp},v1={signature}"


def test_webhook_rejects_an_invalid_signature(db_session, monkeypatch):
    from app.billing_service import BillingService
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_real_secret")
    service = BillingService(db_session)
    body, _ = _signed_request({"id": "evt_1", "type": "customer.subscription.updated"}, "whsec_wrong_secret")
    with pytest.raises(ValueError, match="Invalid webhook signature"):
        service.process_webhook(body, "t=123,v1=deadbeef")


def test_webhook_accepts_a_validly_signed_event_and_is_idempotent(db_session, monkeypatch):
    from app.billing_service import BillingService
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_real_secret")
    service = BillingService(db_session)
    body, header = _signed_request(
        {"id": "evt_replay_1", "type": "customer.subscription.updated", "data": {"object": {"status": "active"}}},
        "whsec_real_secret",
    )
    first = service.process_webhook(body, header)
    assert first["status"] == "processed"
    second = service.process_webhook(body, header)
    assert second["status"] == "skipped"  # same event id replayed -- not double-applied


def test_webhook_fails_closed_in_production_without_a_configured_secret(db_session, monkeypatch):
    from app.billing_service import BillingService
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    service = BillingService(db_session)
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET must be set"):
        service.process_webhook(b'{"id": "evt_x"}', "t=123,v1=whatever")


def test_stripe_webhook_route_is_actually_wired_into_the_app(monkeypatch):
    """The route existed in billing_webhook.py but was never registered on `app`,
    and separately could not even be imported (bare `Depends` with no import) --
    so every real Stripe webhook call would have hit a 404. Both must stay fixed."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_route_test")
    with TestClient(app) as c:
        bad = c.post("/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=bad"})
        assert bad.status_code == 400

        body, header = _signed_request({"id": "evt_route_1", "type": "ping"}, "whsec_route_test")
        good = c.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
        assert good.status_code == 200
        assert good.json()["status"] == "processed"
