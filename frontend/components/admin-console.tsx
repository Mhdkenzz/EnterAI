"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { BillingSection } from "@/components/billing-settings";
import { TeamInvitesSection } from "@/components/team-invites";

type Settings = {
  execution_enabled: boolean;
  ceo_full_access_enabled: boolean;
  agent_workspace_enabled: boolean;
  agent_workspace_available: boolean;
  agent_workspace_id: string;
};
type AdminUser = { id: string; name: string; email?: string; role: "admin" | "manager" | "member"; active: boolean };
type AgentUsage = {
  execution_enabled: boolean;
  id: string; name: string; status: string; last_execution_at: string | null;
  consecutive_task_failures: number; calls: number; failures: number;
  executions: number; execution_failures: number;
};
type Usage = {
  totals: { calls: number; failures: number; duration_ms: number; executions: number; execution_failures: number };
  agents: AgentUsage[]; provider_mode: string;
};
type AIProviderSettings = {
  provider: string;
  configured_provider: string;
  model: string;
  api_key_configured: boolean;
  requests_per_minute: number;
  maximum_requests_per_minute: number;
};
type AuditEvent = {
  id: string; actor_id: string | null; initiator_id: string | null; actor_name?: string | null; initiator_name?: string | null; source: string;
  action: string; entity_type: string; entity_id: string | null; detail: unknown; created_at: string;
};
type AuditPage = { items: AuditEvent[]; total: number; offset: number; limit: number };
type AgentAccess = {
  agent_id: string;
  inherited: { assigned_task_ids: string[]; baseline_tools: string[] };
  effective_tools: string[];
  effective_project_ids: string[];
  effective_document_ids: string[];
  projects: { id: string; name: string; code: string }[];
  documents: { id: string; file_name: string; project_id: string | null }[];
  tools: string[];
  grants: { id: string; resource_type: "project" | "document" | "tool"; resource_id: string; permission: "read" | "write" | "use"; reason: string | null; active: boolean; created_at: string; revoked_at: string | null }[];
};
type Filters = { q: string; action: string; source: string; actor_id: string; entity_type: string; start: string; end: string };
const emptyFilters: Filters = { q: "", action: "", source: "", actor_id: "", entity_type: "", start: "", end: "" };
const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";
const errorText = (error: unknown) => error instanceof Error ? error.message : "Request failed. Please try again.";
// Backend timestamps without a zone are UTC, not the browser's local time.
const timestamp = (value: string | null) => value ? new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`).toLocaleString() : "Never";

function ExecutionSwitch({ label, enabled, disabled, onChange }: {
  label: string; enabled: boolean; disabled: boolean; onChange: () => void;
}) {
  return <Button type="button" variant="outline" role="switch" aria-label={label}
    aria-checked={enabled} disabled={disabled} onClick={onChange}>
    {enabled ? "Enabled" : "Disabled"}
  </Button>;
}

function AgentAccessPanel({ agentId, agentName }: { agentId: string; agentName: string }) {
  const [access, setAccess] = useState<AgentAccess | null>(null);
  const [resource, setResource] = useState("");
  const [permission, setPermission] = useState<"read" | "write" | "use">("read");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function reload(signal?: AbortSignal) {
    const value = await api<AgentAccess>(`/admin/agents/${agentId}/access`, { signal });
    if (!signal?.aborted) {
      setAccess(value);
      if (!resource && value.projects.length) setResource(`project:${value.projects[0].id}`);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    reload(controller.signal).catch(reason => { if (!controller.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [agentId]);

  const [kind, id] = resource.split(":", 2);
  const choices = kind === "project" ? ["read", "write"] : kind === "document" ? ["read"] : ["use"];
  async function grant(event: FormEvent) {
    event.preventDefault();
    if (!kind || !id || pending) return;
    setPending(true); setError(""); setNotice("");
    try {
      await api(`/admin/agents/${agentId}/access`, { method: "POST", body: JSON.stringify({ resource_type: kind, resource_id: id, permission, reason: reason.trim() || undefined }) });
      await reload(); setReason(""); setNotice("Access grant saved.");
    } catch (cause) { setError(errorText(cause)); }
    finally { setPending(false); }
  }
  async function revoke(grantId: string) {
    if (pending) return;
    setPending(true); setError(""); setNotice("");
    try {
      await api(`/admin/agents/${agentId}/access/${grantId}`, { method: "DELETE" });
      await reload(); setNotice("Access revoked.");
    } catch (cause) { setError(errorText(cause)); }
    finally { setPending(false); }
  }
  return <details className="rounded-lg border border-line p-3">
    <summary className="cursor-pointer text-sm font-medium">Access controls for {agentName}</summary>
    {loading ? <p role="status" className="mt-3 text-sm text-zinc-300">Loading access…</p> : access && <div className="mt-3 space-y-3">
      <p className="text-xs text-zinc-300">Effective scope: {access.inherited.assigned_task_ids.length} assigned task(s), {access.effective_project_ids.length} project(s), and {access.effective_document_ids.length} document(s). Effective tools: {access.effective_tools.join(", ") || "none"}.</p>
      <form className="grid gap-2 sm:grid-cols-2" onSubmit={event => void grant(event)}>
        <label className="text-sm text-zinc-300">Resource
          <select className={field} value={resource} onChange={event => { setResource(event.target.value); const next = event.target.value.startsWith("tool:") ? "use" : "read"; setPermission(next); }}>
            {access.projects.map(item => <option key={`p-${item.id}`} value={`project:${item.id}`}>Project · {item.name} ({item.code})</option>)}
            {access.documents.map(item => <option key={`d-${item.id}`} value={`document:${item.id}`}>Document · {item.file_name}</option>)}
            {access.tools.map(item => <option key={`t-${item}`} value={`tool:${item}`}>Tool · {item}</option>)}
          </select>
        </label>
        <label className="text-sm text-zinc-300">Permission
          <select className={field} value={permission} onChange={event => setPermission(event.target.value as "read" | "write" | "use")}>
            {choices.map(item => <option key={item} value={item}>{item === "use" ? "Use tool" : item === "write" ? "Read and write" : "Read"}</option>)}
          </select>
        </label>
        <label className="text-sm text-zinc-300 sm:col-span-2">Reason (optional)
          <input className={field} maxLength={500} value={reason} onChange={event => setReason(event.target.value)} placeholder="Why this access is needed" />
        </label>
        <Button className="sm:col-span-2" type="submit" disabled={pending || !resource}>{pending ? "Saving…" : "Grant access"}</Button>
      </form>
      <div className="space-y-2">
        <h5 className="text-sm font-medium">Explicit grants</h5>
        {!access.grants.length && <p className="text-xs text-zinc-400">No explicit grants.</p>}
        {access.grants.map(grant => <div key={grant.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md bg-canvas p-2 text-xs">
          <span>{grant.resource_type} · {grant.resource_id} · {grant.permission} · {grant.active ? "active" : "revoked"}{grant.reason ? ` · ${grant.reason}` : ""}</span>
          {grant.active && <Button type="button" variant="outline" disabled={pending} onClick={() => void revoke(grant.id)}>Revoke</Button>}
        </div>)}
      </div>
    </div>}
    {error && <p role="alert" className="mt-2 text-sm text-red-300">{error}</p>}
    {notice && <p role="status" className="mt-2 text-sm text-sky-200">{notice}</p>}
  </details>;
}

function AIProviderWorkspaceSettings({ onSaved }: { onSaved: () => void }) {
  const [settings, setSettings] = useState<AIProviderSettings | null>(null);
  const [provider, setProvider] = useState<"ifm" | "openrouter">("ifm");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("IFM/K2-Horizon-375B-A23B");
  const [rpm, setRpm] = useState(20);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    api<AIProviderSettings>("/admin/ai-provider", { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        setSettings(value);
        setProvider(value.configured_provider === "openrouter" ? "openrouter" : "ifm");
        setModel(value.model || "IFM/K2-Horizon-375B-A23B"); setRpm(value.requests_per_minute);
      })
      .catch(reason => { if (!controller.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);

  async function save(event?: FormEvent, clear = false) {
    event?.preventDefault();
    if (pending) return;
    setPending(true); setError(""); setNotice("");
    try {
      const body: { provider: "ifm" | "openrouter"; model?: string; requests_per_minute: number; api_key?: string; clear_api_key?: boolean } = {
        provider, requests_per_minute: rpm,
      };
      if (provider === "openrouter") body.model = model.trim();
      if (clear) body.clear_api_key = true;
      else if (apiKey.trim()) body.api_key = apiKey.trim();
      const next = await api<AIProviderSettings>("/admin/ai-provider", { method: "PATCH", body: JSON.stringify(body) });
      setSettings(next); setProvider(next.configured_provider === "openrouter" ? "openrouter" : "ifm"); setModel(next.model || "IFM/K2-Horizon-375B-A23B"); setRpm(next.requests_per_minute); setApiKey("");
      setNotice(clear ? "Saved AI key removed." : "AI provider settings saved.");
      onSaved();
    } catch (reason) { setError(errorText(reason)); }
    finally { setPending(false); }
  }

  return <section aria-labelledby="ai-provider-heading" className="space-y-3">
    <div>
      <h2 id="ai-provider-heading" className="text-lg font-semibold">Workspace AI provider</h2>
      <p className="mt-1 text-sm text-zinc-300">Save an IFM K2 Horizon or OpenRouter key for this workspace. It is encrypted at rest and never shown again. Requests and relevant workspace context are sent to the selected provider.</p>
    </div>
    <Card className="space-y-4 p-5">
      {loading && <p role="status" className="text-sm text-zinc-300">Loading provider settings…</p>}
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
      {notice && <p role="status" className="text-sm text-sky-200">{notice}</p>}
      {settings && <>
        <p className="text-sm text-zinc-300">Active provider: <strong>{settings.provider}</strong>{settings.api_key_configured ? ` (workspace ${settings.configured_provider === "ifm" ? "IFM" : "OpenRouter"} key)` : " (server default)"}</p>
        <form className="grid gap-3 sm:grid-cols-2" onSubmit={event => void save(event)}>
          <label className="text-sm text-zinc-300">Provider
            <select className={field} value={provider} onChange={event => { setProvider(event.target.value as "ifm" | "openrouter"); setApiKey(""); }}>
              <option value="ifm">IFM · K2 Horizon</option><option value="openrouter">OpenRouter</option>
            </select>
          </label>
          <label className="text-sm text-zinc-300">Requests per minute (1–{settings.maximum_requests_per_minute})
            <input className={field} type="number" min={1} max={settings.maximum_requests_per_minute} value={rpm} required onChange={event => setRpm(Number(event.target.value))} />
          </label>
          {provider === "ifm" ? <div className="text-sm text-zinc-300 sm:col-span-2">Model and endpoint<div className={`${field} text-ink`}>IFM/K2-Horizon-375B-A23B · https://api.ifm.ai/v1</div></div> : <label className="text-sm text-zinc-300 sm:col-span-2">OpenRouter model ID
            <input className={field} value={model} maxLength={200} required onChange={event => setModel(event.target.value)} placeholder="openai/gpt-4o-mini" />
          </label>}
          <label className="text-sm text-zinc-300 sm:col-span-2">{provider === "ifm" ? "IFM API key" : "OpenRouter API key"}
            <input className={field} type="password" autoComplete="new-password" value={apiKey} maxLength={512} onChange={event => setApiKey(event.target.value)} placeholder={settings.api_key_configured && settings.configured_provider === provider ? "Saved key is hidden; enter a new key to replace it" : `Enter ${provider === "ifm" ? "IFM" : "OpenRouter"} key`} />
          </label>
          <div className="flex flex-wrap gap-2 sm:col-span-2">
            <Button type="submit" disabled={pending}>{pending ? "Saving…" : "Save AI provider"}</Button>
            {settings.api_key_configured && <Button type="button" variant="outline" disabled={pending} onClick={() => {
              if (window.confirm("Remove the saved workspace AI key? This workspace will return to the server default AI provider.")) void save(undefined, true);
            }}>Remove saved key</Button>}
          </div>
        </form>
        <p className="text-xs text-zinc-400">Saving a key requires SECRETS_ENCRYPTION_KEY on the server. The request limit applies across all API replicas for this organization.</p>
      </>}
    </Card>
  </section>;
}

export function AdminConsole({ currentUserId }: { currentUserId: string }) {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [mutationError, setMutationError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState(false);
  const [revision, setRevision] = useState(0);
  const [auditRevision, setAuditRevision] = useState(0);
  const mounted = useRef(true);
  const mutationLock = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setLoadError("");
    Promise.all([
      api<Usage>("/admin/usage", { signal: controller.signal }),
      api<Settings>("/admin/settings", { signal: controller.signal }),
      api<AdminUser[]>("/admin/users", { signal: controller.signal }),
    ]).then(([nextUsage, nextSettings, nextUsers]) => {
      if (controller.signal.aborted) return;
      setUsage(nextUsage); setSettings(nextSettings); setUsers(nextUsers);
    }).catch(error => { if (!controller.signal.aborted) setLoadError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);

  // Do not optimistically display durable controls as saved. Serialize mutations
  // so a fast double click cannot issue conflicting writes; show the server state.
  async function mutate(action: () => Promise<void>, message: string) {
    if (mutationLock.current) return;
    mutationLock.current = true;
    setPending(true); setMutationError(""); setNotice("");
    try {
      await action();
      if (mounted.current) { setNotice(message); setAuditRevision(value => value + 1); }
    } catch (error) {
      if (mounted.current) setMutationError(errorText(error));
    } finally {
      mutationLock.current = false;
      if (mounted.current) setPending(false);
    }
  }

  // Re-read server state after execution mutations so status and effective
  // execution reflect the stored settings, never the pre-mutation snapshot.
  async function reloadExecutionState() {
    const [nextUsage, nextSettings] = await Promise.all([api<Usage>("/admin/usage"), api<Settings>("/admin/settings")]);
    if (mounted.current) { setUsage(nextUsage); setSettings(nextSettings); }
  }

  return <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
    <div>
      <h1 className="text-2xl font-semibold">Admin console</h1>
      <p className="mt-2 text-sm text-zinc-300">Organization governance, execution controls and audit history.</p>
    </div>
    {loading && <p role="status" className="text-sm text-zinc-300">Loading admin console…</p>}
    {loadError && <Card className="space-y-3 p-5">
      <p role="alert" className="text-sm text-red-300">Could not load admin console: {loadError}</p>
      <Button type="button" onClick={() => setRevision(value => value + 1)}>Retry admin console</Button>
    </Card>}
    {mutationError && <p role="alert" className="rounded-lg border border-red-400/40 p-3 text-sm text-red-300">{mutationError}</p>}
    <p role="status" aria-live="polite" className="text-sm text-sky-200">{pending ? "Saving changes…" : notice}</p>
    <BillingSection />
    <TeamInvitesSection />
    <AIProviderWorkspaceSettings onSaved={() => setRevision(value => value + 1)} />
    {!loading && !loadError && usage && settings && <>
      <section aria-labelledby="usage-heading" className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="usage-heading" className="text-lg font-semibold">Usage overview</h2>
            <p className="text-sm text-zinc-300">AI provider calls and agent runs for this workspace. Token and cost figures are not tracked yet.</p>
            <p className="mt-1 text-sm text-sky-200">Provider mode: {usage.provider_mode}</p>
          </div>
          <Button type="button" variant="outline" disabled={pending} onClick={() => { setRevision(value => value + 1); setAuditRevision(value => value + 1); }}>Refresh admin data</Button>
        </div>
        <dl className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {([
            ["Provider calls", usage.totals.calls], ["Provider failures", usage.totals.failures],
            ["Provider duration (ms)", usage.totals.duration_ms], ["Executions", usage.totals.executions],
            ["Execution failures", usage.totals.execution_failures],
          ] as const).map(([label, value]) => <Card key={label} className="min-w-0 p-4">
            <dt className="text-sm text-zinc-300">{label}</dt><dd className="mt-2 text-2xl font-semibold">{value.toLocaleString()}</dd>
          </Card>)}
        </dl>
      </section>
      <section aria-labelledby="execution-heading" className="space-y-3">
        <h2 id="execution-heading" className="text-lg font-semibold">Execution controls</h2>
        <Card className="flex flex-wrap items-center justify-between gap-3 p-5">
          <div className="min-w-0 flex-1">
            <h3 className="font-medium">Organization execution</h3>
            <p className="mt-1 text-sm text-zinc-300">{settings.execution_enabled ? "Organization execution is enabled." : "Organization execution is disabled for all agents."}</p>
            <p className="mt-1 text-sm text-zinc-300">Durable settings control future execution attempts; they do not cancel calls already in progress.</p>
          </div>
          <ExecutionSwitch label="Organization execution" enabled={settings.execution_enabled} disabled={pending}
            onChange={() => mutate(async () => {
              await api<Settings>("/admin/settings", { method: "PATCH", body: JSON.stringify({ execution_enabled: !settings.execution_enabled }) });
              await reloadExecutionState();
            }, "Organization execution setting saved.")} />
        </Card>
        <Card className="flex flex-wrap items-center justify-between gap-3 border-amber-300/40 p-5">
          <div className="min-w-0 flex-1">
            <h3 className="font-medium">CEO full access</h3>
            <p className="mt-1 text-sm text-zinc-300">
              {settings.ceo_full_access_enabled
                ? "The CEO can make ordinary workspace changes directly through its UI session."
                : "The CEO must get human confirmation before workspace changes run."}
            </p>
            <p className="mt-1 text-sm text-zinc-300">
              Direct mode skips proposals for workspace data changes. Authentication, billing, privacy, organization security and human-admin controls remain protected.
            </p>
          </div>
          <ExecutionSwitch label="CEO full access" enabled={settings.ceo_full_access_enabled} disabled={pending}
            onChange={() => {
              const enabled = !settings.ceo_full_access_enabled;
              if (enabled && !window.confirm("Enable CEO full access? The CEO will be able to create, edit, or delete ordinary workspace data without asking a human to confirm each change.")) return;
              void mutate(async () => {
                await api<Settings>("/admin/settings", { method: "PATCH", body: JSON.stringify({ ceo_full_access_enabled: enabled }) });
                await reloadExecutionState();
              }, `CEO full access ${enabled ? "enabled" : "disabled"}.`);
            }} />
        </Card>
        <Card className="flex flex-wrap items-center justify-between gap-3 p-5">
          <div className="min-w-0 flex-1">
            <h3 className="font-medium">Organization repository</h3>
            <p className="mt-1 text-sm text-zinc-300">
              {settings.agent_workspace_enabled
                ? "Agents can inspect and search this organization's isolated repository."
                : "Repository tools are disabled for this organization."}
            </p>
            <p className="mt-1 text-sm text-zinc-300">
              Agents can read and search source, inspect Git status, create and edit source files for requested work, and run fixed syntax/type checks. File removals still need human confirmation. Retrieved snippets go to your configured AI provider; arbitrary shell commands are unavailable.
            </p>
            {usage.provider_mode !== "tool_calling" && <p className="mt-2 text-sm text-amber-700">Configure a structured tool-calling AI provider to let agents use repository tools.</p>}
            {!settings.agent_workspace_available && <p className="mt-2 text-sm text-amber-700">Provision a separate Git checkout at AGENT_WORKSPACE_ROOT/{settings.agent_workspace_id} on the API host before enabling this organization.</p>}
          </div>
          <ExecutionSwitch label="Organization repository access" enabled={settings.agent_workspace_enabled} disabled={pending || !settings.agent_workspace_available}
            onChange={() => {
              const enabled = !settings.agent_workspace_enabled;
              if (enabled && !window.confirm("Enable this organization's isolated repository for agents? Agents can read permitted source files from its checkout; when asked to do code work, they can create and edit allowed files directly. Retrieved snippets will be sent to your configured AI provider. File removals still require human review, and arbitrary shell commands are unavailable.")) return;
              void mutate(async () => {
                await api<Settings>("/admin/settings", { method: "PATCH", body: JSON.stringify({ agent_workspace_enabled: enabled }) });
                await reloadExecutionState();
              }, `Organization repository access ${enabled ? "enabled" : "disabled"}.`);
            }} />
        </Card>
        <h3 className="font-medium">Agent usage and execution</h3>
        {!usage.agents.length && <p className="text-sm text-zinc-300">No agents configured.</p>}
        <div className="grid min-w-0 gap-3 xl:grid-cols-2">
          {usage.agents.map(agent => <Card key={agent.id} className="min-w-0 space-y-3 p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1"><h4 className="font-medium">{agent.name}</h4><p className="text-xs text-zinc-300">{agent.id}</p></div>
              <ExecutionSwitch label={`Execution for ${agent.name}`} enabled={agent.execution_enabled} disabled={pending}
                onChange={() => mutate(async () => {
                  await api<Settings & { id: string }>(`/admin/agents/${agent.id}/execution`, { method: "PATCH", body: JSON.stringify({ execution_enabled: !agent.execution_enabled }) });
                  await reloadExecutionState();
                }, `Execution setting saved for ${agent.name}.`)} />
            </div>
            <p className="text-sm text-zinc-300">Status: {agent.status} · Effective execution: {settings.execution_enabled && agent.execution_enabled ? "enabled" : "disabled"}</p>
            {!settings.execution_enabled && agent.execution_enabled && <p className="text-sm text-amber-200">Agent setting is enabled, but organization execution is disabled.</p>}
            <dl className="grid grid-cols-2 gap-2 text-sm">
              {([
                ["Provider calls", agent.calls], ["Provider failures", agent.failures], ["Executions", agent.executions],
                ["Execution failures", agent.execution_failures], ["Consecutive task failures", agent.consecutive_task_failures],
                ["Last execution", timestamp(agent.last_execution_at)],
              ] as const).map(([label, value]) => <div key={label} className="min-w-0"><dt className="text-zinc-300">{label}</dt><dd>{value}</dd></div>)}
            </dl>
            <AgentAccessPanel agentId={agent.id} agentName={agent.name} />
          </Card>)}
        </div>
      </section>
      <section aria-labelledby="roles-heading" className="space-y-3">
        <h2 id="roles-heading" className="text-lg font-semibold">Members and roles</h2>
        <p className="text-sm text-zinc-300">
          Admins run the workspace, billing and members. Managers can also archive or delete projects, delete any task and create teams.
          Members work on tasks and can delete only the tasks they created. You cannot change or remove yourself.
        </p>
        {!users.length && <p className="text-sm text-zinc-300">No human users found.</p>}
        <div className="grid min-w-0 gap-3 xl:grid-cols-2">
          {users.map(user => <RoleForm key={`${user.id}-${user.role}-${user.active}`} user={user} self={user.id === currentUserId} pending={pending}
            save={role => mutate(async () => {
              const next = await api<AdminUser>(`/admin/users/${user.id}/role`, { method: "PATCH", body: JSON.stringify({ role }) });
              if (mounted.current) setUsers(current => current.map(item => item.id === next.id ? next : item));
            }, "Role updated.")}
            setActive={active => {
              if (!active && !window.confirm(`Remove ${user.name} from the workspace? They are signed out now and cannot sign in again until restored. Their tasks and history stay.`)) return;
              void mutate(async () => {
                const next = await api<AdminUser>(`/admin/users/${user.id}/active`, { method: "PATCH", body: JSON.stringify({ active }) });
                if (mounted.current) setUsers(current => current.map(item => item.id === next.id ? next : item));
              }, active ? "Member restored." : "Member removed.");
            }} />)}
        </div>
      </section>
    </>}
    <PrivacyAdminSection />
    <AuditLog revision={auditRevision} />
  </div>;
}

type RetentionPolicy = {
  chat_retention_days: number | null; audit_retention_days: number | null;
  activity_retention_days: number | null; document_retention_days: number | null;
};
type DeletionRequest = {
  id: string; target_type: string; target_id: string; status: string;
  reason: string | null; requested_at: string; scheduled_for: string;
};

const retentionFields = [
  ["chat_retention_days", "Chats (days)"], ["audit_retention_days", "Audit log (days)"],
  ["activity_retention_days", "Activity (days)"], ["document_retention_days", "Documents (days)"],
] as const;

function downloadJson(data: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
  URL.revokeObjectURL(url);
}

function PrivacyAdminSection() {
  const [policy, setPolicy] = useState<RetentionPolicy | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [requests, setRequests] = useState<DeletionRequest[]>([]);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saveOk, setSaveOk] = useState(false);
  const [exportPassword, setExportPassword] = useState("");
  const [exportError, setExportError] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [confirmingOrgDelete, setConfirmingOrgDelete] = useState(false);

  useEffect(() => {
    // Aborting a superseded request (StrictMode's dev-only double-invoke of this
    // effect is the common case, same as AdminConsole's own loader above) stops
    // its response from landing after the current one and clobbering `draft`
    // with stale/empty values -- including over whatever the admin has since
    // typed into the retention form but not saved yet.
    const controller = new AbortController();
    Promise.all([
      api<RetentionPolicy>("/admin/privacy/retention", { signal: controller.signal }),
      api<DeletionRequest[]>("/admin/privacy/deletion-requests", { signal: controller.signal }),
    ]).then(([nextPolicy, nextRequests]) => {
      if (controller.signal.aborted) return;
      setPolicy(nextPolicy); setRequests(nextRequests);
      setDraft(Object.fromEntries(retentionFields.map(([key]) => [key, nextPolicy[key] != null ? String(nextPolicy[key]) : ""])));
    }).catch(err => { if (!controller.signal.aborted) setLoadError(errorText(err)); });
    return () => controller.abort();
  }, []);

  function load() {
    Promise.all([
      api<RetentionPolicy>("/admin/privacy/retention"),
      api<DeletionRequest[]>("/admin/privacy/deletion-requests"),
    ]).then(([nextPolicy, nextRequests]) => {
      setPolicy(nextPolicy); setRequests(nextRequests);
      setDraft(Object.fromEntries(retentionFields.map(([key]) => [key, nextPolicy[key] != null ? String(nextPolicy[key]) : ""])));
    }).catch(err => setLoadError(errorText(err)));
  }

  async function saveRetention(event: FormEvent) {
    event.preventDefault();
    setSaveError(""); setSaveOk(false);
    try {
      const body = Object.fromEntries(retentionFields.map(([key]) => [key, draft[key]?.trim() ? Number(draft[key]) : null]));
      const next = await api<RetentionPolicy>("/admin/privacy/retention", { method: "PATCH", body: JSON.stringify(body) });
      setPolicy(next); setSaveOk(true);
    } catch (err) { setSaveError(errorText(err)); }
  }

  async function exportOrganization(event: FormEvent) {
    event.preventDefault();
    setExportBusy(true); setExportError("");
    try {
      const data = await api<Record<string, unknown>>("/admin/privacy/export", { method: "POST", body: JSON.stringify({ password: exportPassword }) });
      downloadJson(data, "enterai-organization-export.json");
      setExportPassword("");
    } catch (err) { setExportError(errorText(err)); } finally { setExportBusy(false); }
  }

  async function requestOrgDeletion(event: FormEvent) {
    event.preventDefault();
    setDeleteBusy(true); setDeleteError("");
    try {
      await api<DeletionRequest>("/admin/privacy/delete-organization", { method: "POST", body: JSON.stringify({ password: deletePassword }) });
      setDeletePassword(""); setConfirmingOrgDelete(false); load();
    } catch (err) { setDeleteError(errorText(err)); } finally { setDeleteBusy(false); }
  }

  async function cancelRequest(id: string) {
    await api<DeletionRequest>(`/admin/privacy/deletion-requests/${id}/cancel`, { method: "POST" });
    load();
  }

  return <section aria-labelledby="privacy-heading" className="min-w-0 space-y-3">
    <h2 id="privacy-heading" className="text-lg font-semibold">Privacy & retention</h2>
    {loadError && <p role="alert" className="text-sm text-red-300">{loadError}</p>}
    {policy && <>
      <Card className="min-w-0 space-y-3 p-5">
        <h3 className="font-medium">Retention policy</h3>
        <p className="text-sm text-zinc-300">Leave a field blank to keep that category forever.</p>
        <form onSubmit={saveRetention} className="grid min-w-0 gap-3 sm:grid-cols-2">
          {retentionFields.map(([key, label]) => <label key={key} className="block text-sm text-zinc-300">{label}
            <input type="number" min={1} className={field} value={draft[key] ?? ""}
              onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} />
          </label>)}
          <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
            <Button type="submit">Save retention policy</Button>
            {saveOk && <p role="status" className="text-sm text-emerald-300">Saved.</p>}
          </div>
          {saveError && <p role="alert" className="text-sm text-red-300 sm:col-span-2">{saveError}</p>}
        </form>
      </Card>
      <Card className="min-w-0 space-y-3 p-5">
        <h3 className="font-medium">Export organization data</h3>
        <form onSubmit={exportOrganization} className="space-y-3">
          <label className="block text-sm text-zinc-300">Confirm your password
            <input type="password" required className={field} value={exportPassword} onChange={e => setExportPassword(e.target.value)} />
          </label>
          {exportError && <p role="alert" className="text-sm text-red-300">{exportError}</p>}
          <Button type="submit" disabled={exportBusy}>{exportBusy ? "Preparing export…" : "Download organization data"}</Button>
        </form>
      </Card>
      <Card className="min-w-0 space-y-3 p-5">
        <h3 className="font-medium">Delete this organization</h3>
        <p className="text-sm text-zinc-300">Permanently deletes every project, task, document, and user in this workspace after a grace period. This cannot be undone once it runs.</p>
        {!confirmingOrgDelete ? <Button type="button" variant="outline" onClick={() => setConfirmingOrgDelete(true)}>Delete organization</Button> :
          <form onSubmit={requestOrgDeletion} className="space-y-3">
            <label className="block text-sm text-zinc-300">Confirm your password
              <input type="password" required className={field} value={deletePassword} onChange={e => setDeletePassword(e.target.value)} />
            </label>
            {deleteError && <p role="alert" className="text-sm text-red-300">{deleteError}</p>}
            <div className="flex flex-wrap gap-2">
              <Button type="submit" disabled={deleteBusy}>{deleteBusy ? "Requesting…" : "Confirm organization deletion"}</Button>
              <Button type="button" variant="ghost" onClick={() => setConfirmingOrgDelete(false)}>Cancel</Button>
            </div>
          </form>}
      </Card>
      {!!requests.length && <Card className="min-w-0 space-y-3 p-5">
        <h3 className="font-medium">Deletion requests</h3>
        {requests.map(r => <div key={r.id} className="flex flex-wrap items-center justify-between gap-2 border-b border-line/60 py-2 text-sm last:border-0">
          <span>{r.target_type} · {r.status} · scheduled {new Date(r.scheduled_for).toLocaleDateString()}</span>
          {r.status === "pending" && <Button type="button" variant="outline" size="sm" onClick={() => cancelRequest(r.id)}>Cancel</Button>}
        </div>)}
      </Card>}
    </>}
  </section>;
}

function RoleForm({ user, self, pending, save, setActive }: {
  user: AdminUser; self: boolean; pending: boolean; save: (role: AdminUser["role"]) => Promise<void>; setActive: (active: boolean) => void;
}) {
  const [role, setRole] = useState(user.role);
  return <Card className="min-w-0 p-5">
    <form className="space-y-3" onSubmit={event => { event.preventDefault(); if (!self && role !== user.role) void save(role); }}>
      <h3 className="font-medium">{user.name}</h3>
      {user.email && <p className="break-all text-sm text-zinc-400">{user.email}</p>}
      <p className="text-sm text-zinc-300">{user.active ? "Active" : "Removed"} · Current role: {user.role}</p>
      <label className="block text-sm text-zinc-300">Role for {user.name}
        <select className={field} value={role} disabled={self || pending} onChange={event => setRole(event.target.value as AdminUser["role"])}>
          <option value="admin">Admin</option><option value="manager">Manager</option><option value="member">Member</option>
        </select>
      </label>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="outline" aria-label={`Save role for ${user.name}`} disabled={self || pending || role === user.role}>Save role</Button>
        {!self && <Button type="button" variant="ghost" aria-label={`${user.active ? "Remove" : "Restore"} ${user.name}`} disabled={pending}
          onClick={() => setActive(!user.active)}>{user.active ? "Remove from workspace" : "Restore access"}</Button>}
      </div>
    </form>
  </Card>;
}

function AuditLog({ revision }: { revision: number }) {
  const [draft, setDraft] = useState<Filters>(emptyFilters);
  const [query, setQuery] = useState({ filters: emptyFilters, offset: 0, retry: 0 });
  const [data, setData] = useState<AuditPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ offset: String(query.offset), limit: "50" });
    Object.entries(query.filters).forEach(([key, value]) => {
      if (value.trim()) params.set(key, key === "start" || key === "end" ? new Date(value).toISOString() : value.trim());
    });
    setLoading(true); setError(""); setData(null);
    api<AuditPage>(`/admin/audit?${params}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setData(result); })
      .catch(reason => { if (!controller.signal.aborted) setError(errorText(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    // Aborting AND ignoring obsolete responses prevents older filter/page results
    // overwriting the latest query, even when a fetch has already resolved.
    return () => controller.abort();
  }, [query, revision]);

  function apply(event: FormEvent) {
    event.preventDefault();
    if (draft.start && draft.end && new Date(draft.start) > new Date(draft.end)) {
      setError("Start time must be before or equal to end time."); return;
    }
    setQuery(current => ({ filters: { ...draft }, offset: 0, retry: current.retry + 1 }));
  }
  return <section aria-labelledby="audit-heading" className="min-w-0 space-y-3">
    <h2 id="audit-heading" className="text-lg font-semibold">Audit log</h2>
    <Card className="min-w-0 p-5">
      <form onSubmit={apply} className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {([
          ["q", "Search audit"], ["action", "Action"], ["source", "Source"], ["actor_id", "Actor ID"],
          ["entity_type", "Entity type"], ["start", "Start time (local)"], ["end", "End time (local)"],
        ] as const).map(([key, label]) => <label key={key} className="block min-w-0 text-sm text-zinc-300">{label}
          <input type={key === "start" || key === "end" ? "datetime-local" : "text"} className={field} value={draft[key]}
            onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} />
        </label>)}
        <div className="flex flex-wrap items-end gap-2 sm:col-span-2 xl:col-span-4">
          <Button type="submit">Apply audit filters</Button>
          <Button type="button" variant="outline" onClick={() => { setDraft(emptyFilters); setQuery(current => ({ filters: emptyFilters, offset: 0, retry: current.retry + 1 })); }}>Clear audit filters</Button>
        </div>
      </form>
    </Card>
    {loading && <p role="status" className="text-sm text-zinc-300">Loading audit events…</p>}
    {error && <div className="space-y-2"><p role="alert" className="text-sm text-red-300">{error}</p>
      <Button type="button" variant="outline" onClick={() => setQuery(current => ({ ...current, retry: current.retry + 1 }))}>Retry audit log</Button>
    </div>}
    {!loading && !error && data && <>
      <p role="status" className="text-sm text-zinc-300">{data.total ? `Showing ${data.offset + 1}–${data.offset + data.items.length} of ${data.total} events` : "No audit events match these filters."}</p>
      <div className="space-y-3">
        {data.items.map(item => <article key={item.id} aria-label={`Audit event ${item.id}`} className="min-w-0 rounded-lg border border-line bg-panel px-4 py-3 text-sm">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3">
            <h3 className="font-medium">{item.action}</h3>
            <p className="text-xs text-zinc-400">{timestamp(item.created_at)}</p>
          </div>
          <p className="mt-1 text-zinc-300">Actor: {item.actor_name || item.actor_id || "—"} · Initiator: {item.initiator_name || item.initiator_id || "—"} · Source: {item.source} · Entity: {item.entity_type}</p>
          <details className="mt-1"><summary className="cursor-pointer text-xs text-sky-200 focus:outline-none focus:ring-2 focus:ring-accent">Event detail</summary>
            <p className="mt-2 text-xs text-zinc-400">Event: {item.id}<br />Actor ID: {item.actor_id || "—"}<br />Entity ID: {item.entity_id || "—"}</p>
            <pre className="mt-2 whitespace-pre-wrap break-words text-xs text-zinc-300">{typeof item.detail === "string" ? item.detail : JSON.stringify(item.detail, null, 2)}</pre>
          </details>
        </article>)}
      </div>
      <div className="flex flex-wrap gap-2" aria-label="Audit pagination">
        <Button type="button" variant="outline" aria-label="Previous audit page" disabled={data.offset === 0}
          onClick={() => setQuery(current => ({ ...current, offset: Math.max(0, data.offset - data.limit) }))}>Previous</Button>
        <Button type="button" variant="outline" aria-label="Next audit page" disabled={data.offset + data.items.length >= data.total}
          onClick={() => setQuery(current => ({ ...current, offset: data.offset + data.limit }))}>Next</Button>
      </div>
    </>}
  </section>;
}
