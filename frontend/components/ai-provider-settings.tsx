"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type ProviderSettings = {
  provider: string;
  configured_provider: string;
  model: string;
  api_key_configured: boolean;
  requests_per_minute: number;
  maximum_requests_per_minute: number;
};

const field = "mt-1 block w-full min-w-0 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent";

export function AIProviderSettings() {
  const [settings, setSettings] = useState<ProviderSettings | null>(null);
  const [provider, setProvider] = useState<"ifm" | "openrouter">("ifm");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("IFM/K2-Horizon-375B-A23B");
  const [requestsPerMinute, setRequestsPerMinute] = useState(20);
  const [clearKey, setClearKey] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let active = true;
    api<ProviderSettings>("/admin/ai-provider")
      .then(value => {
        if (!active) return;
        setSettings(value);
        const selected = value.configured_provider === "openrouter" ? "openrouter" : "ifm";
        setProvider(selected);
        setModel(value.model || "IFM/K2-Horizon-375B-A23B");
        setRequestsPerMinute(value.requests_per_minute);
      })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : "Could not load AI settings."); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    const body: Record<string, unknown> = { provider, requests_per_minute: requestsPerMinute };
    if (provider === "openrouter") body.model = model.trim();
    if (apiKey.trim()) body.api_key = apiKey.trim();
    if (clearKey) body.clear_api_key = true;
    try {
      const updated = await api<ProviderSettings>("/admin/ai-provider", { method: "PATCH", body: JSON.stringify(body) });
      setSettings(updated);
      setProvider(updated.configured_provider === "openrouter" ? "openrouter" : "ifm");
      setModel(updated.model || "IFM/K2-Horizon-375B-A23B");
      setRequestsPerMinute(updated.requests_per_minute);
      setApiKey(""); setClearKey(false);
      setNotice(updated.api_key_configured
        ? `${updated.configured_provider === "ifm" ? "IFM K2 Horizon" : "OpenRouter"} settings saved. New AI requests will use ${updated.model}.`
        : "Settings saved. Add a provider API key to enable hosted AI.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save AI settings.");
    } finally { setBusy(false); }
  }

  return <section aria-labelledby="ai-provider-heading" className="space-y-3">
    <div>
      <h2 id="ai-provider-heading" className="text-lg font-semibold text-ink">AI provider</h2>
      <p className="mt-1 text-sm text-slate-500">Connect IFM K2 Horizon to use tool-using Copilot and CEO agent workflows.</p>
    </div>
    <Card className="p-4 sm:p-5">
      {loading ? <p role="status" className="text-sm text-slate-500">Loading AI settings…</p> : <form onSubmit={save} className="space-y-4">
        <div className="rounded-lg border border-line bg-canvas p-3 text-sm text-ink">
          {settings?.api_key_configured
            ? <span>{settings.configured_provider === "ifm" ? "IFM K2 Horizon" : "OpenRouter"} is configured. The saved key is encrypted and cannot be viewed here.</span>
            : <span>Current provider: {settings?.provider || "deterministic"}. Add an IFM API key to use K2 Horizon.</span>}
        </div>
        <label className="block text-sm text-ink">
          Provider
          <select className={field} value={provider} onChange={event => { setProvider(event.target.value as "ifm" | "openrouter"); setApiKey(""); setClearKey(false); }}>
            <option value="ifm">IFM · K2 Horizon</option>
            <option value="openrouter">OpenRouter</option>
          </select>
        </label>
        <label className="block text-sm text-ink">
          {provider === "ifm" ? "IFM API key" : "OpenRouter API key"}
          <input
            type="password" autoComplete="new-password" spellCheck={false} className={field}
            value={apiKey} onChange={event => { setApiKey(event.target.value); if (event.target.value) setClearKey(false); }}
            placeholder={settings?.api_key_configured && settings.configured_provider === provider ? "Saved key stays hidden; enter a new key to replace it" : `Paste your ${provider === "ifm" ? "IFM" : "OpenRouter"} API key`}
            aria-label={provider === "ifm" ? "IFM API key" : "OpenRouter API key"}
          />
          <span className="mt-1 block text-xs text-slate-500">Stored encrypted on the server. Enter a new key when changing providers.</span>
        </label>
        {provider === "ifm" ? <div className="text-sm text-ink">Model<div className={`${field} bg-slate-50`}>IFM/K2-Horizon-375B-A23B</div><span className="mt-1 block text-xs text-slate-500">Fixed model and API endpoint: api.ifm.ai/v1.</span></div> : <label className="block text-sm text-ink">
          OpenRouter model ID
          <input type="text" required maxLength={200} className={field} value={model} onChange={event => setModel(event.target.value)} placeholder="openai/gpt-4o-mini" />
        </label>}
        <label className="block max-w-xs text-sm text-ink">
          Requests per minute
          <input
            type="number" required min={1} max={settings?.maximum_requests_per_minute ?? 120}
            className={field} value={requestsPerMinute}
            onChange={event => setRequestsPerMinute(Number(event.target.value))}
          />
          <span className="mt-1 block text-xs text-slate-500">Limits outbound AI calls for this workspace, including agent tool turns. Default: 20 per minute.</span>
        </label>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {notice && <p role="status" className="text-sm text-emerald-700">{notice}</p>}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" disabled={busy}>{busy ? "Saving…" : "Save AI settings"}</Button>
          {settings?.api_key_configured && <Button type="button" variant="outline" disabled={busy} onClick={() => { setClearKey(true); setApiKey(""); }}>
            {clearKey ? "Key will be cleared on save" : "Clear saved key"}
          </Button>}
        </div>
      </form>}
    </Card>
  </section>;
}
