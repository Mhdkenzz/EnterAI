"use client";

import { useEffect, useState } from "react";
import { Mail, PlugZap, RefreshCw, Unplug } from "lucide-react";
import { API_BASE, api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type Integration = {
  id: string;
  provider: string;
  account: string;
  display_name: string;
  status: "connected" | "reauthorization_required" | "disconnected";
  scopes: string[];
  connected_by?: string | null;
  can_manage?: boolean;
  last_sync_at?: string | null;
  last_error?: string | null;
};

type IntegrationResponse = { gmail_available: boolean; slack_available: boolean; notion_available: boolean; items: Integration[] };

export function Integrations({ isAdmin }: { isAdmin: boolean }) {
  const [result, setResult] = useState<IntegrationResponse>({ gmail_available: false, slack_available: false, notion_available: false, items: [] });
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function load() {
    setError("");
    try { setResult(await api<IntegrationResponse>("/integrations")); }
    catch (cause: any) { setError(cause?.message || "Could not load integrations."); }
    finally { setLoading(false); }
  }

  useEffect(() => {
    const outcome = new URLSearchParams(window.location.search).get("gmail");
    const slackOutcome = new URLSearchParams(window.location.search).get("slack");
    const notionOutcome = new URLSearchParams(window.location.search).get("notion");
    if (outcome === "connected") setNotice("Gmail is connected. New inbox messages will appear in Requests shortly.");
    if (outcome === "error") setError("Gmail could not be connected. Try again, or check the Google consent settings.");
    if (slackOutcome === "connected") setNotice("Slack is connected. New messages from the authorized conversations will appear in Requests, visible to your workspace.");
    if (slackOutcome === "error") setError("Slack could not be connected. Check the Slack app settings and required permissions.");
    if (slackOutcome === "workspace-in-use") setError("This Slack workspace is already connected to another Enter AI organization. Disconnect that connection before connecting it here.");
    if (notionOutcome === "connected") setNotice("Notion is connected. New and edited pages you shared will appear in Requests shortly.");
    if (notionOutcome === "error") setError("Notion could not be connected. Check the Notion integration settings and try again.");
    if (outcome || slackOutcome || notionOutcome) window.history.replaceState({}, "", "/integrations");
    void load();
  }, []);

  async function disconnect(integration: Integration) {
    setBusyId(integration.id); setError(""); setNotice("");
    try {
      await api(`/integrations/${integration.id}/disconnect`, { method: "POST" });
      setNotice(`${integration.display_name || integration.account} has been disconnected.`);
      await load();
    } catch (cause: any) { setError(cause?.message || "Could not disconnect this account."); }
    finally { setBusyId(""); }
  }

  const connectUrl = `${API_BASE.replace(/\/$/, "")}/integrations/gmail/connect`;
  const gmailConnections = result.items.filter((item) => item.provider === "gmail");
  const slackConnections = result.items.filter((item) => item.provider === "slack" || item.provider === "slack_user");
  const slackConnectUrl = `${API_BASE.replace(/\/$/, "")}/integrations/slack/connect`;
  const slackUserConnectUrl = `${API_BASE.replace(/\/$/, "")}/integrations/slack/user/connect`;
  const notionConnectUrl = `${API_BASE.replace(/\/$/, "")}/integrations/notion/connect`;
  const notionConnections = result.items.filter((item) => item.provider === "notion");
  return (
    <div className="min-w-0 space-y-6">
      <div>
        <p className="page-kicker mb-2">Workspace setup</p>
        <h1 className="page-title">Integrations</h1>
        <p className="page-description mt-2">Connect work tools so incoming requests can enter the shared workspace queue.</p>
      </div>
      {notice && <p role="status" className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">{notice}</p>}
      {error && <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">{error}</p>}
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line p-5">
          <div className="flex min-w-0 gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-red-50 text-red-600"><Mail size={19} /></span>
            <div>
              <h2 className="font-semibold text-ink">Gmail</h2>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">Read-only access to a shared work mailbox. Enter AI checks for new Inbox messages about every 30 seconds, copies their text and source link into Requests, and has the CEO agent classify them. With an AI provider configured, clear work requests get an editable project plan for approval. Existing mail is not imported when you first connect.</p>
            </div>
          </div>
          {result.gmail_available && isAdmin ? (
            <a href={connectUrl} className="inline-flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-white shadow-sm hover:bg-accent-hover"><PlugZap size={16} /> Connect Gmail</a>
          ) : result.gmail_available ? <Badge>Admin connection</Badge> : <Badge>Not configured</Badge>}
        </div>
        {!result.gmail_available && <p className="border-b border-line bg-amber-50/60 px-5 py-3 text-sm text-amber-900">Gmail connection has not been configured for this Enter AI server yet.</p>}
        {result.gmail_available && !isAdmin && <p className="border-b border-line bg-slate-50 px-5 py-3 text-sm text-slate-600">Ask a workspace admin to connect a shared work mailbox.</p>}
        {loading ? <p className="p-5 text-sm text-slate-500">Loading connections…</p> : gmailConnections.length ? (
          <div className="divide-y divide-line/70">
            {gmailConnections.map((integration) => (
              <div key={integration.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><p className="truncate text-sm font-semibold text-ink">{integration.account}</p><Badge tone={integration.status === "connected" ? "green" : integration.status === "reauthorization_required" ? "amber" : "default"}>{integration.status === "reauthorization_required" ? "Reconnect required" : integration.status}</Badge></div>
                  <p className="mt-1 text-xs text-slate-500">Connected by {integration.connected_by || "workspace member"} · {integration.last_sync_at ? `Last checked ${new Date(integration.last_sync_at).toLocaleString()}` : "Waiting for first check"}</p>
                  {integration.last_error === "temporary_failure" && <p className="mt-1 text-xs text-amber-800">The last check had a temporary problem. Enter AI will retry.</p>}
                </div>
                <div className="flex gap-2">
                  {integration.status === "reauthorization_required" && isAdmin && <a href={connectUrl} className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-line bg-white px-3 text-xs font-semibold text-ink hover:bg-slate-50"><RefreshCw size={14} /> Reconnect</a>}
                  {integration.status !== "disconnected" && <Button variant="outline" size="sm" disabled={busyId === integration.id} onClick={() => disconnect(integration)}><Unplug size={14} /> {busyId === integration.id ? "Disconnecting…" : "Disconnect"}</Button>}
                </div>
              </div>
            ))}
          </div>
        ) : !loading ? <p className="p-5 text-sm text-slate-500">No Gmail accounts connected yet.</p> : null}
      </Card>
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line p-5">
          <div className="flex min-w-0 gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-violet-50 text-violet-700"><PlugZap size={19} /></span>
            <div>
              <h2 className="font-semibold text-ink">Slack</h2>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">An admin can connect the workspace bot to receive messages from channels it joins and DMs sent to the bot. Each person can also authorize their own direct messages with Slack. Messages copied into Requests are visible to workspace members for CEO review.</p>
            </div>
          </div>
          {result.slack_available && isAdmin ? (
            <a href={slackConnectUrl} className="inline-flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-white shadow-sm hover:bg-accent-hover"><PlugZap size={16} /> Connect Slack</a>
          ) : result.slack_available ? <Badge>Admin connection</Badge> : <Badge>Not configured</Badge>}
        </div>
        {!result.slack_available && <p className="border-b border-line bg-amber-50/60 px-5 py-3 text-sm text-amber-900">Slack OAuth and event signing are not configured for this Enter AI server yet.</p>}
          {result.slack_available && <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line bg-slate-50 px-5 py-3"><p className="max-w-2xl text-sm text-slate-600">Connect your DMs with your consent. Slack will share new direct and group messages you can access; they will be visible to everyone in this Enter AI workspace.</p><a href={slackUserConnectUrl} className="inline-flex min-h-9 shrink-0 items-center justify-center gap-2 rounded-lg border border-line bg-white px-3 text-xs font-semibold text-ink hover:bg-slate-100"><PlugZap size={14} /> Connect my DMs</a></div>}
        {loading ? <p className="p-5 text-sm text-slate-500">Loading connections…</p> : slackConnections.length ? (
          <div className="divide-y divide-line/70">
            {slackConnections.map((integration) => (
              <div key={integration.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><p className="truncate text-sm font-semibold text-ink">{integration.display_name || integration.account}</p><Badge tone={integration.status === "connected" ? "green" : integration.status === "reauthorization_required" ? "amber" : "default"}>{integration.status === "reauthorization_required" ? "Reconnect required" : integration.status}</Badge></div>
                  <p className="mt-1 text-xs text-slate-500">Connected by {integration.connected_by || "workspace member"} · {integration.last_sync_at ? `Last message ${new Date(integration.last_sync_at).toLocaleString()}` : "Waiting for the first message"}</p>
                  {integration.last_error === "temporary_failure" && <p className="mt-1 text-xs text-amber-800">Slack token maintenance had a temporary problem and will retry.</p>}
                  {integration.last_error === "workspace_conflict" && <p className="mt-1 text-xs text-red-700">Slack intake is paused because this workspace has duplicate organization connections. Disconnect the duplicate to resume.</p>}
                </div>
                <div className="flex gap-2">
                  {integration.status === "reauthorization_required" && integration.can_manage && <a href={integration.provider === "slack_user" ? slackUserConnectUrl : slackConnectUrl} className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-line bg-white px-3 text-xs font-semibold text-ink hover:bg-slate-50"><RefreshCw size={14} /> Reconnect</a>}
                  {integration.status !== "disconnected" && integration.can_manage && <Button variant="outline" size="sm" disabled={busyId === integration.id} onClick={() => disconnect(integration)}><Unplug size={14} /> {busyId === integration.id ? "Disconnecting…" : "Disconnect"}</Button>}
                </div>
              </div>
            ))}
          </div>
        ) : !loading ? <p className="p-5 text-sm text-slate-500">No Slack workspaces connected yet.</p> : null}
      </Card>
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line p-5">
          <div className="flex min-w-0 gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-slate-100 text-slate-700"><PlugZap size={19} /></span>
            <div>
              <h2 className="font-semibold text-ink">Notion</h2>
              <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">Connect a Notion workspace and choose the pages or databases to share in Notion’s access screen. Enter AI checks for edits about every 30 seconds, copies page text and its source link into Requests, and has the CEO agent review it. Existing content is not imported when you first connect.</p>
            </div>
          </div>
          {result.notion_available && isAdmin ? (
            <a href={notionConnectUrl} className="inline-flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-white shadow-sm hover:bg-accent-hover"><PlugZap size={16} /> Connect Notion</a>
          ) : result.notion_available ? <Badge>Admin connection</Badge> : <Badge>Not configured</Badge>}
        </div>
        {!result.notion_available && <p className="border-b border-line bg-amber-50/60 px-5 py-3 text-sm text-amber-900">Notion OAuth is not configured for this Enter AI server yet.</p>}
        {result.notion_available && !isAdmin && <p className="border-b border-line bg-slate-50 px-5 py-3 text-sm text-slate-600">Ask a workspace admin to connect Notion and select the pages to share.</p>}
        {loading ? <p className="p-5 text-sm text-slate-500">Loading connections…</p> : notionConnections.length ? (
          <div className="divide-y divide-line/70">
            {notionConnections.map((integration) => (
              <div key={integration.id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><p className="truncate text-sm font-semibold text-ink">{integration.display_name || integration.account}</p><Badge tone={integration.status === "connected" ? "green" : integration.status === "reauthorization_required" ? "amber" : "default"}>{integration.status === "reauthorization_required" ? "Reconnect required" : integration.status}</Badge></div>
                  <p className="mt-1 text-xs text-slate-500">Connected by {integration.connected_by || "workspace member"} · {integration.last_sync_at ? `Last checked ${new Date(integration.last_sync_at).toLocaleString()}` : "Waiting for first check"}</p>
                  {integration.last_error === "temporary_failure" && <p className="mt-1 text-xs text-amber-800">Notion could not be checked. Enter AI will retry.</p>}
                </div>
                <div className="flex gap-2">
                  {integration.status === "reauthorization_required" && isAdmin && <a href={notionConnectUrl} className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-line bg-white px-3 text-xs font-semibold text-ink hover:bg-slate-50"><RefreshCw size={14} /> Reconnect</a>}
                  {integration.status !== "disconnected" && <Button variant="outline" size="sm" disabled={busyId === integration.id} onClick={() => disconnect(integration)}><Unplug size={14} /> {busyId === integration.id ? "Disconnecting…" : "Disconnect"}</Button>}
                </div>
              </div>
            ))}
          </div>
        ) : !loading ? <p className="p-5 text-sm text-slate-500">No Notion workspaces connected yet.</p> : null}
      </Card>
    </div>
  );
}
