"use client";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Bot, Check, Circle, FolderKanban, Loader2, Search, Send, Sparkles, X } from "lucide-react";
import { api, API_BASE } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Project, Task, Action } from "@/lib/types";
import { routeToUrl } from "@/lib/routes";

type SearchKind = "projects" | "tasks" | "documents" | "attachments" | "discussions" | "decisions";
type SearchItem = {
  id: string;
  title?: string;
  name?: string;
  file_name?: string;
  snippet?: string;
  created_at?: string | null;
  project_id?: string | null;
  task_id?: string | null;
  source_ref?: string | null;
  [key: string]: unknown;
};
type SearchResponse = Partial<Record<SearchKind, SearchItem[]>>;
type AgentSource = {
  id: string;
  type: string;
  title: string;
  created_at?: string | null;
  source_ref?: string | null;
  project_id?: string | null;
  task_id?: string | null;
};

const SEARCH_GROUPS: { key: SearchKind; label: string }[] = [
  { key: "projects", label: "Projects" },
  { key: "tasks", label: "Tasks" },
  { key: "documents", label: "Files" },
  { key: "attachments", label: "Attachments" },
  { key: "discussions", label: "Discussions" },
  { key: "decisions", label: "Decisions" },
];

function displayDate(value?: string | null) {
  if (!value) return "Date unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Date unavailable" : date.toLocaleString();
}

function safeSourceHref(source: { source_ref?: string | null; project_id?: string | null; task_id?: string | null }) {
  if (source.source_ref?.startsWith("/api/") && !source.source_ref.startsWith("//")) {
    return `${API_BASE}${source.source_ref.slice(4)}`;
  }
  if (source.source_ref?.startsWith("/") && !source.source_ref.startsWith("//")) return source.source_ref;
  if (source.task_id && source.project_id) return routeToUrl({ view: "Project", projectId: source.project_id, taskId: source.task_id });
  if (source.project_id) return routeToUrl({ view: "Project", projectId: source.project_id });
  return undefined;
}

