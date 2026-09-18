"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type DeletionRequest = {
  id: string; status: string; requested_at: string; scheduled_for: string; reason: string | null;
};

const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";
const errorText = (error: unknown) => error instanceof Error ? error.message : "Request failed. Please try again.";

function downloadJson(data: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
  URL.revokeObjectURL(url);
}

export function PrivacySettings({ userId }: { userId: string }) {
  const [status, setStatus] = useState<DeletionRequest | null>(null);
  const [statusError, setStatusError] = useState("");
  const [exportPassword, setExportPassword] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState("");
  const [exportOk, setExportOk] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteReason, setDeleteReason] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [cancelError, setCancelError] = useState("");

  function loadStatus() {
    api<DeletionRequest | null>("/privacy/deletion-status").then(setStatus).catch(err => setStatusError(errorText(err)));
  }
  useEffect(loadStatus, []);

  async function exportData(event: FormEvent) {
    event.preventDefault();
    setExportBusy(true); setExportError(""); setExportOk(false);
    try {
      const data = await api<Record<string, unknown>>("/privacy/export", { method: "POST", body: JSON.stringify({ password: exportPassword }) });
      downloadJson(data, `enterai-export-${userId}.json`);
      setExportOk(true); setExportPassword("");
    } catch (err) { setExportError(errorText(err)); } finally { setExportBusy(false); }
  }

  async function requestDeletion(event: FormEvent) {
    event.preventDefault();
    setDeleteBusy(true); setDeleteError("");
    try {
      const created = await api<DeletionRequest>("/privacy/delete-account", {
        method: "POST", body: JSON.stringify({ password: deletePassword, reason: deleteReason || undefined }),
      });
      setStatus(created); setDeletePassword(""); setDeleteReason(""); setConfirmingDelete(false);
    } catch (err) { setDeleteError(errorText(err)); } finally { setDeleteBusy(false); }
  }

  async function cancelDeletion() {
    if (!status) return;
    setCancelBusy(true); setCancelError("");
    try {
      await api<DeletionRequest>(`/privacy/deletion-requests/${status.id}/cancel`, { method: "POST" });
      setStatus(null);
    } catch (err) { setCancelError(errorText(err)); } finally { setCancelBusy(false); }
  }

  return <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
    <div>
      <h1 className="text-2xl font-semibold">Privacy</h1>
      <p className="mt-2 text-sm text-zinc-300">Export your data or delete your account. Both require your password again, even though you are signed in.</p>
    </div>

    <section aria-labelledby="export-heading" className="space-y-3">
      <h2 id="export-heading" className="text-lg font-semibold">Export my data</h2>
      <Card className="p-5">
        <form onSubmit={exportData} className="space-y-3">
          <label className="block text-sm text-zinc-300">Confirm your password
            <input type="password" required className={field} value={exportPassword} onChange={e => setExportPassword(e.target.value)} />
          </label>
          {exportError && <p role="alert" className="text-sm text-red-300">{exportError}</p>}
          {exportOk && <p role="status" className="text-sm text-emerald-300">Your data has downloaded as a JSON file.</p>}
          <Button type="submit" disabled={exportBusy}>{exportBusy ? "Preparing export…" : "Download my data"}</Button>
        </form>
      </Card>
    </section>

    <section aria-labelledby="delete-heading" className="space-y-3">
      <h2 id="delete-heading" className="text-lg font-semibold">Delete my account</h2>
      {statusError && <p role="alert" className="text-sm text-red-300">{statusError}</p>}
      {status ? <Card className="space-y-3 p-5">
        <p role="status" className="text-sm text-amber-200">
          Your account is scheduled for deletion on {new Date(status.scheduled_for).toLocaleDateString()}.
          You can still cancel this any time before then.
        </p>
        {cancelError && <p role="alert" className="text-sm text-red-300">{cancelError}</p>}
        <Button type="button" variant="outline" disabled={cancelBusy} onClick={cancelDeletion}>
          {cancelBusy ? "Canceling…" : "Cancel deletion"}
        </Button>
      </Card> : <Card className="space-y-3 p-5">
        <p className="text-sm text-zinc-300">
          Deleting your account anonymizes your profile (name, email, password) and signs you out everywhere.
          Tasks, comments, and documents you created stay in the workspace, no longer attributed to your real identity.
        </p>
        {!confirmingDelete ? <Button type="button" variant="outline" onClick={() => setConfirmingDelete(true)}>Delete my account</Button> :
          <form onSubmit={requestDeletion} className="space-y-3">
            <label className="block text-sm text-zinc-300">Confirm your password
              <input type="password" required className={field} value={deletePassword} onChange={e => setDeletePassword(e.target.value)} />
            </label>
            <label className="block text-sm text-zinc-300">Reason (optional)
              <input className={field} value={deleteReason} onChange={e => setDeleteReason(e.target.value)} />
            </label>
            {deleteError && <p role="alert" className="text-sm text-red-300">{deleteError}</p>}
            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={deleteBusy}>{deleteBusy ? "Requesting…" : "Confirm deletion"}</Button>
              <Button type="button" variant="ghost" onClick={() => setConfirmingDelete(false)}>Cancel</Button>
            </div>
          </form>}
      </Card>}
    </section>
  </div>;
}
