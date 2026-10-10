from __future__ import annotations
import json, os, logging, hashlib, hmac
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .models import Organization, User, utc_naive
from .database import get_db

log = logging.getLogger(__name__)

# What an organization gets before it pays. Operators tune these per deployment;
# paid plans come from BILLING_PLANS (see sync_plans_from_env).
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
TRIAL_AI_CALL_LIMIT = int(os.getenv("TRIAL_AI_CALL_LIMIT", "200"))
TRIAL_SEAT_LIMIT = int(os.getenv("TRIAL_SEAT_LIMIT", "0")) or None  # 0 = no seat cap during the trial
# past_due keeps access while Stripe retries the card (its dunning window); an
# organization is only cut off once Stripe gives up and cancels.
ENTITLED_STATUSES = ("trial", "active", "past_due")


def sync_plans_from_env(db: Session) -> None:
    """Upsert the plan catalog from BILLING_PLANS, a JSON list such as
    [{"name": "Team", "stripe_price_id": "price_123", "price_cents": 2900,
      "currency": "USD", "seat_limit": 25, "ai_usage_limit": 2000}].
    Limits are per billing period; omit one to leave it uncapped. Malformed
    configuration stops startup rather than silently selling the wrong plan."""
    from .billing_models import BillingPlan
    raw = os.getenv("BILLING_PLANS", "").strip()
    if not raw:
        return
    try:
        plans = json.loads(raw)
        assert isinstance(plans, list)
        for entry in plans:
            price_id = str(entry["stripe_price_id"]).strip()
            plan = db.scalar(select(BillingPlan).where(BillingPlan.stripe_plan_id == price_id))
            if plan is None:
                plan = BillingPlan(stripe_plan_id=price_id)
                db.add(plan)
            plan.name = str(entry["name"])[:120]
            plan.price_cents = int(entry.get("price_cents", 0))
            plan.currency = str(entry.get("currency", "USD"))[:3].upper()
            plan.seat_limit = entry.get("seat_limit")
            plan.ai_usage_limit = entry.get("ai_usage_limit")
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("BILLING_PLANS must be a JSON list of plans with name and stripe_price_id") from error
    db.commit()


def ensure_trial_subscription(db: Session, organization_id: str):
    """Every organization gets exactly one OrganizationSubscription row, created
    at registration (see main.py's register()). Without this, enforce_plan_limit
    denies every org outright -- it treats "no subscription row" as "not entitled
    to any usage" -- so every newly registered org would be locked out of AI
    features from the first request. The trial runs for TRIAL_DAYS with
    TRIAL_AI_CALL_LIMIT AI calls; after that an admin has to choose a plan."""
    from .billing_models import OrganizationSubscription
    existing = db.scalars(select(OrganizationSubscription).where(
        OrganizationSubscription.organization_id == organization_id)).first()
    if existing:
        return existing
    sub = OrganizationSubscription(organization_id=organization_id, status="trial",
                                   trial_ends_at=utc_naive() + timedelta(days=TRIAL_DAYS))
    db.add(sub)
    db.flush()
    return sub


class BillingNotConfigured(Exception):
    """No Stripe secret key: checkout and the customer portal are unavailable."""


