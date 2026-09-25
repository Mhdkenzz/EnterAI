"""Trial, paid plans and seats: what an organization may use, and when it has to pay."""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import billing_service
from app.billing_models import BillingPlan, OrganizationSubscription, UsageRecord
from app.billing_service import BillingService, ensure_trial_subscription, sync_plans_from_env
from app.database import SessionLocal
from app.main import app
from app.models import utc_naive
from test_billing_phase12 import _signed_request, db_session, org  # noqa: F401 -- fixtures
from test_observability import workspace


def _use(db, org_id, n, when=None):
    for _ in range(n):
        db.add(UsageRecord(organization_id=org_id, metric="ai_calls", quantity=1, created_at=when or utc_naive()))
    db.flush()


def test_a_new_workspace_gets_a_time_boxed_trial(db_session, org):
    sub = ensure_trial_subscription(db_session, org.id)
    assert sub.status == "trial"
    assert timedelta(days=billing_service.TRIAL_DAYS - 1) < sub.trial_ends_at - utc_naive() <= timedelta(days=billing_service.TRIAL_DAYS)


def test_the_trial_is_capped_and_then_ends(db_session, org, monkeypatch):
    monkeypatch.setattr(billing_service, "TRIAL_AI_CALL_LIMIT", 3)
    sub = ensure_trial_subscription(db_session, org.id)
    service = BillingService(db_session)
    _use(db_session, org.id, 2)
    assert service.enforce_plan_limit(org.id, "ai_calls", 1)
    _use(db_session, org.id, 1)
    assert "used its 3 AI requests for this trial" in service.entitlement_problem(org.id, "ai_calls", 1)

    sub.trial_ends_at = utc_naive() - timedelta(minutes=1)
    assert "free trial has ended" in service.entitlement_problem(org.id, "ai_calls", 1)


def test_paid_usage_is_counted_per_billing_period_not_forever(db_session, org):
    plan = BillingPlan(name="Team", stripe_plan_id="price_team", ai_usage_limit=5)
    db_session.add(plan); db_session.flush()
    db_session.add(OrganizationSubscription(organization_id=org.id, plan_id=plan.id, status="active",
                                            current_period_start=utc_naive() - timedelta(days=2)))
    _use(db_session, org.id, 50, when=utc_naive() - timedelta(days=40))  # last month's usage
    _use(db_session, org.id, 4)
    service = BillingService(db_session)
    assert service.enforce_plan_limit(org.id, "ai_calls", 1)
    assert not service.enforce_plan_limit(org.id, "ai_calls", 2)


@pytest.mark.parametrize("status,allowed", [("past_due", True), ("canceled", False), ("incomplete", False)])
def test_access_follows_the_stripe_status(db_session, org, status, allowed):
    db_session.add(OrganizationSubscription(organization_id=org.id, status=status)); db_session.flush()
    assert BillingService(db_session).enforce_plan_limit(org.id, "ai_calls", 1) is allowed


def test_plans_come_from_configuration_and_are_upserted(db_session, monkeypatch):
    monkeypatch.setenv("BILLING_PLANS", '[{"name": "Team", "stripe_price_id": "price_t", "price_cents": 2900, "ai_usage_limit": 2000, "seat_limit": 25}]')
    sync_plans_from_env(db_session)
    monkeypatch.setenv("BILLING_PLANS", '[{"name": "Team plus", "stripe_price_id": "price_t", "price_cents": 3900}]')
    sync_plans_from_env(db_session)
    plans = db_session.scalars(select(BillingPlan)).all()
    assert len(plans) == 1 and plans[0].name == "Team plus" and plans[0].price_cents == 3900
    monkeypatch.setenv("BILLING_PLANS", '{"not": "a list"}')
    with pytest.raises(RuntimeError, match="BILLING_PLANS"):
        sync_plans_from_env(db_session)


def test_a_subscription_webhook_assigns_the_plan_by_its_stripe_price(db_session, org):
    plan = BillingPlan(name="Team", stripe_plan_id="price_team", ai_usage_limit=10)
    db_session.add(plan)
    sub = OrganizationSubscription(organization_id=org.id, status="active", stripe_customer_id="cus_1")
    db_session.add(sub); db_session.flush()
    BillingService(db_session)._apply_subscription_event("customer.subscription.updated", {
        "id": "sub_1", "customer": "cus_1", "status": "active", "current_period_start": 1_790_000_000,
        "items": {"data": [{"price": {"id": "price_team"}}]}})
    assert sub.plan_id == plan.id and sub.stripe_subscription_id == "sub_1"
    assert sub.current_period_start.tzinfo is None


