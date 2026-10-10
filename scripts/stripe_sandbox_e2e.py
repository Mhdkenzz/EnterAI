"""End-to-end billing check against Stripe *test mode* (never live).

Run by .github/workflows/stripe-sandbox.yml against a locally started API whose
webhooks are forwarded by the Stripe CLI. It walks the real paid lifecycle:

  register a workspace -> start Checkout through our API -> pay on Stripe's hosted
  page with test card 4242 -> webhooks mark the workspace active on the right plan
  with a billing period -> customer portal opens -> cancel in Stripe -> webhook
  marks it canceled and AI usage is refused (402).

Usage:
  python scripts/stripe_sandbox_e2e.py setup      # prints BILLING_PLANS JSON for the API
  python scripts/stripe_sandbox_e2e.py run        # the lifecycle above

Environment: STRIPE_SECRET_KEY (sk_test_/rk_test_ only), API_URL (default
http://127.0.0.1:8000/api).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

import httpx
import stripe

LOOKUP_KEY = "enterai_sandbox_team_monthly"
PLAN_NAME = "Sandbox Team"
API = os.getenv("API_URL", "http://127.0.0.1:8000/api").rstrip("/")


def _stripe() -> None:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit("STRIPE_SECRET_KEY must be a TEST-mode key (sk_test_... or rk_test_...). Refusing to run.")
    stripe.api_key = key


def setup() -> None:
    """Reuse (or create once) a test-mode product/price, and print BILLING_PLANS."""
    _stripe()
    prices = stripe.Price.list(lookup_keys=[LOOKUP_KEY], limit=1).data
    if prices:
        price = prices[0]
    else:
        product = stripe.Product.create(name="Enter AI sandbox plan", metadata={"enterai_sandbox": "1"})
        price = stripe.Price.create(product=product.id, unit_amount=2900, currency="usd",
                                    recurring={"interval": "month"}, lookup_key=LOOKUP_KEY)
    # The customer portal cannot open in test mode until a configuration exists.
    if not stripe.billing_portal.Configuration.list(limit=1).data:
        stripe.billing_portal.Configuration.create(
            business_profile={"headline": "Enter AI (sandbox)"},
            features={"invoice_history": {"enabled": True}, "payment_method_update": {"enabled": True},
                      "subscription_cancel": {"enabled": True, "mode": "immediately"}})
    print(json.dumps([{"name": PLAN_NAME, "stripe_price_id": price.id, "price_cents": 2900,
                       "currency": "USD", "seat_limit": 5, "ai_usage_limit": 100}]))


def _wait_for(client: httpx.Client, headers: dict, condition, what: str, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    overview: dict = {}
    while time.monotonic() < deadline:
        overview = client.get(f"{API}/billing", headers=headers).json()
        if condition(overview):
            print(f"ok: {what}")
            return overview
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}; last billing state: {json.dumps(overview)}")


def _pay_on_hosted_checkout(url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.locator("#cardNumber").fill("4242424242424242", timeout=60_000)
        page.locator("#cardExpiry").fill("12 / 34")
        page.locator("#cardCvc").fill("123")
        page.locator("#billingName").fill("Sandbox Tester")
        postal = page.locator("#billingPostalCode")
        if postal.count() and postal.is_visible():
            postal.fill("10001")
        try:
            page.locator("button[type=submit]").first.click()
            # Our success_url points at a frontend that is not running here; the
            # navigation itself is the proof Stripe accepted the payment.
            page.wait_for_url(re.compile(r"billing=success"), wait_until="commit", timeout=90_000)
        except Exception:
            page.screenshot(path="stripe-checkout-failure.png", full_page=True)
            raise
        finally:
            browser.close()
    print("ok: paid on Stripe Checkout with test card")


def run() -> None:
    _stripe()
    key = uuid.uuid4().hex[:10]
    email = f"stripe-sandbox-{key}@example.com"
    with httpx.Client(timeout=30) as client:
        registered = client.post(f"{API}/auth/register", json={
            "organization_name": f"Stripe Sandbox {key}", "name": "Sandbox Owner", "email": email,
            "password": "sandbox-password-9", "accept_terms": True})
        assert registered.status_code == 200, registered.text
        headers = {"Authorization": f"Bearer {registered.json()['token']}", "X-EnterAI-Client": "stripe-sandbox"}

        overview = client.get(f"{API}/billing", headers=headers).json()
        assert overview["status"] == "trial" and overview["checkout_available"], overview
        plan = next((p for p in overview["plans"] if p["name"] == PLAN_NAME), None)
        assert plan, f"{PLAN_NAME} not in plans; was BILLING_PLANS set from `setup`? {overview['plans']}"

        checkout = client.post(f"{API}/billing/checkout", headers=headers, json={"plan_id": plan["id"]})
        assert checkout.status_code == 200, checkout.text
        url = checkout.json()["url"]
        assert url.startswith("https://checkout.stripe.com/"), url
        session_id = re.search(r"(cs_test_[A-Za-z0-9]+)", url).group(1)
        print("ok: Checkout session created through the API")

        _pay_on_hosted_checkout(url)

        paid = _wait_for(client, headers, lambda o: o.get("status") == "active" and (o.get("plan") or {}).get("name") == PLAN_NAME
                         and o.get("current_period_end"), "webhooks marked the workspace active on the plan with a billing period")
        assert paid["usage"]["ai_limit"] == 100 and paid["blocked_reason"] is None, paid

        portal = client.post(f"{API}/billing/portal", headers=headers)
        assert portal.status_code == 200 and portal.json()["url"].startswith("https://billing.stripe.com/"), portal.text
        print("ok: customer portal session opens")

        session = stripe.checkout.Session.retrieve(session_id)
        stripe.Subscription.cancel(session.subscription)
        _wait_for(client, headers, lambda o: o.get("status") == "canceled" and o.get("blocked_reason"),
                  "cancellation webhook marked the workspace canceled")
        refused = client.post(f"{API}/ai/plan", headers=headers, json={"message": "plan my week"})
        assert refused.status_code == 402, refused.text
        print("ok: AI usage refused after cancellation (402)")

        try:
            stripe.Customer.delete(session.customer)
        except Exception as error:  # cleanup only
            print(f"note: could not delete test customer: {error}")
    print("PASS: Stripe sandbox lifecycle")


if __name__ == "__main__":
    {"setup": setup, "run": run}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
