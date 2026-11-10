"use client";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Bot, Check, ChevronDown, Loader2, MessageCircle, Send, Settings2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Avatar } from "@/components/workspace/shared";
import type { Action, Agent, AgentProposal, HierarchyConfigT, AgentMsg } from "@/lib/types";

export function Agents({
  agents,
  config,
  save,
  isAdmin,
  inspect,
  proposals,
  approveProposal,
  rejectProposal,
}: {
  agents: Agent[];
  config: HierarchyConfigT | null;
  save: (v: Partial<HierarchyConfigT>) => Promise<void>;
  isAdmin: boolean;
  inspect: (a: Agent) => void;
  proposals: AgentProposal[];
  approveProposal: (proposal: AgentProposal) => Promise<void>;
  rejectProposal: (proposal: AgentProposal) => Promise<void>;
}) {
  const roots = agents.filter((a) => !a.parent_agent_id);
  const childrenOf = (id: string) => agents.filter((a) => a.parent_agent_id === id);
  const mapViewport = useRef<HTMLDivElement>(null);
  const working = agents.filter((a) => a.is_working).length;
  const blocked = agents.filter((a) => a.is_blocked).length;
  const limited = agents.filter((a) => a.is_rate_limited).length;
  const awaitingApproval = agents.filter((a) => a.is_awaiting_approval).length;
  useLayoutEffect(() => {
    const viewport = mapViewport.current;
    if (viewport) viewport.scrollLeft = Math.max(0, (viewport.scrollWidth - viewport.clientWidth) / 2);
  }, [agents.length, roots.length]);
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="page-kicker mb-2">Organization</p>
          <h1 className="page-title">Agent map</h1>
          <p className="page-description mt-2">See who reports to whom, what each agent is doing, and where work is blocked.</p>
        </div>
        <div className="flex flex-wrap gap-2" aria-label="Agent status summary">
          <span className="agent-summary"><Bot size={14} /> {agents.length} agents</span>
          <span className="agent-summary"><i className="agent-status-dot is-working" /> {working} working</span>
          <span className="agent-summary"><i className={cn("agent-status-dot", blocked ? "is-blocked" : "is-idle")} /> {blocked} blocked</span>
          {awaitingApproval > 0 && <span className="agent-summary"><i className="agent-status-dot is-review" /> {awaitingApproval} awaiting approval</span>}
          {limited > 0 && <span className="agent-summary"><i className="agent-status-dot is-limited" /> {limited} at daily limit</span>}
        </div>
      </header>
      {proposals.length > 0 && <section aria-labelledby="pending-agent-actions" className="space-y-3">
        <div>
          <h2 id="pending-agent-actions" className="text-sm font-semibold text-ink">Agent actions awaiting approval</h2>
          <p className="mt-1 text-xs text-slate-500">Tasks pause until a human approves or rejects the proposed action.</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {proposals.map((proposal) => <Card key={proposal.id} className="space-y-3 p-4">
            <div>
              <p className="text-sm font-semibold text-ink">{proposal.label}</p>
              <p className="mt-1 text-xs text-slate-500">{proposal.agent_name} · {proposal.task_title}</p>
            </div>
            {proposal.details?.length ? <dl className="space-y-1 text-xs text-slate-600">
              {proposal.details.map((detail, index) => <div key={index} className="flex gap-2"><dt className="font-medium">{detail.label}:</dt><dd className="break-all">{detail.value}</dd></div>)}
            </dl> : null}
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="outline" onClick={() => void rejectProposal(proposal)}><X size={14} /> Reject</Button>
              <Button size="sm" onClick={() => void approveProposal(proposal)}><Check size={14} /> Approve</Button>
            </div>
          </Card>)}
        </div>
      </section>}
      {isAdmin && <HierarchyConfigForm config={config} save={save} />}
      <section aria-labelledby="reporting-structure-title" className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-2 px-1">
          <div>
            <h2 id="reporting-structure-title" className="text-sm font-semibold text-ink">Reporting structure</h2>
            <p className="mt-1 text-xs text-slate-500">Select an agent to view its activity and open a persistent chat.</p>
          </div>
          {agents.length > 0 && <span className="text-xs font-medium text-slate-500">{roots.length} {roots.length === 1 ? "top-level agent" : "top-level agents"}</span>}
        </div>
        {roots.length ? (
          <Card className="agent-map-panel overflow-hidden">
            <div className="agent-map-legend">
              <span><i className="agent-status-dot is-working" /> Working</span>
              <span><i className="agent-status-dot is-idle" /> Available</span>
              <span><i className="agent-status-dot is-blocked" /> Blocked</span>
              <span><i className="agent-status-dot is-review" /> Awaiting approval</span>
              <span><i className="agent-status-dot is-limited" /> Daily limit</span>
            </div>
            <div ref={mapViewport} className="agent-map-scroll" role="region" aria-label="Scrollable agent reporting map" tabIndex={0}>
              <div className="agent-map">
                <ul className="agent-tree agent-tree-root">
                  {roots.map((r) => (
                    <AgentNode key={r.id} agent={r} childrenOf={childrenOf} inspect={inspect} root />
                  ))}
                </ul>
              </div>
            </div>
          </Card>
        ) : (
          <Card className="p-8 text-center text-sm text-zinc-500">
            {isAdmin
              ? "No agents yet — configure the hierarchy above to create your first agent."
              : "No agents yet. Ask an admin to configure the agent hierarchy."}
          </Card>
        )}
      </section>
    </div>
  );
}
export function HierarchyConfigForm({
  config,
  save,
}: {
  config: HierarchyConfigT | null;
  save: (v: Partial<HierarchyConfigT>) => Promise<void>;
}) {
  const [form, setForm] = useState<HierarchyConfigT>(
    config || { vp_count: 0, directors_per_vp: 0, managers_per_director: 0, workers_per_manager: 0 },
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    if (config) setForm(config);
  }, [config]);
  const small =
    "mt-1 w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm outline-none focus:border-accent";
  const fields: [keyof HierarchyConfigT, string][] = [
    ["vp_count", "VPs under CEO"],
    ["directors_per_vp", "Directors per VP"],
    ["managers_per_director", "Senior managers per Director"],
    ["workers_per_manager", "Workers per Senior Manager"],
  ];
  return (
    <Card className="mb-6 p-5">
      <div className="mb-4 flex items-center gap-2">
        <Settings2 size={16} className="text-accent" />
        <h2 className="text-sm font-semibold text-ink">Configure hierarchy</h2>
      </div>
      <form
        className="grid gap-3 sm:grid-cols-4"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            await save(form);
          } catch (err: any) {
            setError(err.message);
          } finally {
            setBusy(false);
          }
        }}
      >
        {fields.map(([key, label]) => (
          <label key={key} className="text-xs font-medium text-slate-600">
            {label}
            <input
              type="number"
              min={0}
              className={small}
              value={form[key]}
              onChange={(e) => setForm((f) => ({ ...f, [key]: Number(e.target.value) }))}
            />
          </label>
        ))}
        <div className="flex items-center gap-3 sm:col-span-4">
          {error && (
            <p role="alert" className="text-sm text-red-300">
              {error}
            </p>
          )}
          <Button type="submit" disabled={busy} className="ml-auto">
            {busy && <Loader2 className="animate-spin" size={15} />}Save hierarchy
          </Button>
        </div>
      </form>
    </Card>
  );
}
export function AgentNode({
  agent,
  childrenOf,
  inspect,
  root,
}: {
  agent: Agent;
  childrenOf: (id: string) => Agent[];
  inspect: (a: Agent) => void;
  root?: boolean;
}) {
  // Keep the first view focused on the CEO and direct reports. Larger branches
  // can be expanded as needed instead of rendering every worker at once.
  const [open, setOpen] = useState(() => agent.hierarchy_level === "ceo");
  const kids = childrenOf(agent.id);
  const status = agent.is_awaiting_approval ? "review" : agent.is_blocked ? "blocked" : agent.is_rate_limited ? "limited" : agent.is_working ? "working" : "idle";
  return (
    <li className={cn("agent-tree-item", root && "is-root")}>
      <div className="agent-tree-branch">
        <button
          onClick={() => inspect(agent)}
          className={cn("agent-node-card", agent.hierarchy_level === "ceo" && "agent-node-ceo")}
          aria-label={`Open ${agent.name}, ${agent.title}, ${status}`}
        >
          <span className={cn("agent-node-status", `is-${status}`)} aria-label={`Status: ${status}`} title={`Status: ${status}`} />
          <span className="agent-node-identity">
            <Avatar user={agent} />
            <span className="min-w-0 text-left">
              <span className="block truncate text-sm font-semibold text-ink">{agent.name}</span>
              <span className="mt-0.5 block truncate text-xs text-slate-500">{agent.title}</span>
            </span>
          </span>
          <span className="agent-node-task">
            <span className="agent-node-task-label">Current focus</span>
            <span className={cn("block truncate text-xs", agent.current_task ? "font-medium text-blue-700" : "text-slate-500")}>
              {agent.current_task ? agent.current_task.title : "Available for work"}
            </span>
          </span>
        </button>
        {kids.length > 0 && (
          <button
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} ${kids.length} reports for ${agent.name}`}
            onClick={() => setOpen(!open)}
            className="agent-report-toggle"
          >
            <ChevronDown size={13} className={cn("transition-transform", !open && "-rotate-90")} />
            {kids.length} {kids.length === 1 ? "report" : "reports"}
          </button>
        )}
      </div>
      {open && kids.length > 0 && (
        <ul className="agent-tree agent-tree-children">
          {kids.map((k) => (
            <AgentNode key={k.id} agent={k} childrenOf={childrenOf} inspect={inspect} />
          ))}
        </ul>
      )}
    </li>
  );
}
export function AgentInspector({
  agent,
  reports,
  messages,
  busy,
  error,
  close,
  send,
  confirm,
  connect,
}: {
  agent: Agent;
  reports: Agent[];
  messages: AgentMsg[];
  busy: boolean;
  error: string;
  close: () => void;
  send: (t: string) => void;
  confirm: (a: Action) => void;
  connect: () => void;
}) {
  const [text, setText] = useState("");
  return (
    <div className="fixed inset-0 z-30 bg-black/45" onMouseDown={close}>
      <aside
        className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-l border-line bg-white text-ink shadow-float"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-line p-5">
          <div className="flex items-center gap-3">
            <Avatar user={agent} />
            <div>
              <h2 className="font-semibold">{agent.name}</h2>
              <p className="text-xs text-zinc-500">{agent.title}</p>
            </div>
          </div>
          <button aria-label="Close agent inspector" className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 hover:text-ink" onClick={close}>
            <X className="text-zinc-500" />
          </button>
        </div>
        <div className="max-h-72 space-y-4 overflow-y-auto border-b border-line p-5">
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Current task</h3>
            <p className="mt-1 text-sm">
              {agent.current_task ? agent.current_task.title : "Idle -- no active task."}
            </p>
            {agent.is_blocked && (
              <p className="mt-1 text-xs text-red-700">
                Blocked -- review the task and use Unblock to resume it.
              </p>
            )}
            {agent.is_rate_limited && (
              <p className="mt-1 text-xs text-amber-700">
                Paused at the daily execution limit. Work resumes automatically when capacity renews.
              </p>
            )}
            {agent.is_working && (
              <p className="mt-1 text-xs text-blue-700">Working autonomously on this task.</p>
            )}
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Last completed task</h3>
            <p className="mt-1 text-sm">
              {agent.last_completed_task ? agent.last_completed_task.title : "None yet."}
            </p>
          </div>
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Direct reports ({reports.length})
            </h3>
            {reports.length ? (
              <ul className="mt-2 space-y-1.5">
                {reports.map((r) => (
                  <li key={r.id} className="flex items-center gap-2 text-sm text-slate-700">
                    <Avatar user={r} />
                    {r.name} -- {r.title}
                  </li>
                ))}
              </ul>
            ) : (
            <p className="mt-1 text-sm text-slate-500">No direct reports.</p>
            )}
          </div>
        </div>
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="flex items-center gap-2 border-b border-line px-5 py-3 text-xs text-slate-600">
            <MessageCircle size={14} />
            Persistent chat with {agent.name}
          </div>
          <div
            tabIndex={0}
            role="log"
            aria-label={"Conversation with " + agent.name}
            className="flex-1 space-y-4 overflow-y-auto px-5 pb-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
          >
            {messages.length === 0 && agent.hierarchy_level === "ceo" && (
              <div className="mx-auto mt-8 max-w-sm rounded-2xl border border-blue-100 bg-blue-50/70 p-5 text-center">
                <div className="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-white text-blue-700 shadow-sm"><Bot size={19} /></div>
                <h3 className="mt-3 text-sm font-semibold text-ink">What should we connect first?</h3>
                <p className="mt-1 text-xs leading-5 text-slate-600">Connect a work tool or tell me to start a project. I’ll ask if I need a key detail.</p>
                <div className="mt-4 flex flex-wrap justify-center gap-2">
                  {["Gmail", "Slack", "Notion"].map((name) => <Button key={name} variant="outline" size="sm" onClick={connect}>{name}</Button>)}
                </div>
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id} className={cn("flex gap-3", m.role === "user" && "justify-end")}>
                {m.role === "agent" && (
                  <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-blue-50 text-blue-700">
                    <Bot size={14} />
                  </div>
                )}
                <div
                  className={cn(
                    "max-w-[82%] rounded-lg px-3 py-2 text-sm",
                    m.role === "agent" ? "border border-line bg-slate-50 text-ink" : "bg-blue-600 text-white",
                  )}
                >
                  <p>{m.body}</p>
                  {m.actions?.map((a) => (
                    <div
                      key={a.label}
                      className="mt-3 rounded-lg border border-blue-100 bg-blue-50 p-3 text-ink"
                    >
                      <p className="font-semibold">{a.label}</p>
                      {!!a.details?.length && (
                        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                          {a.details.map((detail) => (
                            <div key={detail.label + detail.value} className="contents">
                              <dt className="text-slate-500">{detail.label}</dt>
                              <dd className="break-words">{detail.value}</dd>
                            </div>
                          ))}
                        </dl>
                      )}
                      {a.preview && (
                        <details className="mt-3">
                          <summary className="cursor-pointer text-xs font-semibold">Review patch preview</summary>
                          <pre className="mt-2 max-h-72 overflow-auto rounded-md bg-slate-950 p-3 text-xs leading-5 text-slate-100">{a.preview}</pre>
                        </details>
                      )}
                      <Button size="sm" className="mt-3" onClick={() => confirm(a)}>
                        <Check size={14} />
                        Confirm and run
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {busy && <Loader2 className="animate-spin text-accent" size={16} />}
            {error && (
              <p role="alert" className="text-sm text-red-700">
                {error}
              </p>
            )}
          </div>
          <form
            className="flex gap-2 border-t border-line bg-white p-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (text.trim()) {
                send(text);
                setText("");
              }
            }}
          >
            <input
              className="min-w-0 flex-1 rounded-lg border border-line bg-canvas p-2.5 text-sm outline-none focus:border-accent"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"Message " + agent.name}
            />
            <Button aria-label={"Send message to " + agent.name} size="sm" disabled={busy}>
              <Send size={14} />
            </Button>
          </form>
        </div>
      </aside>
    </div>
  );
}
