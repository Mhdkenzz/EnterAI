"use client";
import { useState } from "react";
import {
  Archive,
  ChevronLeft,
  ChevronRight,
  FileText,
  Grid2X2,
  LayoutList,
  Plus,
  Sparkles,
  Trash2,
} from "lucide-react";
import { API_BASE } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { states, when, Avatar, Health, Row, describeActivity } from "@/components/workspace/shared";
import type { Doc, Project, Task, InboxNotice } from "@/lib/types";

export function Home({ data, activity, project, task, toggle, all, ai }: any) {
  const tasks: Task[] = data?.my_tasks || [];
  return (
    <>
      <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-sky-300">
            {new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(
              new Date(),
            )}
          </p>
          <h1 className="mt-1 text-2xl font-semibold">Your workspace</h1>
          <p className="mt-1 text-sm text-zinc-400">Keep work moving or ask Enter AI to help.</p>
        </div>
        <Button onClick={ai}>
          <Sparkles size={16} />
          Ask Enter AI
        </Button>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[
          ["Active projects", data?.stats?.active_projects, "text-sky-300"],
          ["At risk", data?.stats?.at_risk, "text-red-300"],
          ["Open tasks", data?.stats?.my_open_tasks, "text-amber-300"],
          ["Completed", data?.stats?.completed_this_week, "text-emerald-300"],
        ].map(([l, v, c]) => (
          <Card key={String(l)} className="p-4">
            <p className="text-xs text-zinc-500">{l}</p>
            <p className={cn("mt-2 text-2xl font-semibold", String(c))}>{v ?? "-"}</p>
          </Card>
        ))}
      </div>
      <div className="grid gap-5 xl:grid-cols-[1.45fr_.85fr]">
        <Card>
          <div className="flex justify-between border-b border-line px-5 py-4">
            <div>
              <h2 className="text-sm font-semibold">My priorities</h2>
              <p className="text-xs text-zinc-500">Completed work stays visible.</p>
            </div>
            <Button variant="ghost" size="sm" onClick={all}>
              View all
            </Button>
          </div>
          {tasks.length ? (
            tasks
              .slice(0, 6)
              .map((t) => <Row key={t.id} task={t} open={() => task(t)} toggle={() => toggle(t)} />)
          ) : (
            <p className="p-5 text-sm text-zinc-500">No assigned work yet.</p>
          )}
        </Card>
        <Card>
          <div className="border-b border-line px-5 py-4">
            <h2 className="text-sm font-semibold">Project health</h2>
          </div>
          <div className="space-y-4 p-5">
            {(data?.projects || []).map((p: Project) => (
              <button key={p.id} onClick={() => project(p)} className="block w-full text-left">
                <div className="mb-1.5 flex justify-between">
                  <span className="text-sm">{p.name}</span>
                  <Health health={p.health} />
                </div>
                <div className="h-1.5 rounded-full bg-white/5">
                  <div
                    className="h-full rounded-full"
                    style={{ width: p.progress + "%", background: p.color }}
                  />
                </div>
                <p className="mt-1 text-xs text-zinc-500">
                  {p.progress}% complete, {p.task_count} {p.task_count === 1 ? "task" : "tasks"}
                </p>
              </button>
            ))}
          </div>
        </Card>
      </div>
      <Card className="mt-5">
        <div className="border-b border-line px-5 py-4 text-sm font-semibold">Recent activity</div>
        {activity.slice(0, 5).map((a: any) => (
          <div key={a.id} className="flex gap-3 border-b border-line/60 px-5 py-3">
            <Avatar user={a.actor} />
            <p className="text-sm text-zinc-300">
              <b className="text-white">{a.actor?.name || "Enter AI"}</b> {describeActivity(a)}
            </p>
          </div>
        ))}
      </Card>
    </>
  );
}
export function Portfolio({ projects: all, open, create }: any) {
  const [showArchived, setShowArchived] = useState(false);
  const archivedCount = all.filter((p: Project) => p.status === "archived").length;
  const projects = all.filter((p: Project) => showArchived || p.status !== "archived");
  return (
    <>
      <div className="mb-6 flex justify-between">
        <div>
          <p className="text-sm text-sky-300">Portfolio</p>
          <h1 className="mt-1 text-2xl font-semibold">All projects</h1>
          {archivedCount > 0 && (
            <button
              className="mt-1 text-xs text-sky-300 hover:underline"
              onClick={() => setShowArchived((v) => !v)}
            >
              {showArchived ? "Hide archived" : `Show ${archivedCount} archived`}
            </button>
          )}
        </div>
        <Button onClick={create}>
          <Plus size={15} />
          New project
        </Button>
      </div>
      {!projects.length && (
        <Card className="p-8 text-center">
          <p className="font-medium">No projects yet</p>
          <p className="mt-1 text-sm text-zinc-400">
            Create one, or upload a brief and let Enter AI draft it.
          </p>
        </Card>
      )}
      <Card className={cn("overflow-x-auto", !projects.length && "hidden")}>
        <div className="min-w-[650px]">
          <div className="grid grid-cols-[1.6fr_.8fr_.6fr_.5fr] gap-4 border-b border-line px-5 py-2 text-xs uppercase tracking-wide text-zinc-500">
            <span>Project</span>
            <span>Team</span>
            <span>Health</span>
            <span>Progress</span>
          </div>
          {projects.map((p: Project) => (
            <button
              key={p.id}
              onClick={() => open(p)}
              className="grid w-full grid-cols-[1.6fr_.8fr_.6fr_.5fr] gap-4 border-b border-line/60 px-5 py-4 text-left hover:bg-white/[.025]"
            >
              <span className="text-sm font-medium">
                <i className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: p.color }} />
                {p.name}
                {p.status === "archived" && <small className="ml-2 text-amber-200">Archived</small>}
                <small className="ml-2 text-zinc-600">{p.code}</small>
              </span>
              <span className="text-sm text-zinc-400">{p.team || "-"}</span>
              <Health health={p.health} />
              <span className="text-sm text-zinc-400">{p.progress}%</span>
            </button>
          ))}
        </div>
      </Card>
    </>
  );
}
export function Work({ tasks, open, toggle }: any) {
  return (
    <>
      <div className="mb-6">
        <p className="text-sm text-sky-300">Personal execution</p>
        <h1 className="mt-1 text-2xl font-semibold">My work</h1>
        <p className="mt-1 text-sm text-zinc-500">Tick work off without losing it from the list.</p>
      </div>
      <Card>
        {tasks.length ? (
          tasks.map((t: Task) => <Row task={t} key={t.id} open={() => open(t)} toggle={() => toggle(t)} />)
        ) : (
          <p className="p-6 text-sm text-zinc-500">No work assigned.</p>
        )}
      </Card>
    </>
  );
}
export function InboxView({ notices, open }: any) {
  return (
    <>
      <div className="mb-6">
        <p className="text-sm text-sky-300">Updates</p>
        <h1 className="mt-1 text-2xl font-semibold">Inbox</h1>
        <p className="mt-1 text-sm text-zinc-500">Open an update to mark it read and jump to its project.</p>
      </div>
      <Card>
        {notices.length ? (
          notices.map((n: InboxNotice) => (
            <button
              key={n.id}
              onClick={() => open(n)}
              className={cn(
                "flex w-full gap-4 border-b border-line/70 px-5 py-4 text-left hover:bg-white/[.02]",
                !n.read && "bg-sky-400/[.035]",
              )}
            >
              <i className={cn("mt-1.5 h-2 w-2 rounded-full", n.read ? "bg-transparent" : "bg-sky-400")} />
              <span>
                <b className="block text-sm">{n.title}</b>
                <span className="mt-1 block text-sm text-zinc-400">{n.body}</span>
              </span>
            </button>
          ))
        ) : (
          <p className="p-6 text-sm text-zinc-500">Inbox zero.</p>
        )}
      </Card>
    </>
  );
}
export function Teams({ teams, selected, projects, open, create, select }: any) {
  return (
    <>
      <div className="mb-6 flex justify-between">
        <div>
          <p className="text-sm text-sky-300">People and ownership</p>
          <h1 className="mt-1 text-2xl font-semibold">Teams</h1>
        </div>
        <Button onClick={create}>
          <Plus size={15} />
          New team
        </Button>
      </div>
      {!teams.length && (
        <Card className="p-8 text-center">
          <p className="font-medium">No teams yet</p>
          <p className="mt-1 text-sm text-zinc-400">
            Teams group projects by who owns them. Create one to get started.
          </p>
        </Card>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {teams.map((t: any) => (
          <button
            key={t.id}
            onClick={() => select(t)}
            className={cn(
              "rounded-lg border p-5 text-left",
              selected?.id === t.id
                ? "border-sky-400/50 bg-sky-400/[.05]"
                : "border-line bg-panel hover:border-zinc-600",
            )}
          >
            <h2 className="font-medium">{t.name}</h2>
            <p className="mt-1 text-sm text-zinc-500">{t.description || "No description"}</p>
            <p className="mt-5 text-xs text-zinc-500">
              {t.projects} {t.projects === 1 ? "project" : "projects"}. Open to view.
            </p>
          </button>
        ))}
      </div>
      {selected && (
        <section className="mt-6">
          <h2 className="mb-3 text-sm font-semibold">{selected.name} projects</h2>
          <div className="grid gap-3 md:grid-cols-2">
            {projects.length ? (
              projects.map((p: Project) => (
                <button
                  key={p.id}
                  onClick={() => open(p)}
                  className="rounded-lg border border-line bg-panel p-4 text-left hover:border-zinc-600"
                >
                  <div className="flex justify-between">
                    <span className="text-sm font-medium">{p.name}</span>
                    <Health health={p.health} />
                  </div>
                  <p className="mt-2 text-xs text-zinc-500">{p.progress}% complete</p>
                </button>
              ))
            ) : (
              <p className="text-sm text-zinc-500">No projects assigned.</p>
            )}
          </div>
        </section>
      )}
    </>
  );
}
export function ProjectView({
  project,
  tasks,
  open,
  update,
  edit,
  create,
  ai,
  upload: put,
  canManage,
  archive,
  remove,
}: any) {
  const [mode, setMode] = useState("board");
  if (!project) return <p className="text-zinc-500">Choose a project.</p>;
  // Subtasks live inside their parent (the task drawer); the board and list show
  // top-level work with a count, so a checklist does not flood the columns.
  const topLevel = tasks.filter((t: Task) => !t.parent_id);
  const subtaskCount = (t: Task) => tasks.filter((x: Task) => x.parent_id === t.id).length;
  const archived = project.status === "archived";
  return (
    <>
      {archived && (
        <div
          role="status"
          className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-400/30 bg-amber-400/[.06] px-4 py-3 text-sm text-amber-100"
        >
          <span>This project is archived. It is hidden from the portfolio but nothing has been deleted.</span>
          {canManage && (
            <Button size="sm" variant="ghost" onClick={() => archive(false)}>
              Restore project
            </Button>
          )}
        </div>
      )}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-sm text-sky-300">{project.code}</p>
          <div className="mt-1 flex gap-3">
            <i className="mt-2 h-3 w-3 rounded-full" style={{ background: project.color }} />
            <h1 className="text-2xl font-semibold">{project.name}</h1>
            <Health health={project.health} />
          </div>
          <p className="mt-2 text-sm text-zinc-400">{project.description}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="ghost" size="sm" onClick={ai}>
            <Sparkles size={15} />
            Ask Enter AI
          </Button>
          <Button variant="ghost" size="sm" onClick={edit}>
            Edit project
          </Button>
          {canManage && !archived && (
            <Button variant="ghost" size="sm" onClick={() => archive(true)}>
              <Archive size={15} />
              Archive
            </Button>
          )}
          {canManage && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                if (
                  window.confirm(
                    `Delete “${project.name}” permanently? Its ${project.task_count} tasks, comments and files will be deleted. Archive it instead to keep them.`,
                  )
                )
                  remove();
              }}
            >
              <Trash2 size={15} />
              Delete
            </Button>
          )}
          <div className="flex rounded-lg border border-line bg-panel p-1">
            <button
              aria-label="Board view"
              className={cn("rounded p-1.5", mode === "board" && "bg-white/10")}
              onClick={() => setMode("board")}
            >
              <Grid2X2 size={15} />
            </button>
            <button
              aria-label="List view"
              className={cn("rounded p-1.5", mode === "list" && "bg-white/10")}
              onClick={() => setMode("list")}
            >
              <LayoutList size={15} />
            </button>
          </div>
          <Button onClick={create}>
            <Plus size={15} />
            New task
          </Button>
        </div>
      </div>
      <Card className="mb-5 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <FileText size={16} className="text-sky-300" />
          <span className="text-sm font-medium">Project documents</span>
          {project.documents?.map((d: Doc) => (
            <a
              key={d.id}
              href={`${API_BASE}/project-documents/${d.id}/download`}
              className="rounded bg-white/5 px-2 py-1 text-xs text-sky-300 hover:underline"
            >
              {d.file_name}
            </a>
          ))}
          <label className="ml-auto cursor-pointer text-xs text-sky-300">
            Attach document
            <input
              className="hidden"
              type="file"
              accept=".pdf,.doc,.docx,.txt,.xlsx,.csv,.json"
              onChange={(e) => e.target.files?.[0] && put(e.target.files[0])}
            />
          </label>
        </div>
      </Card>
      {mode === "list" ? (
        <Card>
          {topLevel.length === 0 && <p className="px-5 py-6 text-sm text-zinc-400">No tasks yet.</p>}
          {topLevel.map((t: Task) => (
            <Row
              key={t.id}
              task={t}
              open={() => open(t)}
              toggle={() => update(t, { status: t.status === "done" ? "todo" : "done" })}
            />
          ))}
        </Card>
      ) : (
        <Kanban tasks={topLevel} subtaskCount={subtaskCount} open={open} update={update} />
      )}
    </>
  );
}
export function Kanban({ tasks, open, update, subtaskCount = () => 0 }: any) {
  const [over, setOver] = useState<string | null>(null);
  const move = (t: Task, step: number) => {
    const i = states.indexOf(t.status) + step;
    if (i >= 0 && i < states.length) update(t, { status: states[i] });
  };
  return (
    // Tracks never shrink below a readable card width; when five don't fit beside the
    // sidebar the board scrolls sideways instead of overlapping its columns.
    <div className="grid gap-3 overflow-x-auto pb-3 lg:grid-cols-[repeat(5,minmax(14rem,1fr))]">
      {states.map((s) => (
        <section
          aria-label={s.replace("_", " ")}
          className="min-w-0"
          key={s}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(s);
          }}
          onDragLeave={() => setOver((current) => (current === s ? null : current))}
          onDrop={(e) => {
            e.preventDefault();
            setOver(null);
            const task = tasks.find((t: Task) => t.id === e.dataTransfer.getData("text/task-id"));
            if (task && task.status !== s) update(task, { status: s });
          }}
        >
          <div className="mb-3 flex justify-between px-1 text-xs text-zinc-400">
            <span className="capitalize">{s.replace("_", " ")}</span>
            <span>{tasks.filter((t: Task) => t.status === s).length}</span>
          </div>
          <div
            className={cn(
              "min-h-40 space-y-2 rounded-lg bg-white/[.025] p-2 transition-colors",
              over === s && "bg-sky-400/[.08] ring-1 ring-sky-400/40",
            )}
          >
            {tasks
              .filter((t: Task) => t.status === s)
              .map((t: Task) => {
                const subtasks = subtaskCount(t);
                return (
                  <article
                    key={t.id}
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData("text/task-id", t.id);
                      e.dataTransfer.effectAllowed = "move";
                    }}
                    className={cn(
                      "cursor-grab rounded-lg border border-line bg-panel p-3 hover:border-zinc-600 active:cursor-grabbing",
                      t.status === "done" && "border-emerald-500/30 bg-emerald-500/[.035]",
                    )}
                  >
                    <div className="mb-3 flex items-center justify-between gap-2">
                      <Badge
                        tone={t.priority === "high" ? "red" : t.priority === "medium" ? "amber" : "default"}
                      >
                        {t.priority}
                      </Badge>
                      <span className="flex">
                        <button
                          aria-label={"Move back " + t.title}
                          disabled={t.status === states[0]}
                          className="text-zinc-500 hover:text-sky-300 disabled:opacity-30"
                          onClick={() => move(t, -1)}
                        >
                          <ChevronLeft size={16} />
                        </button>
                        <button
                          aria-label={"Advance " + t.title}
                          disabled={t.status === states[states.length - 1]}
                          className="text-zinc-500 hover:text-sky-300 disabled:opacity-30"
                          onClick={() => move(t, 1)}
                        >
                          <ChevronRight size={16} />
                        </button>
                      </span>
                    </div>
                    <button
                      onClick={() => open(t)}
                      className={cn(
                        "block w-full text-left text-sm hover:underline",
                        t.status === "done" && "text-emerald-100/70 line-through",
                      )}
                    >
                      {t.title}
                    </button>
                    {(!!t.labels?.length || subtasks > 0) && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        {t.labels?.slice(0, 3).map((l: string) => (
                          <Badge key={l} tone="blue">
                            {l}
                          </Badge>
                        ))}
                        {subtasks > 0 && (
                          <span className="text-xs text-zinc-400">
                            {subtasks} {subtasks === 1 ? "subtask" : "subtasks"}
                          </span>
                        )}
                      </div>
                    )}
                    <div className="mt-4 flex justify-between">
                      <span className="text-xs text-zinc-600">{when(t.due_date)}</span>
                      <Avatar user={t.assignee} />
                    </div>
                  </article>
                );
              })}
          </div>
        </section>
      ))}
    </div>
  );
}
