"use client";
import { useEffect, useState } from "react";
import { Bot, Check, Circle, FolderKanban, Loader2, Search, Send, Sparkles, X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Project, Task, Action } from "@/lib/types";

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
      setMessages((x: any[]) => [...x, { role: "assistant", body: r.reply, actions: r.actions }]);
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
export function Command({ projects, close, open, ai }: any) {
  const [q, setQ] = useState(""),
    [result, setResult] = useState<any>(null);
  useEffect(() => {
    const id = setTimeout(() => {
      if (q.trim())
        api("/search?q=" + encodeURIComponent(q))
          .then(setResult)
          .catch(() => setResult(null));
    }, 180);
    return () => clearTimeout(id);
  }, [q]);
  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-slate-950/35 p-4 pt-[12vh] backdrop-blur-sm" onMouseDown={close}>
      <Card className="w-full max-w-2xl overflow-hidden" onMouseDown={(e) => e.stopPropagation()}>
        <div className="flex gap-3 border-b border-line px-4">
          <Search size={18} className="my-auto text-zinc-500" />
          <input
            autoFocus
            className="h-14 flex-1 bg-transparent text-sm outline-none"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search projects and tasks"
          />
          <button aria-label="Close command bar" onClick={close}>
            <X className="text-zinc-500" size={18} />
          </button>
        </div>
        <div className="max-h-80 overflow-auto p-2">
          <button onClick={ai} className="flex w-full gap-3 rounded-lg p-3 text-left transition hover:bg-blue-50">
            <Sparkles className="text-accent" size={17} />
            <span className="text-sm">Ask Enter AI</span>
          </button>
          {(!q ? projects : result?.projects || []).map((p: Project) => (
            <button
              key={p.id}
              onClick={() => open(p)}
              className="flex w-full gap-3 rounded-lg p-3 text-left transition hover:bg-blue-50"
            >
              <FolderKanban size={16} className="text-zinc-500" />
              <span className="text-sm">{p.name}</span>
            </button>
          ))}
          {result?.tasks?.map((t: Task) => (
            <div key={t.id} className="flex gap-3 p-3 text-sm">
              <Circle size={13} />
              {t.title}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
