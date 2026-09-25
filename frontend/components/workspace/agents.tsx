"use client";
import { useEffect, useState } from "react";
import { Bot, Check, ChevronDown, Loader2, MessageCircle, Send, Settings2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Avatar } from "@/components/workspace/shared";
import type { Action, Agent, HierarchyConfigT, AgentMsg } from "@/lib/types";

export function Agents({
  agents,
  config,
  save,
  isAdmin,
  inspect,
}: {
  agents: Agent[];
  config: HierarchyConfigT | null;
  save: (v: Partial<HierarchyConfigT>) => Promise<void>;
  isAdmin: boolean;
  inspect: (a: Agent) => void;
}) {
  const roots = agents.filter((a) => !a.parent_agent_id);
  const childrenOf = (id: string) => agents.filter((a) => a.parent_agent_id === id);
  return (
    <>
      <div className="mb-6">
        <p className="text-sm text-sky-300">Org chart</p>
        <h1 className="mt-1 text-2xl font-semibold">Agent hierarchy</h1>
        <p className="mt-1 text-sm text-zinc-400">
          {agents.length} agent{agents.length === 1 ? "" : "s"} reporting up to the CEO.
        </p>
      </div>
      {isAdmin && <HierarchyConfigForm config={config} save={save} />}
      {roots.length ? (
        <Card className="overflow-x-auto p-6">
          <div className="flex min-w-max justify-center gap-10">
            {roots.map((r) => (
              <AgentNode key={r.id} agent={r} childrenOf={childrenOf} inspect={inspect} />
            ))}
          </div>
        </Card>
      ) : (
        <Card className="p-8 text-center text-sm text-zinc-500">
          {isAdmin
            ? "No agents yet -- configure the hierarchy above to create your first agent."
            : "No agents yet. Ask an admin to configure the agent hierarchy."}
        </Card>
      )}
    </>
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
        <Settings2 size={16} className="text-sky-300" />
        <h2 className="text-sm font-semibold">Configure hierarchy</h2>
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
          <label key={key} className="text-xs text-zinc-400">
            {label}
            <input
              type="number"
              min={0}
              max={20}
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
}: {
  agent: Agent;
  childrenOf: (id: string) => Agent[];
  inspect: (a: Agent) => void;
}) {
  const [open, setOpen] = useState(true);
  const kids = childrenOf(agent.id);
  const status = agent.is_blocked ? "blocked" : agent.is_working ? "working" : "idle";
  return (
    <div className="flex flex-col items-center">
      <button
        onClick={() => inspect(agent)}
        className="relative w-40 shrink-0 rounded-lg border border-line bg-panel p-3 text-center hover:border-sky-400/50"
      >
        <i
          aria-label={"Status: " + status}
          title={"Status: " + status}
          className={cn(
            "absolute right-2 top-2 h-2 w-2 rounded-full",
            status === "blocked" && "bg-red-400",
            status === "working" && "bg-sky-400 animate-pulse",
            status === "idle" && "bg-zinc-700",
          )}
        />
        <Avatar user={agent} />
        <p className="mt-2 truncate text-sm font-medium">{agent.name}</p>
        <p className="truncate text-xs text-zinc-500">{agent.title}</p>
        <p className={cn("mt-2 truncate text-[10px]", agent.current_task ? "text-sky-300" : "text-zinc-600")}>
          {agent.current_task ? agent.current_task.title : "Idle"}
        </p>
      </button>
      {kids.length > 0 && (
        <>
          <button
            aria-label={(open ? "Collapse " : "Expand ") + agent.name + "'s reports"}
            onClick={() => setOpen(!open)}
            className="my-2 text-zinc-500 hover:text-sky-300"
          >
            <ChevronDown size={14} className={cn("transition-transform", !open && "-rotate-90")} />
          </button>
          {open && (
            <div className="flex gap-6 border-t border-line/40 pt-4">
              {kids.map((k) => (
                <AgentNode key={k.id} agent={k} childrenOf={childrenOf} inspect={inspect} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
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
}: {
  agent: Agent;
  reports: Agent[];
  messages: AgentMsg[];
  busy: boolean;
  error: string;
  close: () => void;
  send: (t: string) => void;
  confirm: (a: Action) => void;
}) {
  const [text, setText] = useState("");
  return (
    <div className="fixed inset-0 z-30 bg-black/45" onMouseDown={close}>
      <aside
        className="absolute inset-y-0 right-0 flex w-full max-w-xl flex-col border-l border-line bg-[#0e0e12]"
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
          <button aria-label="Close agent inspector" onClick={close}>
            <X className="text-zinc-500" />
          </button>
        </div>
        <div className="max-h-72 space-y-4 overflow-y-auto border-b border-line p-5">
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">Current task</h3>
            <p className="mt-1 text-sm">
              {agent.current_task ? agent.current_task.title : "Idle -- no active task."}
            </p>
            {agent.is_blocked && (
              <p className="mt-1 text-xs text-red-300">
                Blocked -- repeated attempts failed. Send a message or reassign the task to reset it.
              </p>
            )}
            {agent.is_working && (
              <p className="mt-1 text-xs text-sky-300">Working autonomously on this task.</p>
            )}
          </div>
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">Last completed task</h3>
            <p className="mt-1 text-sm">
              {agent.last_completed_task ? agent.last_completed_task.title : "None yet."}
            </p>
          </div>
          <div>
            <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Direct reports ({reports.length})
            </h3>
            {reports.length ? (
              <ul className="mt-2 space-y-1.5">
                {reports.map((r) => (
                  <li key={r.id} className="flex items-center gap-2 text-sm text-zinc-300">
                    <Avatar user={r} />
                    {r.name} -- {r.title}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-sm text-zinc-500">No direct reports.</p>
            )}
          </div>
        </div>
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="flex items-center gap-2 px-5 py-3 text-xs text-zinc-400">
            <MessageCircle size={14} />
            Persistent chat with {agent.name}
          </div>
          <div
            tabIndex={0}
            role="log"
            aria-label={"Conversation with " + agent.name}
            className="flex-1 space-y-4 overflow-y-auto px-5 pb-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/60"
          >
            {messages.map((m) => (
              <div key={m.id} className={cn("flex gap-3", m.role === "user" && "justify-end")}>
                {m.role === "agent" && (
                  <div className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-sky-400/10 text-sky-300">
                    <Bot size={14} />
                  </div>
                )}
                <div
                  className={cn(
                    "max-w-[82%] rounded-lg px-3 py-2 text-sm",
                    m.role === "agent" ? "bg-white/[.045]" : "bg-sky-400 text-slate-950",
                  )}
                >
                  <p>{m.body}</p>
                  {m.actions?.map((a) => (
                    <div
                      key={a.label}
                      className="mt-3 rounded-lg border border-sky-400/30 bg-black/20 p-3 text-zinc-100"
                    >
                      <p>{a.label}</p>
                      <Button size="sm" className="mt-3" onClick={() => confirm(a)}>
                        <Check size={14} />
                        Confirm and run
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {busy && <Loader2 className="animate-spin text-sky-300" size={16} />}
            {error && (
              <p role="alert" className="text-sm text-red-300">
                {error}
              </p>
            )}
          </div>
          <form
            className="flex gap-2 border-t border-line p-4"
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
