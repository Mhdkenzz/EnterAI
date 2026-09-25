"""Billing for workspace administrators: current plan and usage, Stripe Checkout
to subscribe, and Stripe's customer portal to change plan, card or cancel.

Payment details never touch this service -- Checkout and the portal are hosted by
Stripe, and the resulting state arrives through the signed webhook."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .admin import human_admin
from .billing_models import BillingPlan
from .billing_service import BillingNotConfigured, BillingService, NoBillingCustomer
from .database import get_db
from .email import app_base_url
from .models import User, utc_naive

router = APIRouter(prefix="/api/billing", tags=["billing"])

NOT_CONFIGURED = "Online billing is not set up for this deployment yet. Contact support to change your plan."


def _plan_out(plan: BillingPlan) -> dict:
    return {"id": plan.id, "name": plan.name, "price_cents": plan.price_cents, "currency": plan.currency,
            "seat_limit": plan.seat_limit, "ai_usage_limit": plan.ai_usage_limit}


@router.get("")
def billing_overview(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    service = BillingService(db)
    sub = service.subscription(user.organization_id)
    plans = [_plan_out(p) for p in db.scalars(select(BillingPlan).order_by(BillingPlan.price_cents)).all()]
    if sub is None:
        return {"status": "none", "plan": None, "plans": plans, "usage": None,
                "checkout_available": bool(service.stripe_key), "portal_available": False}
    plan = db.get(BillingPlan, sub.plan_id) if sub.plan_id else None
    trial_ends_at = sub.trial_ends_at.replace(tzinfo=None) if sub.trial_ends_at else None
    days_left = None
    if sub.status == "trial" and trial_ends_at:
        days_left = max(0, (trial_ends_at - utc_naive() + timedelta(hours=23, minutes=59)).days)
    return {
        "status": sub.status,
        "plan": _plan_out(plan) if plan else None,
        "trial_ends_at": trial_ends_at.isoformat() + "Z" if trial_ends_at else None,
        "trial_days_left": days_left,
        "current_period_end": sub.current_period_end.replace(tzinfo=None).isoformat() + "Z" if sub.current_period_end else None,
        "usage": {"ai_calls": service.usage_since(user.organization_id, "ai_calls", service.period_start(sub)),
                  "ai_limit": service.limit_for(sub, "ai_calls"), "seat_limit": service.limit_for(sub, "seats")},
        "blocked_reason": service.entitlement_problem(user.organization_id, "ai_calls", 0),
        "plans": plans,
        "checkout_available": bool(service.stripe_key),
        "portal_available": bool(service.stripe_key and sub.stripe_customer_id),
    }


class CheckoutIn(BaseModel):
    plan_id: str


@router.post("/checkout")
def start_checkout(data: CheckoutIn, user: User = Depends(human_admin), db: Session = Depends(get_db)):
    plan = db.get(BillingPlan, data.plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    try:
        url = BillingService(db).create_checkout_url(user.organization_id, plan, user.email, app_base_url())
    except BillingNotConfigured:
        raise HTTPException(503, NOT_CONFIGURED) from None
    return {"url": url}


@router.post("/portal")
def open_portal(user: User = Depends(human_admin), db: Session = Depends(get_db)):
    try:
        url = BillingService(db).create_portal_url(user.organization_id, app_base_url())
    except BillingNotConfigured:
        raise HTTPException(503, NOT_CONFIGURED) from None
    except NoBillingCustomer:
        raise HTTPException(409, "Choose a plan first; billing details are managed after the first payment.") from None
    return {"url": url}
