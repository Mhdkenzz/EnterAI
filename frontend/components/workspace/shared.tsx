"use client";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { Check, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { Task } from "@/lib/types";

export const states = ["backlog", "todo", "in_progress", "review", "done"],
  field =
    "mt-1.5 w-full rounded-lg border border-line bg-canvas px-3 py-2.5 text-sm outline-none focus:border-accent";
export const when = (v?: string) =>
  v ? new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "No date";
export function Avatar({ user }: { user?: { name?: string; avatar?: string } }) {
  return (
    <span className="inline-grid h-7 w-7 shrink-0 place-items-center rounded-full bg-sky-400/15 text-[10px] font-bold text-sky-200">
      {user?.avatar ||
        user?.name
          ?.split(" ")
          .map((x) => x[0])
          .join("") ||
        "EA"}
    </span>
  );
}
export function Health({ health }: { health: string }) {
  return (
    <Badge tone={health === "at_risk" ? "red" : health === "on_track" ? "green" : "amber"}>
      {health.replace("_", " ")}
    </Badge>
  );
}
export function Row({ task, open, toggle }: { task: Task; open: () => void; toggle: () => void }) {
  const done = task.status === "done";
  return (
    <div
      className={cn(
        "flex items-center gap-3 border-b border-line/70 px-4 py-3",
        done && "bg-emerald-500/[.035]",
      )}
    >
      <button
        aria-label={(done ? "Reopen " : "Complete ") + task.title}
        onClick={toggle}
        className={cn(
          "grid h-5 w-5 shrink-0 place-items-center rounded-full border",
          done ? "border-emerald-400 bg-emerald-400 text-slate-950" : "border-zinc-600 hover:border-accent",
        )}
      >
        {done && <Check size={12} />}
      </button>
      <button onClick={open} className="min-w-0 flex-1 text-left">
        <p className={cn("truncate text-sm", done && "text-emerald-200/70 line-through")}>{task.title}</p>
        <div className="mt-1 flex gap-2">
          <Badge tone={task.priority === "high" ? "red" : task.priority === "medium" ? "amber" : "default"}>
            {task.priority}
          </Badge>
          {task.labels?.slice(0, 1).map((x) => (
            <Badge key={x} tone="blue">
              {x}
            </Badge>
          ))}
        </div>
      </button>
      <span className="hidden text-xs text-zinc-500 sm:block">
        {done ? "Completed" : when(task.due_date)}
      </span>
      <Avatar user={task.assignee} />
    </div>
  );
}
export function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // Parents pass a new `close` on every render; read the latest through a ref so the
  // effect below runs once, instead of stealing focus back on each re-render.
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    // Keyboard users land inside the dialog, and Escape closes it.
    panel.current
      ?.querySelector<HTMLElement>("input, select, textarea, button:not([aria-label='Close dialog'])")
      ?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4" onMouseDown={close}>
      <Card
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto p-5 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-5 flex justify-between">
          <h2 id={titleId} className="font-medium">
            {title}
          </h2>
          <button aria-label="Close dialog" onClick={close}>
            <X className="text-zinc-500" size={18} />
          </button>
        </div>
        {children}
      </Card>
    </div>
  );
}