def _admin_client_with_plan(c):
    headers, account = workspace(c)
    with SessionLocal() as db:
        plan = BillingPlan(name="Team", stripe_plan_id=f"price_{uuid4().hex}", price_cents=2900, ai_usage_limit=100, seat_limit=2)
        db.add(plan); db.commit()
        return headers, account, plan.id


def test_admins_see_their_plan_trial_and_usage():
    with TestClient(app) as c:
        headers, _account, _plan = _admin_client_with_plan(c)
        overview = c.get("/api/billing", headers=headers).json()
        assert overview["status"] == "trial"
        assert overview["trial_days_left"] == billing_service.TRIAL_DAYS
        assert overview["usage"]["ai_calls"] == 0 and overview["usage"]["ai_limit"] == billing_service.TRIAL_AI_CALL_LIMIT
        assert any(p["name"] == "Team" for p in overview["plans"])
        assert overview["blocked_reason"] is None


def test_checkout_and_portal_go_through_stripe(monkeypatch):
    calls = {}

    class FakeStripe:
        class checkout:
            class Session:
                @staticmethod
                def create(**params):
                    calls["checkout"] = params
                    return SimpleNamespace(url="https://checkout.stripe.test/c/1")

        class billing_portal:
            class Session:
                @staticmethod
                def create(**params):
                    calls["portal"] = params
                    return SimpleNamespace(url="https://billing.stripe.test/p/1")

    monkeypatch.setattr(BillingService, "_stripe", lambda self: FakeStripe)
    with TestClient(app) as c:
        headers, account, plan_id = _admin_client_with_plan(c)
        assert c.post("/api/billing/portal", headers=headers).status_code == 409  # nothing bought yet
        checkout = c.post("/api/billing/checkout", headers=headers, json={"plan_id": plan_id})
        assert checkout.json()["url"] == "https://checkout.stripe.test/c/1"
        assert calls["checkout"]["client_reference_id"] == account["organization"]["id"]
        assert calls["checkout"]["success_url"].endswith("/?billing=success")
        with SessionLocal() as db:
            sub = db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == account["organization"]["id"]))
            sub.stripe_customer_id = "cus_123"; db.commit()
        assert c.post("/api/billing/portal", headers=headers).json()["url"] == "https://billing.stripe.test/p/1"
        assert calls["portal"]["customer"] == "cus_123"


def test_billing_without_stripe_configured_says_so(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with TestClient(app) as c:
        headers, _account, plan_id = _admin_client_with_plan(c)
        response = c.post("/api/billing/checkout", headers=headers, json={"plan_id": plan_id})
        assert response.status_code == 503 and "not set up" in response.json()["detail"]


def test_invites_stop_at_the_plan_seat_limit():
    with TestClient(app) as c:
        headers, account, plan_id = _admin_client_with_plan(c)  # seat_limit=2, owner holds one
        with SessionLocal() as db:
            sub = db.scalar(select(OrganizationSubscription).where(OrganizationSubscription.organization_id == account["organization"]["id"]))
            sub.plan_id, sub.status = plan_id, "active"; db.commit()
        first = f"{uuid4().hex}@example.com"
        assert c.post("/api/auth/invites", headers=headers, json={"email": first, "role": "member"}).status_code == 201
        assert c.post("/api/auth/invites", headers=headers, json={"email": first, "role": "member"}).status_code == 201  # re-invite is not a new seat
        blocked = c.post("/api/auth/invites", headers=headers, json={"email": f"{uuid4().hex}@example.com", "role": "member"})
        assert blocked.status_code == 402 and "2 seats" in blocked.json()["detail"]


def test_members_cannot_see_billing():
    from test_observability import identity
    with TestClient(app) as c:
        _headers, account = workspace(c)
        _member_id, member_headers = identity(account["organization"]["id"], "member")
        assert c.get("/api/billing", headers=member_headers).status_code == 403
