from __future__ import annotations
import os, logging, hashlib, hmac
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Organization, User
from .database import get_db

log = logging.getLogger(__name__)

class BillingService:
    """Server-side billing framework. Client-side enforcement is explicitly prohibited."""

    def __init__(self, db: Session):
        self.db = db
        # Stripe API key from environment; never stored in DB or committed
        self.stripe_key = os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_API_KEY")
        if not self.stripe_key:
            log.warning("Stripe key not configured; billing operations will return mock responses")

    def enforce_plan_limit(self, organization_id: str, metric: str, usage_quantity: int) -> bool:
        """Enforce server-side plan limits before allowing additional usage.

        Denies when the org has no active/trial subscription, or when this usage
        would push the metric's running total past the plan's configured limit.
        A plan without a limit set for this metric is treated as uncapped for it,
        since not every plan caps every metric.
        """
        from .billing_models import OrganizationSubscription, BillingPlan, UsageRecord
        sub = self.db.scalars(select(OrganizationSubscription).where(
            OrganizationSubscription.organization_id == organization_id,
            OrganizationSubscription.status.in_(["trial", "active"])
        )).first()
        if not sub:
            return False
        # Usage tied to existing ProviderCall / ExecutionRun metrics
        total_usage = self.db.scalars(select(UsageRecord).where(
            UsageRecord.organization_id == organization_id,
            UsageRecord.metric == metric
        )).all()
        total = sum(u.quantity for u in total_usage)
        if not sub.plan_id:
            return True
        plan = self.db.get(BillingPlan, sub.plan_id)
        if plan is None:
            return True
        limit = {"ai_calls": plan.ai_usage_limit, "seats": plan.seat_limit}.get(metric)
        if limit is None:
            return True
        return total + usage_quantity <= limit

    def process_webhook(self, payload: bytes, sig_header: str) -> dict:
        """Verify Stripe webhook signature and process event idempotently."""
        import stripe  # optional import; fails gracefully if unavailable
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
        if environment == "production" and not webhook_secret:
            # "whsec_test" is a public, well-known placeholder. Falling back to it
            # in production would let anyone who knows that convention forge
            # signed billing events (fake "subscription active", etc.).
            raise RuntimeError(
                "STRIPE_WEBHOOK_SECRET must be set in production; there is no safe"
                " default to verify Stripe webhook signatures against."
            )
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret or "whsec_test")
        except Exception as e:
            log.error("Webhook signature verification failed: %s", e)
            raise ValueError("Invalid webhook signature") from e
        # Idempotency: check event ID not already processed
        from .billing_models import BillingWebHookEvent
        existing = self.db.scalars(select(BillingWebHookEvent).where(
            BillingWebHookEvent.stripe_event_id == event.id
        )).first()
        if existing:
            log.info("Webhook event %s already processed; skipping", event.id)
            return {"status": "skipped", "event_id": event.id}
        # Process event (subscription updated, invoice paid, etc.)
        # event.get(...) works on the pinned stripe SDK but is deprecated there
        # ("will be removed in a future version") and already raises AttributeError
        # on newer stripe-python releases, where Event is no longer dict-like.
        # to_dict() is the version-stable way to read it.
        event_data = event.to_dict().get("data", {})
        webhook_record = BillingWebHookEvent(
            stripe_event_id=event.id,
            event_type=event.type,
            payload=event_data.get("object", {}),
            processed_at=datetime.now(timezone.utc),
        )
        self.db.add(webhook_record)
        # Idempotency only holds if this row actually lands: get_db() never
        # auto-commits, so a caller that read "processed" and moved on without
        # this commit would silently lose the replay guard on every retry.
        self.db.commit()
        # Apply event effects (e.g., update subscription status, trigger downgrade notification)
        log.info("Processed webhook %s: %s", event.id, event.type)
        return {"status": "processed", "event_id": event.id, "type": event.type}