class NoBillingCustomer(Exception):
    """The organization has never completed a checkout, so Stripe has no customer for it."""


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

    def subscription(self, organization_id: str):
        from .billing_models import OrganizationSubscription
        return self.db.scalars(select(OrganizationSubscription).where(
            OrganizationSubscription.organization_id == organization_id)).first()

    @staticmethod
    def period_start(sub) -> datetime:
        """Usage limits are per billing period: the whole trial while trialing, the
        current Stripe period once paying (or a rolling 30 days if Stripe has not
        reported one yet)."""
        if sub.status == "trial":
            return (sub.created_at.replace(tzinfo=None) if sub.created_at else utc_naive() - timedelta(days=TRIAL_DAYS))
        if sub.current_period_start:
            return sub.current_period_start.replace(tzinfo=None)
        return utc_naive() - timedelta(days=30)

    def limit_for(self, sub, metric: str) -> int | None:
        from .billing_models import BillingPlan
        plan = self.db.get(BillingPlan, sub.plan_id) if sub.plan_id else None
        if plan is not None:
            return {"ai_calls": plan.ai_usage_limit, "seats": plan.seat_limit}.get(metric)
        if sub.status == "trial":
            return {"ai_calls": TRIAL_AI_CALL_LIMIT, "seats": TRIAL_SEAT_LIMIT}.get(metric)
        return None  # active without a mapped plan: set up by hand, not metered

    def usage_since(self, organization_id: str, metric: str, since: datetime) -> int:
        from .billing_models import UsageRecord
        return int(self.db.scalar(select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
            UsageRecord.organization_id == organization_id, UsageRecord.metric == metric,
            UsageRecord.created_at >= since)) or 0)

    def entitlement_problem(self, organization_id: str, metric: str, usage_quantity: int) -> str | None:
        """Why this usage is not allowed, in words an end user can act on -- or None."""
        sub = self.subscription(organization_id)
        if sub is None or sub.status not in ENTITLED_STATUSES:
            return "This workspace has no active plan. An administrator can choose one in the Admin console."
        if sub.status == "trial" and sub.trial_ends_at and utc_naive() >= sub.trial_ends_at.replace(tzinfo=None):
            return "This workspace's free trial has ended. An administrator can choose a plan in the Admin console."
        limit = self.limit_for(sub, metric)
        if limit is None:
            return None
        if self.usage_since(organization_id, metric, self.period_start(sub)) + usage_quantity > limit:
            period = "trial" if sub.status == "trial" else "billing period"
            return f"This workspace has used its {limit} AI requests for this {period}. An administrator can upgrade the plan in the Admin console."
        return None

    def enforce_plan_limit(self, organization_id: str, metric: str, usage_quantity: int) -> bool:
        """Server-side entitlement check before allowing additional usage: denies
        without an entitled subscription, after the trial ends, or when this usage
        would push the current period's total past the plan's limit. A plan without
        a limit for this metric is uncapped for it."""
        return self.entitlement_problem(organization_id, metric, usage_quantity) is None

    def seat_problem(self, organization_id: str, adding: int = 1, reinviting: str | None = None) -> str | None:
        """Seats are people, not metered events: count active humans and outstanding
        invitations against the plan's seat limit before inviting another. A re-invite
        replaces that address's pending invitation, so it is not counted twice."""
        from .models import Invite
        sub = self.subscription(organization_id)
        limit = self.limit_for(sub, "seats") if sub else None
        if limit is None:
            return None
        people = self.db.scalar(select(func.count()).select_from(User).where(
            User.organization_id == organization_id, User.kind == "human", User.active == True)) or 0
        pending = self.db.scalar(select(func.count()).select_from(Invite).where(
            Invite.organization_id == organization_id, Invite.accepted_at.is_(None),
            Invite.revoked_at.is_(None), Invite.expires_at > utc_naive(),
            func.lower(Invite.email) != (reinviting or "").lower())) or 0
        if people + pending + adding > limit:
            return f"Your plan includes {limit} seats and they are all in use or invited. Upgrade the plan to add more people."
        return None

    def _stripe(self):
        if not self.stripe_key:
            raise BillingNotConfigured()
        import stripe
        stripe.api_key = self.stripe_key
        return stripe

    def create_checkout_url(self, organization_id: str, plan, customer_email: str, return_base: str) -> str:
        """Stripe-hosted Checkout for a subscription. client_reference_id carries the
        organization through to the checkout.session.completed webhook, which links
        the resulting customer and subscription back to it."""
        stripe = self._stripe()
        sub = self.subscription(organization_id)
        params = {
            "mode": "subscription",
            "line_items": [{"price": plan.stripe_plan_id, "quantity": 1}],
            "client_reference_id": organization_id,
            "success_url": f"{return_base}/?billing=success",
            "cancel_url": f"{return_base}/?billing=canceled",
            "allow_promotion_codes": True,
            "metadata": {"organization_id": organization_id},
        }
        if sub is not None and sub.stripe_customer_id:
            params["customer"] = sub.stripe_customer_id
        else:
            params["customer_email"] = customer_email
        return stripe.checkout.Session.create(**params).url

    def create_portal_url(self, organization_id: str, return_base: str) -> str:
        """Stripe's customer portal: change plan, update the card, download invoices, cancel."""
        stripe = self._stripe()
        sub = self.subscription(organization_id)
        if sub is None or not sub.stripe_customer_id:
            raise NoBillingCustomer()
        return stripe.billing_portal.Session.create(customer=sub.stripe_customer_id,
                                                    return_url=f"{return_base}/").url

    def process_webhook(self, payload: bytes, sig_header: str) -> dict:
        """Verify Stripe webhook signature and process event idempotently."""
        import stripe  # optional import; fails gracefully if unavailable
        from .deployment_config import is_deployment_environment
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
        # Staging is internet-facing too, so it gets the same rule as production.
        if is_deployment_environment() and not webhook_secret:
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

    def _sync_subscription_after_checkout(self, subscription_id: str | None) -> None:
        """Stripe does not order webhook deliveries: customer.subscription.created
        often lands before checkout.session.completed, finds no organization linked
        to that customer yet, and is skipped. Nothing else is guaranteed to follow,
        so the workspace would be active with no plan (unmetered) and no period.
        Once checkout has linked the customer, read the subscription itself and
        apply it as if its own event had just arrived. Best effort: the workspace is
        already active, and any later subscription event corrects it again."""
        if not subscription_id or not self.stripe_key:
            return
        try:
            subscription = self._stripe().Subscription.retrieve(subscription_id)
            data = subscription.to_dict() if hasattr(subscription, "to_dict") else dict(subscription)
        except Exception as error:  # network or Stripe-side failure; never fail the webhook
            log.warning("Could not fetch subscription %s after checkout: %s", subscription_id, error)
            return
        self.db.flush()  # the customer/subscription link above must be visible to the lookup
        self._apply_subscription_event("customer.subscription.updated", data)

    def _apply_subscription_event(self, event_type: str, obj: dict) -> None:
        """Update local subscription state from the Stripe event that just arrived.
        This is the only place OrganizationSubscription rows change status outside
        of ensure_trial_subscription -- the whole point of storing status/period
        fields is so enforce_plan_limit reflects what Stripe actually thinks is
        true, not just what the last direct DB write said."""
        from .billing_models import OrganizationSubscription

        def _from_unix(ts) -> datetime | None:
            # Naive UTC, like every column this is compared against (see models.utc_naive).
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None

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
            self._sync_subscription_after_checkout(obj.get("subscription"))
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
        first_item = (((obj.get("items") or {}).get("data") or [{}])[0]) or {}
        # Stripe API 2025-03-31 ("basil", what the pinned stripe-python 12 speaks)
        # moved the billing period from the subscription onto its items. Read the
        # item's when the subscription has none, so both API versions work.
        period_start = _from_unix(obj.get("current_period_start") or first_item.get("current_period_start"))
        period_end = _from_unix(obj.get("current_period_end") or first_item.get("current_period_end"))
        price_id = (first_item.get("price") or {}).get("id")
        if price_id:
            from .billing_models import BillingPlan
            plan = self.db.scalar(select(BillingPlan).where(BillingPlan.stripe_plan_id == price_id))
            if plan is not None:
                sub.plan_id = plan.id  # plan changes made in the customer portal land here too
            else:
                log.warning("Stripe price %s is not in BILLING_PLANS; subscription left unmetered", price_id)
        if period_start:
            sub.current_period_start = period_start
        if period_end:
            sub.current_period_end = period_end
        if event_type == "customer.subscription.deleted":
            sub.canceled_at = utc_naive()
