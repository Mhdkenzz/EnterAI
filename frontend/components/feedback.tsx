"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";
const errorText = (error: unknown) => error instanceof Error ? error.message : "Request failed. Please try again.";
const statuses = ["new", "acknowledged", "investigating", "resolved", "closed"] as const;
type FeedbackStatus = typeof statuses[number];
type Feedback = {
  id: string; category: string; impact: string; description: string; page: string | null;
  diagnostics: Record<string, string | null>; release_identifier: string | null;
  status: FeedbackStatus; admin_note: string | null; created_at: string;
  reporter?: { id: string; name: string; email: string } | null;
  assignee?: { id: string; name: string; email: string } | null;
};
type AdminUser = { id: string; name: string; email?: string; role: "admin" | "manager" | "member"; active: boolean };

export function FeedbackDialog({ open, close }: { open: boolean; close: () => void }) {
  const [category, setCategory] = useState("bug");
  const [impact, setImpact] = useState("medium");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const firstField = useRef<HTMLSelectElement>(null);

  useEffect(() => {
    if (open) {
      setSent(false); setError(""); setDescription("");
      setTimeout(() => firstField.current?.focus(), 0);
    }
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, close]);
  if (!open) return null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try {
      await api("/feedback", {
        method: "POST",
        body: JSON.stringify({
          category, impact, description,
          page: window.location.pathname,
          client_version: process.env.NEXT_PUBLIC_APP_VERSION || "web-demo",
        }),
      });
      setSent(true);
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }

  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}>
    <section role="dialog" aria-modal="true" aria-labelledby="feedback-title" className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-line bg-panel p-6 shadow-2xl">
      <div className="flex items-start justify-between gap-4">
        <div><h2 id="feedback-title" className="text-lg font-semibold">Report a problem</h2><p className="mt-1 text-sm text-zinc-300">Tell us what happened. Do not include passwords, tokens or private customer data.</p></div>
        <Button type="button" variant="ghost" aria-label="Close report a problem dialog" onClick={close}>Close</Button>
      </div>
      {sent ? <div className="mt-6 space-y-4"><p role="status" className="text-sm text-emerald-300">Thanks — your report was sent to your workspace administrators.</p><Button type="button" onClick={close}>Done</Button></div> : <form className="mt-6 space-y-4" onSubmit={submit}>
        <label className="block text-sm text-zinc-300">Category<select ref={firstField} className={field} value={category} onChange={event => setCategory(event.target.value)}><option value="bug">Bug</option><option value="usability">Usability</option><option value="performance">Performance</option><option value="accessibility">Accessibility</option><option value="security">Security concern</option><option value="other">Other</option></select></label>
        <label className="block text-sm text-zinc-300">Impact<select className={field} value={impact} onChange={event => setImpact(event.target.value)}><option value="blocking">Blocking — cannot continue</option><option value="high">High — major feature affected</option><option value="medium">Medium — workaround exists</option><option value="low">Low — minor issue</option></select></label>
        <label className="block text-sm text-zinc-300">What happened?<textarea className={`${field} min-h-32`} required minLength={10} maxLength={5000} value={description} onChange={event => setDescription(event.target.value)} placeholder="Include the steps that led to the problem and what you expected." /><span className="mt-1 block text-xs text-zinc-500">{description.length}/5000</span></label>
        {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
        <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={close}>Cancel</Button><Button type="submit" disabled={busy}>{busy ? "Sending…" : "Send report"}</Button></div>
      </form>}
    </section>
  </div>;
}

export function AdminFeedbackSection({ users }: { users: AdminUser[] }) {
  const [reports, setReports] = useState<Feedback[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState<string | null>(null);
  const activeUsers = users.filter(user => user.active);

  async function load() {
    setLoading(true); setError("");
    try { setReports((await api<{ items: Feedback[] }>("/admin/feedback?limit=50")).items); }
    catch (err) { setError(errorText(err)); } finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function update(report: Feedback, next: Partial<Pick<Feedback, "status">> & { assignee_id?: string | null }) {
    setSaving(report.id); setError("");
    try {
      const saved = await api<Feedback>(`/admin/feedback/${report.id}`, { method: "PATCH", body: JSON.stringify(next) });
      setReports(current => current.map(item => item.id === saved.id ? saved : item));
    } catch (err) { setError(errorText(err)); } finally { setSaving(null); }
  }

  return <section aria-labelledby="feedback-heading" className="space-y-3">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 id="feedback-heading" className="text-lg font-semibold">Demo feedback</h2><p className="mt-1 text-sm text-zinc-300">Review reports from this workspace only. Assign an owner, acknowledge the report, and close it after the user-facing fix is verified.</p></div><Button type="button" variant="outline" onClick={() => void load()} disabled={loading}>Refresh</Button></div>
    {loading && <p role="status" className="text-sm text-zinc-300">Loading feedback…</p>}
    {error && <p role="alert" className="text-sm text-red-300">Could not load feedback: {error}</p>}
    {!loading && !reports.length && <Card className="p-5 text-sm text-zinc-300">No reports yet.</Card>}
    <div className="space-y-3">{reports.map(report => <Card key={report.id} className="space-y-3 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{report.category} · {report.impact} impact</p><p className="mt-1 text-xs text-zinc-400">{report.reporter?.name || "Unknown member"} · {new Date(report.created_at).toLocaleString()} · {report.release_identifier || "release unknown"}</p></div><span className="rounded-full border border-line px-2 py-1 text-xs">{report.status}</span></div>
      <p className="whitespace-pre-wrap break-words text-sm text-zinc-200">{report.description}</p>
      <p className="text-xs text-zinc-400">Page: {report.page || "not provided"}</p>
      <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm text-zinc-300">Status<select className={field} disabled={saving === report.id} value={report.status} onChange={event => void update(report, { status: event.target.value as FeedbackStatus })}>{statuses.map(status => <option key={status} value={status}>{status}</option>)}</select></label><label className="text-sm text-zinc-300">Owner<select className={field} disabled={saving === report.id} value={report.assignee?.id || ""} onChange={event => void update(report, { assignee_id: event.target.value || null })}><option value="">Unassigned</option>{activeUsers.map(member => <option key={member.id} value={member.id}>{member.name}</option>)}</select></label></div>
      {report.admin_note && <p className="text-sm text-amber-200">Admin note: {report.admin_note}</p>}
    </Card>)}</div>
  </section>;
}
