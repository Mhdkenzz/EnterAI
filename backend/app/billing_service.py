from __future__ import annotations
import os, logging, hashlib, hmac
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .models import Organization, User
from .database import get_db

log = logging.getLogger(__name__)


def ensure_trial_subscription(db: Session, organization_id: str):
    """Every organization gets exactly one OrganizationSubscription row, created
    at registration (see main.py's register()). Without this, enforce_plan_limit
    denies every org outright -- it treats "no subscription row" as "not entitled
    to any usage" -- so every newly registered org would be locked out of AI
    features from the first request. A trial row with no plan_id is uncapped
    (enforce_plan_limit's "plan without a limit" rule), matching today's
    unmetered behavior until a real plan is actually assigned."""
    from .billing_models import OrganizationSubscription
    existing = db.scalars(select(OrganizationSubscription).where(
        OrganizationSubscription.organization_id == organization_id)).first()
    if existing:
        return existing
    sub = OrganizationSubscription(organization_id=organization_id, status="trial")
    db.add(sub)
    db.flush()
    return sub


class BillingService:
    """Server-side billing framework. Client-side enforcement is explicitly prohibited."""

    def __init__(self, db: Session):
        self.db = db
        # Stripe API key from environment; never stored in DB or committed
        self.stripe_key = os.getenv("STRIPE_SECRET_KEY") or os.getenv("STRIPE_API_KEY")
        if not self.stripe_key:
            log.warning("Stripe key not configured; billing operations will return mock responses")

    def record_usage(self, organization_id: str, metric: str, quantity: int = 1) -> None:
        """Meter one unit of usage after a call that enforce_plan_limit already
        allowed. Separate from enforce_plan_limit so a caller can check-then-act:
        check before doing the (expensive, possibly-failing) work, record only
        once it actually happened."""
        from .billing_models import UsageRecord
        self.db.add(UsageRecord(organization_id=organization_id, metric=metric, quantity=quantity))

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
        event_object = event_data.get("object", {})
        webhook_record = BillingWebHookEvent(
            stripe_event_id=event.id,
            event_type=event.type,
            payload=event_object,
            processed_at=datetime.now(timezone.utc),
        )
        self.db.add(webhook_record)
        try:
            # Idempotency only holds if this row actually lands: get_db() never
            # auto-commits, so a caller that read "processed" and moved on without
            # this commit would silently lose the replay guard on every retry.
            self.db.commit()
        except IntegrityError:
            # Two deliveries of the same event landed concurrently (Stripe retries
            # aggressively on anything but a fast 2xx): the unique constraint on
            # stripe_event_id caught the race the earlier SELECT could not. The
            # other request already recorded and will apply this event; answer the
            # same idempotent "skipped" rather than a 500 that makes Stripe retry
            # an event that in fact succeeded.
            self.db.rollback()
            log.info("Webhook event %s recorded concurrently; skipping", event.id)
            return {"status": "skipped", "event_id": event.id}
        self._apply_subscription_event(event.type, event_object)
        self.db.commit()
        log.info("Processed webhook %s: %s", event.id, event.type)
        return {"status": "processed", "event_id": event.id, "type": event.type}

    def _apply_subscription_event(self, event_type: str, obj: dict) -> None:
        """Update local subscription state from the Stripe event that just arrived.
        This is the only place OrganizationSubscription rows change status outside
        of ensure_trial_subscription -- the whole point of storing status/period
        fields is so enforce_plan_limit reflects what Stripe actually thinks is
        true, not just what the last direct DB write said."""
        from .billing_models import OrganizationSubscription

        def _from_unix(ts) -> datetime | None:
            return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None

        if event_type == "checkout.session.completed":
            # Checkout's client_reference_id is the standard way to carry our
            # internal organization id through Stripe's hosted flow back to us.
            org_id = obj.get("client_reference_id")
            customer_id = obj.get("customer")
            if not org_id or not customer_id:
                log.warning("checkout.session.completed missing client_reference_id/customer; cannot link to an organization")
                return
            sub = self.db.scalars(select(OrganizationSubscription).where(
                OrganizationSubscription.organization_id == org_id)).first()
            if sub is None:
                sub = OrganizationSubscription(organization_id=org_id, status="active")
                self.db.add(sub)
            sub.stripe_customer_id = customer_id
            if obj.get("subscription"):
                sub.stripe_subscription_id = obj["subscription"]
            sub.status = "active"
            return

        if not event_type.startswith("customer.subscription."):
            return  # nothing else in scope changes subscription state

        subscription_id = obj.get("id")
        customer_id = obj.get("customer")
        sub = None
        if subscription_id:
            sub = self.db.scalars(select(OrganizationSubscription).where(
                OrganizationSubscription.stripe_subscription_id == subscription_id)).first()
        if sub is None and customer_id:
            sub = self.db.scalars(select(OrganizationSubscription).where(
                OrganizationSubscription.stripe_customer_id == customer_id)).first()
        if sub is None:
            # No local row links this Stripe customer/subscription to an
            # organization yet -- most commonly a subscription created directly in
            # the Stripe Dashboard rather than through our Checkout flow. There is
            # nothing to update; this is not an error.
            log.warning("No local subscription found for Stripe subscription %s (customer %s); skipping",
                       subscription_id, customer_id)
            return
        if subscription_id:
            sub.stripe_subscription_id = subscription_id
        stripe_status = obj.get("status")
        if stripe_status:
            sub.status = "canceled" if event_type == "customer.subscription.deleted" else stripe_status
        period_start = _from_unix(obj.get("current_period_start"))
        period_end = _from_unix(obj.get("current_period_end"))
        if period_start:
            sub.current_period_start = period_start
        if period_end:
            sub.current_period_end = period_end
        if event_type == "customer.subscription.deleted":
            sub.canceled_at = datetime.now(timezone.utc)
