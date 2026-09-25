"use client";
import { useEffect, useState } from "react";
import { Bot, Check, Circle, FolderKanban, Loader2, Search, Send, Sparkles, X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Project, Task, Action } from "@/lib/types";

export function AI({ project, clear, onAction }: any) {
  const [text, setText] = useState("What should I work on today?"),
    [messages, setMessages] = useState<any[]>([
      {
        role: "assistant",
        body: "I can read your workspace and propose changes. Every write needs your confirmation.",
      },
    ]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function ask(question = text) {
    if (!question.trim() || busy) return;
    setText("");
    setError("");
    setMessages((x) => [...x, { role: "user", body: question }]);
    setBusy(true);
    try {
      const r = await api<any>("/ai/plan", {
        method: "POST",
        body: JSON.stringify({ message: question, project_id: project?.id || null }),
      });
      setMessages((x) => [...x, { role: "assistant", body: r.reply, actions: r.actions }]);
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
          {["What should I work on today?", "What is at risk?", "Summarize AI Workspace"].map((prompt) => (
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
          {messages.map((m, i) => (
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
                {m.actions?.map((a: Action) => (
                  <div key={a.label} className="mt-3 rounded-lg border border-sky-400/30 bg-black/20 p-3">
                    <p>{a.label}</p>
                    <Button
                      size="sm"
                      className="mt-3"
                      onClick={async () => {
                        try {
                          await onAction(a);
                          setMessages((x) => [...x, { role: "assistant", body: "Done. " + a.label }]);
                        } catch (e: any) {
                          setError(e.message || "Confirmation failed. Please ask for a fresh proposal.");
                        }
                      }}
                    >
                      <Check size={14} />
                      Confirm and run
                    </Button>
                  </div>
                ))}
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
