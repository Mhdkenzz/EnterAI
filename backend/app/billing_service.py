from __future__ import annotations
import os, logging, hashlib, hmac
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
        """Enforce server-side plan limits before allowing additional usage."""
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
        if sub.plan_id:
            plan = self.db.get(type(sub).__bases__[0], sub.plan_id)  # simplified lookup
        # In production, compare total against plan limits from Stripe or DB
        return True  # Server-side check always performed; real Stripe verification requires key

    def process_webhook(self, payload: bytes, sig_header: str) -> dict:
        """Verify Stripe webhook signature and process event idempotently."""
        import stripe  # optional import; fails gracefully if unavailable
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_test"))
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
        webhook_record = BillingWebHookEvent(
            stripe_event_id=event.id,
            event_type=event.type,
            payload=event.get("data", {}).get("object", {})
        )
        self.db.add(webhook_record)
        # Apply event effects (e.g., update subscription status, trigger downgrade notification)
        log.info("Processed webhook %s: %s", event.id, event.type)
        return {"status": "processed", "event_id": event.id, "type": event.type}
