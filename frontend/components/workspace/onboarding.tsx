"use client";
import { useState } from "react";
import { Check, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { field } from "@/components/workspace/shared";
import type { User } from "@/lib/types";

export function Onboarding({
  state,
  reload,
  invite,
  done,
}: {
  state: any;
  reload: () => void;
  invite: (email: string, role: string) => Promise<void>;
  done: () => Promise<void>;
}) {
  const [email, setEmail] = useState(""),
    [role, setRole] = useState("member"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [sent, setSent] = useState("");
  // Setup is the administrator's checklist; members could neither act on it nor dismiss it.
  if (!state || state.complete || !state.can_invite) return null;
  const steps = state.steps || [],
    remaining = steps.filter((s: any) => !s.done).length;
  return (
    <Card className="mb-6 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">Finish setting up {state.organization}</h2>
          <p className="mt-1 text-xs text-zinc-500">
            {remaining
              ? `${remaining} step${remaining === 1 ? "" : "s"} left.`
              : "All done -- dismiss this when you're ready."}
          </p>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={async () => {
            await done();
            reload();
          }}
        >
          Dismiss
        </Button>
      </div>
      <ul className="mt-4 space-y-2">
        {steps.map((step: any) => (
          <li key={step.key} className="flex items-center gap-3 text-sm">
            <span
              aria-hidden
              className={cn(
                "grid h-5 w-5 shrink-0 place-items-center rounded-full border",
                step.done ? "border-emerald-400 bg-emerald-400 text-slate-950" : "border-zinc-600",
              )}
            >
              {step.done && <Check size={12} />}
            </span>
            <span className={cn(step.done && "text-zinc-500 line-through")}>{step.label}</span>
            <span className="sr-only">{step.done ? "completed" : "not completed"}</span>
          </li>
        ))}
      </ul>
      {state.can_invite && (
        <form
          className="mt-5 flex flex-wrap items-end gap-2 border-t border-line pt-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            setSent("");
            try {
              await invite(email, role);
              setSent(email);
              setEmail("");
              reload();
            } catch (err: any) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <label className="min-w-48 flex-1 text-xs text-zinc-400">
            Invite a teammate
            <input
              type="email"
              required
              placeholder="colleague@company.com"
              className={field}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="text-xs text-zinc-400">
            Role
            <select className={field} value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="member">member</option>
              <option value="manager">manager</option>
              <option value="admin">admin</option>
            </select>
          </label>
          <Button type="submit" disabled={busy}>
            {busy && <Loader2 className="animate-spin" size={15} />}Send invite
          </Button>
          {sent && (
            <p role="status" className="w-full text-xs text-emerald-300">
              Invitation sent to {sent}.
            </p>
          )}
          {error && (
            <p role="alert" className="w-full text-xs text-red-300">
              {error}
            </p>
          )}
        </form>
      )}
    </Card>
  );
}
export function VerifyBanner({ user, resend }: { user: User | null; resend: () => Promise<void> }) {
  const [sent, setSent] = useState(false);
  if (!user || user.email_verified !== false || user.kind === "agent") return null;
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100"
    >
      <span>Confirm your email address to secure your account.</span>
      {sent ? (
        <span className="text-xs text-amber-200/80">Confirmation link sent.</span>
      ) : (
        <button
          className="text-xs underline underline-offset-2"
          onClick={async () => {
            await resend();
            setSent(true);
          }}
        >
          Resend the link
        </button>
      )}
    </div>
  );
}
