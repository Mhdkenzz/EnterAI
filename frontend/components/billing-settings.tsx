import React from "react";

export function BillingSettingsUI({
  subscription,
  usage,
  plans,
}: {
  subscription?: { status: string; current_period_end: string; plan_id?: string };
  usage?: { ai_calls: number; seats: number; limit: number };
  plans?: Array<{ id: string; name: string; price_cents: number; seat_limit?: number }>
}) {
  return (
    <div className="space-y-4 rounded-lg border border-gray-200 p-4 bg-white shadow-sm">
      <h3 className="text-sm font-semibold text-gray-900">Billing</h3>
      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <span className="text-gray-500">Status</span>
          <div className="font-medium capitalize">{subscription?.status || "none"}</div>
        </div>
        <div>
          <span className="text-gray-500">Period End</span>
          <div className="font-medium">{subscription?.current_period_end || "—"}</div>
        </div>
        <div>
          <span className="text-gray-500">AI Usage</span>
          <div className="font-medium">{usage?.ai_calls || 0} / {usage?.limit || "—"}</div>
        </div>
        <div>
          <span className="text-gray-500">Seats</span>
          <div className="font-medium">{usage?.seats || 0} / {usage?.limit || "—"}</div>
        </div>
      </div>
    </div>
  );
}
