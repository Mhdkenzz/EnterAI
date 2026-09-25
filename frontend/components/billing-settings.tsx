"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type Plan = {
  id: string;
  name: string;
  price_cents: number;
  currency: string;
  seat_limit: number | null;
  ai_usage_limit: number | null;
};
type Billing = {
  status: string;
  plan: Plan | null;
  trial_ends_at?: string | null;
  trial_days_left?: number | null;
  current_period_end?: string | null;
  usage: { ai_calls: number; ai_limit: number | null; seat_limit: number | null } | null;
  blocked_reason?: string | null;
  plans: Plan[];
  checkout_available: boolean;
  portal_available: boolean;
};

const statusLabel: Record<string, string> = {
  trial: "Free trial",
  active: "Active",
  past_due: "Payment failed — retrying",
  canceled: "Canceled",
  incomplete: "Payment incomplete",
  unpaid: "Unpaid",
  none: "No plan",
};
const money = (plan: Plan) =>
  new Intl.NumberFormat(undefined, { style: "currency", currency: plan.currency || "USD" }).format(
    plan.price_cents / 100,
  );
const day = (value?: string | null) => (value ? new Date(value).toLocaleDateString() : "");

/** Plan, trial and usage for workspace admins. Payment happens on Stripe's hosted
 *  Checkout and customer portal; this only starts those flows. */
export function BillingSection() {
  const [billing, setBilling] = useState<Billing | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [returned, setReturned] = useState<"success" | "canceled" | null>(null);

  useEffect(() => {
    const outcome = new URLSearchParams(window.location.search).get("billing");
    if (outcome === "success" || outcome === "canceled") {
      setReturned(outcome);
      window.history.replaceState({}, "", window.location.pathname);
    }
    const controller = new AbortController();
    api<Billing>("/billing", { signal: controller.signal })
      .then(setBilling)
      .catch((e: unknown) => {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not load billing.");
      });
    return () => controller.abort();
  }, []);

  async function go(path: string, body?: object) {
    setBusy(path);
    setError("");
    try {
      const { url } = await api<{ url: string }>(path, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      window.location.assign(url);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not open billing.");
      setBusy("");
    }
  }

  const usage = billing?.usage;
  const percent = usage?.ai_limit ? Math.min(100, Math.round((usage.ai_calls / usage.ai_limit) * 100)) : 0;

  return (
    <section aria-labelledby="billing-heading" className="min-w-0 space-y-3">
      <h2 id="billing-heading" className="text-lg font-semibold">
        Plan & billing
      </h2>
      {returned === "success" && (
        <p role="status" className="rounded-lg border border-emerald-400/40 p-3 text-sm text-emerald-200">
          Payment received. Your plan updates within a minute; refresh if it still shows the old one.
        </p>
      )}
      {returned === "canceled" && (
        <p role="status" className="rounded-lg border border-line p-3 text-sm text-zinc-300">
          Checkout was canceled. Nothing was charged.
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-300">
          {error}
        </p>
      )}
      {!billing && !error && (
        <p role="status" className="text-sm text-zinc-300">
          Loading billing…
        </p>
      )}
      {billing && (
        <>
          <Card className="min-w-0 space-y-3 p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="text-sm text-zinc-300">Current plan</p>
                <p className="mt-1 text-xl font-semibold">
                  {billing.plan?.name ?? statusLabel[billing.status] ?? billing.status}
                </p>
                <p className="mt-1 text-sm text-zinc-300">
                  {billing.status === "trial" && billing.trial_days_left != null
                    ? billing.trial_days_left > 0
                      ? `${billing.trial_days_left} day${billing.trial_days_left === 1 ? "" : "s"} left in your trial (ends ${day(billing.trial_ends_at)}).`
                      : "Your trial has ended."
                    : billing.current_period_end
                      ? `${statusLabel[billing.status] ?? billing.status} · renews ${day(billing.current_period_end)}`
                      : (statusLabel[billing.status] ?? billing.status)}
                </p>
              </div>
              {billing.portal_available && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={!!busy}
                  onClick={() => go("/billing/portal")}
                >
                  {busy === "/billing/portal" ? "Opening…" : "Manage billing"}
                </Button>
              )}
            </div>
            {usage && (
              <div>
                <div className="flex justify-between text-sm text-zinc-300">
                  <span>AI requests this {billing.status === "trial" ? "trial" : "period"}</span>
                  <span>
                    {usage.ai_calls.toLocaleString()}
                    {usage.ai_limit != null ? ` / ${usage.ai_limit.toLocaleString()}` : " (unlimited)"}
                  </span>
                </div>
                {usage.ai_limit != null && (
                  <div
                    className="mt-2 h-2 overflow-hidden rounded-full bg-line"
                    role="progressbar"
                    aria-label="AI requests used"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={percent}
                  >
                    <div
                      className={percent >= 90 ? "h-full bg-red-400" : "h-full bg-accent"}
                      style={{ width: `${percent}%` }}
                    />
                  </div>
                )}
              </div>
            )}
            {billing.blocked_reason && (
              <p role="alert" className="text-sm text-amber-200">
                {billing.blocked_reason}
              </p>
            )}
          </Card>
          {billing.plans.length > 0 && !billing.portal_available && (
            <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {billing.plans.map((plan) => (
                <Card key={plan.id} className="flex min-w-0 flex-col gap-2 p-5">
                  <h3 className="font-medium">{plan.name}</h3>
                  <p className="text-2xl font-semibold">
                    {money(plan)}
                    <span className="text-sm font-normal text-zinc-400"> / month</span>
                  </p>
                  <ul className="flex-1 space-y-1 text-sm text-zinc-300">
                    <li>{plan.seat_limit ? `${plan.seat_limit} seats` : "Unlimited seats"}</li>
                    <li>
                      {plan.ai_usage_limit
                        ? `${plan.ai_usage_limit.toLocaleString()} AI requests / month`
                        : "Unlimited AI requests"}
                    </li>
                  </ul>
                  <Button
                    type="button"
                    disabled={!!busy || !billing.checkout_available}
                    onClick={() => go("/billing/checkout", { plan_id: plan.id })}
                  >
                    {busy === "/billing/checkout" ? "Opening checkout…" : `Choose ${plan.name}`}
                  </Button>
                </Card>
              ))}
            </div>
          )}
          {!billing.checkout_available && billing.plans.length > 0 && !billing.portal_available && (
            <p className="text-sm text-zinc-400">
              Online checkout is not set up for this deployment yet. Contact support to change your plan.
            </p>
          )}
        </>
      )}
    </section>
  );
}
