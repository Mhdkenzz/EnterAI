"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type Invite = { id: string; email: string; role: string; expires_at: string; accepted_at: string | null };
const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

/** Invite teammates and manage outstanding invitations. The onboarding card had the
 *  only invite form, so once it was dismissed there was no way to add anyone. */
export function TeamInvitesSection() {
  const [invites, setInvites] = useState<Invite[]>([]),
    [email, setEmail] = useState(""),
    [role, setRole] = useState("member"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");

  async function load(signal?: AbortSignal) {
    const rows = await api<Invite[]>("/auth/invites", { signal });
    setInvites(rows.filter(i => !i.accepted_at));
  }
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).catch((e: unknown) => {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "Could not load invitations.");
    });
    return () => controller.abort();
  }, []);

  async function send(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    const replacing = invites.some(i => i.email.toLowerCase() === email.trim().toLowerCase());
    try {
      await api("/auth/invites", { method: "POST", body: JSON.stringify({ email: email.trim(), role }) });
      setNotice(replacing ? `Sent a new invitation to ${email.trim()}. The earlier link no longer works.` : `Invitation sent to ${email.trim()}.`);
      setEmail("");
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not send the invitation.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(invite: Invite) {
    setError(""); setNotice("");
    try {
      await api(`/auth/invites/${invite.id}`, { method: "DELETE" });
      setNotice(`Invitation for ${invite.email} revoked.`);
      await load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not revoke the invitation.");
    }
  }

  return <section aria-labelledby="invites-heading" className="min-w-0 space-y-3">
    <h2 id="invites-heading" className="text-lg font-semibold">Invite teammates</h2>
    <Card className="min-w-0 space-y-4 p-5">
      <form onSubmit={send} className="flex min-w-0 flex-wrap items-end gap-2">
        <label className="min-w-48 flex-1 text-sm text-zinc-300">Email address
          <input type="email" required className={field} value={email} onChange={e => setEmail(e.target.value)} placeholder="teammate@company.com" />
        </label>
        <label className="text-sm text-zinc-300">Role
          <select className={field} value={role} onChange={e => setRole(e.target.value)}>
            <option value="member">Member</option><option value="manager">Manager</option><option value="admin">Admin</option>
          </select>
        </label>
        <Button type="submit" disabled={busy}>{busy ? "Sending…" : "Send invite"}</Button>
      </form>
      {notice && <p role="status" className="text-sm text-emerald-300">{notice}</p>}
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
      {invites.length > 0 && <div>
        <h3 className="text-sm font-medium">Pending invitations</h3>
        <ul className="mt-2 divide-y divide-line">
          {invites.map(invite => <li key={invite.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
            <span className="min-w-0 break-all">{invite.email} <span className="text-zinc-400">· {invite.role} · expires {new Date(invite.expires_at.endsWith("Z") ? invite.expires_at : invite.expires_at + "Z").toLocaleDateString()}</span></span>
            <Button type="button" size="sm" variant="ghost" onClick={() => revoke(invite)}>Revoke</Button>
          </li>)}
        </ul>
      </div>}
    </Card>
  </section>;
}
