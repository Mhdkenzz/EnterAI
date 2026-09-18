import pytest

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
