"use client";

import { useEffect, useState, type FormEvent } from "react";
import { API_BASE, api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type Provider = {
  id: string; name: string; protocol: string; active: boolean; sso_only: boolean;
  oidc_issuer: string | null; client_id: string | null; has_client_secret: boolean; has_scim_token: boolean;
  role_mapping_json: Record<string, string>; redirect_uri: string;
  domain_binding: string | null; domain_verified: boolean;
  domain_verification_record: { name: string; type?: string; value: string } | null;
};
type Draft = {
  name: string; oidc_issuer: string; client_id: string; client_secret: string;
  domain_binding: string; sso_only: boolean; roles: string;
};

const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";
const errorText = (e: unknown) => e instanceof Error ? e.message : "Request failed. Please try again.";
const emptyDraft: Draft = { name: "", oidc_issuer: "", client_id: "", client_secret: "", domain_binding: "", sso_only: false, roles: "" };
// The SCIM endpoints live beside /api on the same origin, not under it.
const scimBaseUrl = `${new URL(API_BASE).origin}/scim/v2`;

/** "IdP group = role" per line <-> {"IdP group": "role"}. Unknown roles are kept as
 *  typed; the API maps anything that isn't admin/manager/member to member. */
function rolesToText(mapping: Record<string, string>) {
  return Object.entries(mapping).map(([claim, role]) => `${claim} = ${role}`).join("\n");
}
function textToRoles(text: string): Record<string, string> {
  const mapping: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.lastIndexOf("=");
    if (at <= 0) continue;
    const claim = line.slice(0, at).trim(), role = line.slice(at + 1).trim().toLowerCase();
    if (claim && role) mapping[claim] = role;
  }
  return mapping;
}
function draftFrom(p: Provider): Draft {
  return { name: p.name, oidc_issuer: p.oidc_issuer ?? "", client_id: p.client_id ?? "", client_secret: "",
           domain_binding: p.domain_binding ?? "", sso_only: p.sso_only, roles: rolesToText(p.role_mapping_json) };
}

function Copyable({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return <div className="min-w-0">
    <p className="text-xs text-zinc-400">{label}</p>
    <div className="mt-1 flex min-w-0 items-center gap-2">
      <code className="min-w-0 flex-1 break-all rounded-md border border-line bg-canvas px-2 py-1.5 text-xs">{value}</code>
      <Button type="button" size="sm" variant="outline" aria-label={`Copy ${label}`} onClick={() => {
        navigator.clipboard?.writeText(value).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => {});
      }}>{copied ? "Copied" : "Copy"}</Button>
    </div>
  </div>;
}

