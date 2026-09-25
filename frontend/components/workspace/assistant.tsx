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
    <div className="mx-auto max-w-4xl">
      <div className="mb-7 text-center">
        <div className="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-xl bg-sky-400/10 text-sky-300">
          <Sparkles size={22} />
        </div>
        <h1 className="text-2xl font-semibold">Enter AI Copilot</h1>
        <p className="mt-2 text-sm text-zinc-500">
          Grounded in your workspace. No change happens without your confirmation.
        </p>
      </div>
      <Card className="min-h-[480px] overflow-hidden">
        {project ? (
          <div className="flex items-center justify-between border-b border-line bg-sky-400/[.04] px-5 py-2 text-xs text-sky-200">
            <span>
              Scoped to <b>{project.name}</b> -- only this project's data is used.
            </span>
            <button className="text-sky-300 underline-offset-2 hover:underline" onClick={clear}>
              Ask about everything instead
            </button>
          </div>
        ) : (
          <div className="border-b border-line px-5 py-3 text-xs text-zinc-400">
            Read tools: projects, tasks, and priority summaries. Write tools are proposed first.
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
              className="rounded-full border border-line px-3 py-1 text-xs text-sky-200 hover:border-sky-400/50"
              onClick={() => ask(prompt)}
              disabled={busy}
            >
              {prompt}
            </button>
          ))}
        </div>
        <div className="space-y-5 p-5">
          {messages.map((m: any, i: number) => (
            <div className={cn("flex gap-3", m.role === "user" && "justify-end")} key={i}>
              {m.role === "assistant" && (
                <div className="grid h-7 w-7 place-items-center rounded-lg bg-sky-400/10 text-sky-300">
                  <Bot size={15} />
                </div>
              )}
              <div
                className={cn(
                  "max-w-[82%] rounded-lg px-4 py-3 text-sm",
                  m.role === "assistant" ? "bg-white/[.045]" : "bg-sky-400 text-slate-950",
                )}
              >
                <p>{m.body}</p>
                {m.actions?.map((a: Action) => {
                  const outcome = outcomes[a.confirmation_token];
                  return (
                    <div
                      key={a.confirmation_token}
                      className="mt-3 rounded-lg border border-sky-400/30 bg-black/20 p-3"
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
                      {outcome === "done" && <p className="mt-3 text-xs text-emerald-300">Done.</p>}
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
          {busy && <Loader2 className="animate-spin text-sky-300" size={18} />}{" "}
          {error && (
            <p role="alert" className="text-sm text-red-300">
              {error}
            </p>
          )}
        </div>
        <div className="border-t border-line p-4">
          <div className="flex gap-2 rounded-lg border border-line bg-canvas p-2 focus-within:border-accent">
            <Sparkles className="m-1 text-sky-300" size={16} />
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
    <div className="fixed inset-0 z-50 grid place-items-start bg-black/65 p-4 pt-[12vh]" onMouseDown={close}>
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
          <button onClick={ai} className="flex w-full gap-3 rounded-lg p-3 text-left hover:bg-sky-400/10">
            <Sparkles className="text-sky-300" size={17} />
            <span className="text-sm">Ask Enter AI</span>
          </button>
          {(!q ? projects : result?.projects || []).map((p: Project) => (
            <button
              key={p.id}
              onClick={() => open(p)}
              className="flex w-full gap-3 rounded-lg p-3 text-left hover:bg-white/5"
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