function SourceList({ sources }: { sources?: AgentSource[] }) {
  if (!sources?.length) return null;
  return (
    <section className="mt-4 border-t border-line pt-3" aria-label="Sources used for this answer">
      <h3 className="text-xs font-semibold text-slate-600">Sources</h3>
      <ul className="mt-2 space-y-1.5">
        {sources.map((source, index) => {
          const href = safeSourceHref(source);
          return (
            <li key={`${source.type}:${source.id}:${index}`} className="text-xs leading-5 text-slate-600">
              {href ? <a className="font-medium text-blue-700 underline-offset-2 hover:underline" href={href}>{source.title}</a> : <span className="font-medium text-ink">{source.title}</span>}
              <span className="ml-1 text-slate-500">· {source.type} · <time dateTime={source.created_at || undefined}>{displayDate(source.created_at)}</time></span>
              {source.source_ref && <span className="ml-1 break-all text-slate-500">· {source.source_ref}</span>}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export const AI_GREETING = {
  role: "assistant",
  body: "I can read your workspace and propose changes. Every write needs your confirmation.",
};

/** `messages` lives in the page, so the conversation survives switching views. Each
 *  proposed action records its outcome, so it can be run or rejected exactly once. */
export function AI({ project, clear, onAction, messages, setMessages }: any) {
  const [text, setText] = useState("What should I work on today?"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [outcomes, setOutcomes] = useState<Record<string, "running" | "done" | "rejected">>({});
  async function ask(question = text) {
    if (!question.trim() || busy) return;
    setText("");
    setError("");
    setMessages((x: any[]) => [...x, { role: "user", body: question }]);
    setBusy(true);
    try {
      const r = await api<any>("/ai/plan", {
        method: "POST",
        body: JSON.stringify({ message: question, project_id: project?.id || null }),
      });
      setMessages((x: any[]) => [...x, { role: "assistant", body: r.reply, actions: r.actions, sources: r.sources || [] }]);
    } catch (e: any) {
      setError(e.message || "Enter AI could not answer right now.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-4">
        <div className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl bg-blue-600 text-white shadow-[0_8px_18px_rgba(49,95,206,.22)]">
          <Sparkles size={22} />
        </div>
        <div>
          <p className="page-kicker mb-1">Workspace intelligence</p>
          <h1 className="page-title">Enter AI Copilot</h1>
          <p className="page-description mt-1">Get answers from your workspace and review actions before they run.</p>
        </div>
        </div>
        <span className="hidden items-center gap-2 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-semibold text-emerald-800 sm:inline-flex"><span className="h-2 w-2 rounded-full bg-emerald-500" /> Ready to help</span>
      </div>
      <Card className="min-h-[560px] overflow-hidden">
        {project ? (
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-blue-100 bg-blue-50/80 px-5 py-3 text-xs text-blue-900">
            <span>
              Scoped to <b>{project.name}</b> -- only this project's data is used.
            </span>
            <button className="font-semibold text-blue-800 underline-offset-2 hover:underline" onClick={clear}>
              Ask about everything instead
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2 border-b border-line bg-slate-50/80 px-5 py-3 text-xs text-zinc-400">
            <span className="grid h-6 w-6 shrink-0 place-items-center rounded-md bg-blue-50 text-blue-700"><Circle size={13} /></span>
            Answers are grounded in projects, tasks, and priority summaries. Proposed changes always need your approval.
          </div>
        )}
        <div className="flex flex-wrap gap-2 border-b border-line/60 px-5 py-3">
          {[
            "What should I work on today?",
            "What is at risk?",
            project ? `Summarize ${project.name}` : "Summarize my projects",
          ].map((prompt) => (
            <button
              key={prompt}
              className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-blue-200 hover:bg-blue-50 hover:text-blue-800 disabled:opacity-50"
              onClick={() => ask(prompt)}
              disabled={busy}
            >
              {prompt}
            </button>
          ))}
        </div>
        <div className="min-h-[300px] space-y-5 bg-slate-50/50 p-5 sm:p-6">
          {messages.map((m: any, i: number) => (
            <div className={cn("flex gap-3", m.role === "user" && "justify-end")} key={i}>
              {m.role === "assistant" && (
                <div className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-blue-100 text-blue-800">
                  <Bot size={15} />
                </div>
              )}
              <div
                className={cn(
                  "max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-6 shadow-sm sm:max-w-[82%]",
                  m.role === "assistant" ? "border border-line bg-white text-ink" : "bg-blue-600 text-white",
                )}
              >
                <p>{m.body}</p>
                <SourceList sources={m.sources} />
                {m.actions?.map((a: Action) => {
                  const outcome = outcomes[a.confirmation_token];
                  return (
                    <div
                      key={a.confirmation_token}
                      className="mt-3 rounded-xl border border-blue-100 bg-blue-50/80 p-3 text-ink"
                    >
                      <p className="font-medium">{a.label}</p>
                      {!!a.details?.length && (
                        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                          {a.details.map((d) => (
                            <div key={d.label + d.value} className="contents">
                              <dt className="text-zinc-400">{d.label}</dt>
                              <dd className="break-words">{d.value}</dd>
                            </div>
                          ))}
                        </dl>
                      )}
                      {outcome === "done" && <p className="mt-3 text-xs font-semibold text-emerald-700">Done.</p>}
                      {outcome === "rejected" && (
                        <p className="mt-3 text-xs text-zinc-400">Rejected. Nothing was changed.</p>
                      )}
                      {(!outcome || outcome === "running") && (
                        <div className="mt-3 flex flex-wrap gap-2">
                          <Button
                            size="sm"
                            disabled={outcome === "running"}
                            onClick={async () => {
                              setOutcomes((o) => ({ ...o, [a.confirmation_token]: "running" }));
                              try {
                                await onAction(a);
                                setOutcomes((o) => ({ ...o, [a.confirmation_token]: "done" }));
                              } catch (e: any) {
                                setOutcomes((o) => {
                                  const rest = { ...o };
                                  delete rest[a.confirmation_token];
                                  return rest;
                                });
                                setError(
                                  e.message || "Confirmation failed. Please ask for a fresh proposal.",
                                );
                              }
                            }}
                          >
                            <Check size={14} />
                            {outcome === "running" ? "Running…" : "Confirm and run"}
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={outcome === "running"}
                            onClick={() => setOutcomes((o) => ({ ...o, [a.confirmation_token]: "rejected" }))}
                          >
                            <X size={14} />
                            Reject
                          </Button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
          {busy && <div role="status" className="flex items-center gap-2 text-xs text-slate-500"><Loader2 className="animate-spin text-accent" size={16} /> Thinking…</div>}
          {error && (
            <p role="alert" className="text-sm text-red-700">
              {error}
            </p>
          )}
        </div>
        <div className="border-t border-line p-4">
          <div className="flex gap-2 rounded-lg border border-line bg-canvas p-2 focus-within:border-accent">
            <Sparkles className="m-1 text-accent" size={16} />
            <input
              className="min-w-0 flex-1 bg-transparent text-sm outline-none"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && ask()}
              placeholder="Ask about work or propose a task"
            />
            <Button aria-label="Send message" size="sm" onClick={() => ask()} disabled={busy}>
              <Send size={15} />
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
export function Command({ projects, close, open, openTask, ai }: {
  projects: Project[];
  close: () => void;
  open: (project: SearchItem | Project) => void;
  openTask?: (task: SearchItem | Task) => void;
  ai: () => void;
}) {
  const [q, setQ] = useState(""),
    [result, setResult] = useState<SearchResponse | null>(null),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [activeIndex, setActiveIndex] = useState(0);
  const inputId = useId();
  const listId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const searchGroups = useMemo<{ key: SearchKind; label: string; items: SearchItem[] }[]>(() => SEARCH_GROUPS.map((group) => {
    const items: SearchItem[] = group.key === "projects" && !q.trim()
      ? projects.map((project) => ({ ...project, title: project.name, created_at: (project as any).created_at }))
      : result?.[group.key] || [];
    return { ...group, items };
  }).filter((group) => group.items.length), [projects, q, result]);
  const flattened = useMemo(() => searchGroups.flatMap((group) => group.items.map((item) => ({ ...item, _kind: group.key, _kindLabel: group.label }))), [searchGroups]);

  useEffect(() => {
    let live = true;
    const query = q.trim();
    if (query) {
      setResult(null);
      setLoading(true);
    }
    const id = setTimeout(async () => {
      if (!query) {
        setResult(null);
        setError("");
        setLoading(false);
        return;
      }
      setLoading(true);
      setError("");
      try {
        const found = await api<SearchResponse>("/search?q=" + encodeURIComponent(query));
        if (live) setResult(found);
      } catch (e: any) {
        if (live) {
          setResult(null);
          setError(e?.message || "Search is unavailable right now.");
        }
      } finally {
        if (live) setLoading(false);
      }
    }, 180);
    return () => { live = false; clearTimeout(id); };
  }, [q]);

  useEffect(() => setActiveIndex(0), [q, result]);

  function selectResult(item: SearchItem & { _kind: SearchKind }) {
    close();
    if (item._kind === "projects") {
      open(item);
      return;
    }
    if (item._kind === "tasks" && openTask) {
      openTask(item);
      return;
    }
    const href = safeSourceHref(item);
    if (href) window.location.assign(href);
    else if (item.project_id) {
      const project = projects.find((candidate) => candidate.id === item.project_id);
      if (project) open(project);
    }
  }

  function onDialogKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Tab") {
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("input:not([disabled]), button:not([disabled]), [href]:not([aria-disabled='true'])") || []);
      if (focusable.length < 2) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-slate-950/35 p-4 pt-[12vh] backdrop-blur-sm" onMouseDown={(event) => event.target === event.currentTarget && close()}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={`${inputId}-title`} className="app-card rounded-card border w-full max-w-2xl overflow-hidden" onKeyDown={onDialogKeyDown}>
        <div className="flex gap-3 border-b border-line px-4">
          <Search size={18} className="my-auto text-zinc-500" />
          <h2 id={`${inputId}-title`} className="sr-only">Search workspace knowledge</h2>
          <input
            autoFocus
            id={inputId}
            role="combobox"
            aria-label="Search projects, tasks, files, discussions, and decisions"
            aria-autocomplete="list"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={flattened[activeIndex] ? `${listId}-option-${activeIndex}` : undefined}
            className="h-14 flex-1 bg-transparent text-sm outline-none"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown" && flattened.length) {
                event.preventDefault();
                setActiveIndex((index) => (index + 1) % flattened.length);
              } else if (event.key === "ArrowUp" && flattened.length) {
                event.preventDefault();
                setActiveIndex((index) => (index - 1 + flattened.length) % flattened.length);
              } else if (event.key === "Enter" && flattened[activeIndex]) {
                event.preventDefault();
                selectResult(flattened[activeIndex] as SearchItem & { _kind: SearchKind });
              }
            }}
            placeholder="Search projects, tasks, files, discussions, decisions"
          />
          <button type="button" aria-label="Close search dialog" onClick={close}>
            <X className="text-zinc-500" size={18} />
          </button>
        </div>
        <div className="max-h-[min(60vh,32rem)] overflow-auto p-2">
          <button type="button" onClick={ai} className="flex w-full gap-3 rounded-lg p-3 text-left transition hover:bg-blue-50">
            <Sparkles className="text-accent" size={17} />
            <span className="text-sm">Ask Enter AI</span>
          </button>
          <div id={listId} role="listbox" aria-label="Workspace search results" aria-busy={loading} className="mt-2">
            {loading && <p role="status" className="px-3 py-2 text-xs text-slate-500">Searching workspace…</p>}
            {error && <p role="alert" className="px-3 py-2 text-sm text-red-700">{error}</p>}
            {!q.trim() && <p className="px-3 py-2 text-xs text-slate-500">Search authorized projects, tasks, files, discussions, and decisions.</p>}
            {searchGroups.map((group) => (
              <div key={group.key} role="group" aria-label={group.label} className="mt-2">
                <h3 className="px-3 py-2 text-[10px] font-bold uppercase tracking-[.12em] text-slate-500">{group.label}</h3>
                {group.items.map((item) => {
                  const index = flattened.findIndex((candidate) => candidate._kind === group.key && candidate.id === item.id);
                  const title = item.title || item.name || item.file_name || "Untitled result";
                  return (
                    <button
                      type="button"
                      role="option"
                      aria-selected={activeIndex === index}
                      id={`${listId}-option-${index}`}
                      key={`${group.key}:${item.id}`}
                      onMouseEnter={() => setActiveIndex(index)}
                      onClick={() => selectResult({ ...item, _kind: group.key })}
                      className={cn("flex w-full gap-3 rounded-lg p-3 text-left transition", activeIndex === index ? "bg-blue-50 ring-1 ring-blue-200" : "hover:bg-blue-50")}
                    >
                      {group.key === "projects" ? <FolderKanban size={16} className="mt-0.5 shrink-0 text-zinc-500" /> : <Circle size={13} className="mt-1 shrink-0 text-blue-600" />}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium text-ink">{title}</span>
                        <span className="mt-0.5 block truncate text-xs text-slate-500">{group.label} · <time dateTime={item.created_at || undefined}>{displayDate(item.created_at)}</time></span>
                        {item.snippet && <span className="mt-1 block line-clamp-2 text-xs leading-5 text-slate-600">{item.snippet}</span>}
                        {item.source_ref && <span className="mt-1 block truncate text-[11px] text-slate-500">Source: {item.source_ref}</span>}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
            {!loading && q.trim() && !error && flattened.length === 0 && <p role="status" className="px-3 py-5 text-center text-sm text-slate-500">No matching workspace knowledge found.</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