function ProviderForm({ initial, existing, busy, submit, cancel }: {
  initial: Draft; existing?: Provider; busy: boolean; submit: (draft: Draft) => void; cancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft(d => ({ ...d, [key]: value }));
  const domainChanged = existing && (existing.domain_binding ?? "") !== draft.domain_binding.trim().toLowerCase();
  const canEnforce = Boolean(existing?.domain_verified) && !domainChanged;
  return <form className="grid min-w-0 gap-3 sm:grid-cols-2" onSubmit={(e: FormEvent) => { e.preventDefault(); submit(draft); }}>
    <label className="text-sm text-zinc-300">Name
      <input required className={field} value={draft.name} onChange={e => set("name", e.target.value)} placeholder="Okta" />
    </label>
    <label className="text-sm text-zinc-300">Email domain
      <input className={field} value={draft.domain_binding} onChange={e => set("domain_binding", e.target.value)} placeholder="yourcompany.com" />
    </label>
    <label className="text-sm text-zinc-300 sm:col-span-2">Issuer URL
      <input required type="url" className={field} value={draft.oidc_issuer} onChange={e => set("oidc_issuer", e.target.value)} placeholder="https://yourcompany.okta.com/oauth2/default" />
    </label>
    <label className="text-sm text-zinc-300">Client ID
      <input required className={field} value={draft.client_id} onChange={e => set("client_id", e.target.value)} />
    </label>
    <label className="text-sm text-zinc-300">Client secret
      <input type="password" autoComplete="new-password" className={field} value={draft.client_secret}
        required={!existing?.has_client_secret} onChange={e => set("client_secret", e.target.value)}
        placeholder={existing?.has_client_secret ? "Stored — leave blank to keep" : ""} />
    </label>
    <label className="text-sm text-zinc-300 sm:col-span-2">Role mapping <span className="text-zinc-400">(one per line: IdP group or role = admin, manager or member; anything unmapped is member)</span>
      <textarea rows={3} className={field} value={draft.roles} onChange={e => set("roles", e.target.value)} placeholder={"Enter AI Admins = admin\nProject Leads = manager"} />
    </label>
    <label className="flex items-start gap-2 text-sm text-zinc-300 sm:col-span-2">
      <input type="checkbox" className="mt-1" checked={draft.sso_only && canEnforce} disabled={!canEnforce}
        onChange={e => set("sso_only", e.target.checked)} />
      <span>Require SSO for this domain{!canEnforce && <span className="block text-xs text-zinc-400">Available once the domain is verified.</span>}</span>
    </label>
    <div className="flex flex-wrap gap-2 sm:col-span-2">
      <Button type="submit" disabled={busy}>{busy ? "Saving…" : existing ? "Save provider" : "Add provider"}</Button>
      <Button type="button" variant="ghost" onClick={cancel}>Cancel</Button>
    </div>
  </form>;
}

/** Single sign-on: connect an OIDC identity provider, prove the email domain with a
 *  DNS TXT record (until then the provider signs no one in), and issue the SCIM
 *  token for automatic provisioning. Everything the API supports, nothing it doesn't:
 *  OIDC only (SAML has no sign-in endpoint). */
export function SsoSection() {
  const [providers, setProviders] = useState<Provider[]>([]),
    [editing, setEditing] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [checking, setChecking] = useState<string | null>(null),
    [scimToken, setScimToken] = useState<{ id: string; token: string } | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");

  async function load(signal?: AbortSignal) {
    setProviders(await api<Provider[]>("/admin/sso/providers", { signal }));
  }
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).catch(e => { if (!controller.signal.aborted) setError(errorText(e)); });
    return () => controller.abort();
  }, []);

  async function run(action: () => Promise<void>, message: string) {
    setBusy(true); setError(""); setNotice("");
    try { await action(); setNotice(message); await load(); }
    catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  }

  function save(draft: Draft, existing?: Provider) {
    const body: Record<string, unknown> = {
      name: draft.name.trim(), protocol: "oidc", oidc_issuer: draft.oidc_issuer.trim(), client_id: draft.client_id.trim(),
      domain_binding: draft.domain_binding.trim() || null, sso_only: draft.sso_only, role_mapping_json: textToRoles(draft.roles),
    };
    if (draft.client_secret) body.client_secret = draft.client_secret;
    run(async () => {
      await api(existing ? `/admin/sso/providers/${existing.id}` : "/admin/sso/providers",
        { method: existing ? "PATCH" : "POST", body: JSON.stringify(body) });
      setEditing(null);
    }, existing ? `Saved ${body.name}.` : `Added ${body.name}. Publish the DNS record below, then check it.`);
  }

  async function verify(provider: Provider) {
    setChecking(provider.id); setError(""); setNotice("");
    try {
      await api(`/admin/sso/providers/${provider.id}/verify-domain`, { method: "POST" });
      setNotice(`${provider.domain_binding} is verified. People at that domain can now sign in with ${provider.name}.`);
      await load();
    } catch (e) { setError(errorText(e)); }
    finally { setChecking(null); }
  }

  function rotateScim(provider: Provider) {
    if (provider.has_scim_token && !window.confirm("Generate a new SCIM token? The current one stops working immediately.")) return;
    run(async () => {
      const result = await api<{ scim_token: string }>(`/admin/sso/providers/${provider.id}/scim-token`, { method: "POST" });
      setScimToken({ id: provider.id, token: result.scim_token });
    }, "New SCIM token generated. Copy it now — it is shown only once.");
  }

  return <section aria-labelledby="sso-heading" className="min-w-0 space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 id="sso-heading" className="text-lg font-semibold">Single sign-on</h2>
      {editing !== "new" && <Button type="button" size="sm" variant="outline" onClick={() => { setEditing("new"); setError(""); setNotice(""); }}>Add identity provider</Button>}
    </div>
    <p className="text-sm text-zinc-400">Connect Okta, Microsoft Entra ID, Google Workspace or any OpenID Connect provider. A provider signs people in only after you prove you own its email domain.</p>
    {notice && <p role="status" className="text-sm text-emerald-300">{notice}</p>}
    {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
    {editing === "new" && <Card className="min-w-0 space-y-3 p-5">
      <h3 className="text-sm font-medium">New identity provider</h3>
      <ProviderForm initial={emptyDraft} busy={busy} submit={draft => save(draft)} cancel={() => setEditing(null)} />
    </Card>}
    {providers.length === 0 && editing !== "new" && <Card className="p-5 text-sm text-zinc-400">No identity provider yet. Members sign in with email and password.</Card>}
    {providers.map(provider => <Card key={provider.id} className="min-w-0 space-y-4 p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-medium">{provider.name}</h3>
          <p className="break-all text-xs text-zinc-400">{provider.oidc_issuer || "No issuer configured"}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {provider.domain_binding
            ? <Badge tone={provider.domain_verified ? "green" : "amber"}>{provider.domain_binding} · {provider.domain_verified ? "verified" : "unverified"}</Badge>
            : <Badge>no domain</Badge>}
          {provider.sso_only && <Badge tone="blue">SSO required</Badge>}
          {!provider.active && <Badge tone="red">inactive</Badge>}
        </div>
      </div>

      {editing === provider.id
        ? <ProviderForm initial={draftFrom(provider)} existing={provider} busy={busy} submit={draft => save(draft, provider)} cancel={() => setEditing(null)} />
        : <>
          <Copyable label="Redirect URI to register with your identity provider" value={provider.redirect_uri} />
          {provider.domain_binding && !provider.domain_verified && provider.domain_verification_record && <div className="space-y-3 rounded-lg border border-amber-400/30 bg-amber-500/5 p-4">
            <p className="text-sm text-amber-200">Prove you own <strong>{provider.domain_binding}</strong>: add this TXT record at your DNS provider, wait for it to publish (usually a few minutes), then check.</p>
            <Copyable label="TXT record name" value={provider.domain_verification_record.name} />
            <Copyable label="TXT record value" value={provider.domain_verification_record.value} />
            <Button type="button" size="sm" disabled={checking === provider.id} onClick={() => verify(provider)}>
              {checking === provider.id ? "Checking DNS…" : "Check DNS record"}
            </Button>
          </div>}
          <div className="space-y-2">
            <p className="text-sm text-zinc-300">Automatic provisioning (SCIM) {provider.has_scim_token ? <Badge tone="green">token issued</Badge> : <Badge>not set up</Badge>}</p>
            <Copyable label="SCIM base URL" value={scimBaseUrl} />
            {scimToken?.id === provider.id && <div className="rounded-lg border border-emerald-400/30 p-3">
              <Copyable label="SCIM bearer token — shown once; paste it into your identity provider now" value={scimToken.token} />
            </div>}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" onClick={() => { setEditing(provider.id); setScimToken(null); setError(""); setNotice(""); }}>Edit</Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => rotateScim(provider)}>{provider.has_scim_token ? "Rotate SCIM token" : "Generate SCIM token"}</Button>
          </div>
        </>}
    </Card>)}
  </section>;
}
