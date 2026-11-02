"use client";
import { useState } from "react";
import {
  Archive,
  ArrowUpRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  FileText,
  FolderKanban,
  Grid2X2,
  Layers3,
  LayoutList,
  ListTodo,
  Plus,
  Search,
  Sparkles,
  Trash2,
  Users,
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
  const stats = [
    { label: "Active projects", value: data?.stats?.active_projects, icon: Layers3, tone: "blue" },
    { label: "At risk", value: data?.stats?.at_risk, icon: CircleAlert, tone: "rose" },
    { label: "Open tasks", value: data?.stats?.my_open_tasks, icon: ListTodo, tone: "amber" },
    { label: "Completed this week", value: data?.stats?.completed_this_week, icon: CheckCircle2, tone: "green" },
  ];
  return (
    <>
      <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="page-kicker mb-2">
            {new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" }).format(
              new Date(),
            )}
          </p>
          <h1 className="page-title">Workspace overview</h1>
          <p className="page-description mt-2">A clear view of your projects, priorities, and team momentum.</p>
        </div>
        <Button onClick={ai}>
          <Sparkles size={16} />
          Ask Enter AI
        </Button>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {stats.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label} className="group flex items-center gap-4 p-4 transition duration-150 hover:-translate-y-0.5 hover:shadow-card sm:p-5">
            <span className={cn(
              "grid h-11 w-11 shrink-0 place-items-center rounded-xl",
              tone === "blue" && "bg-blue-50 text-blue-700",
              tone === "rose" && "bg-rose-50 text-rose-700",
              tone === "amber" && "bg-amber-50 text-amber-700",
              tone === "green" && "bg-emerald-50 text-emerald-700",
            )}>
              <Icon size={19} strokeWidth={1.8} />
            </span>
            <span>
              <span className="block text-xs font-medium text-zinc-500">{label}</span>
              <span className="mt-1 block text-2xl font-bold tracking-tight text-ink">{value ?? "—"}</span>
            </span>
          </Card>
        ))}
      </div>
      <div className="grid gap-5 xl:grid-cols-[1.45fr_.85fr]">
        <Card>
          <div className="flex items-center justify-between border-b border-line px-5 py-4">
            <div>
              <h2 className="text-sm font-semibold tracking-tight text-ink">My priorities</h2>
              <p className="mt-1 text-xs text-zinc-500">The work that needs your attention next.</p>
            </div>
            <Button variant="ghost" size="sm" onClick={all} className="text-accent hover:text-accent-hover">
              View all <ArrowUpRight size={14} />
            </Button>
          </div>
          {tasks.length ? (
            tasks
              .slice(0, 6)
              .map((t) => <Row key={t.id} task={t} open={() => task(t)} toggle={() => toggle(t)} />)
          ) : (
            <div className="p-8 text-center">
              <p className="text-sm font-medium text-ink">You’re all caught up</p>
              <p className="mt-1 text-xs text-zinc-500">New assignments will appear here.</p>
            </div>
          )}
        </Card>
        <Card>
          <div className="flex items-center justify-between border-b border-line px-5 py-4">
            <div>
              <h2 className="text-sm font-semibold tracking-tight text-ink">Project health</h2>
              <p className="mt-1 text-xs text-zinc-500">Progress across active work.</p>
            </div>
            <Layers3 size={16} className="text-slate-400" />
          </div>
          <div className="space-y-4 p-5">
            {(data?.projects || []).map((p: Project) => (
              <button key={p.id} onClick={() => project(p)} className="block w-full rounded-lg p-1 text-left transition hover:bg-slate-50 focus-visible:outline-offset-1">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <span className="truncate text-sm font-medium text-ink">{p.name}</span>
                  <Health health={p.health} />
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full rounded-full transition-[width]"
                    style={{ width: p.progress + "%", background: p.color }}
                  />
                </div>
                <p className="mt-1.5 text-[11px] text-zinc-500">
                  {p.progress}% complete <span className="px-1 text-slate-300">·</span> {p.task_count} {p.task_count === 1 ? "task" : "tasks"}
                </p>
              </button>
            ))}
            {!data?.projects?.length && <p className="text-sm text-zinc-500">Create a project to see its health here.</p>}
          </div>
        </Card>
      </div>
      <Card className="mt-5">
        <div className="border-b border-line px-5 py-4">
          <h2 className="text-sm font-semibold tracking-tight text-ink">Recent activity</h2>
          <p className="mt-1 text-xs text-zinc-500">The latest changes across your workspace.</p>
        </div>
        {activity.slice(0, 5).map((a: any, index: number) => (
          <div key={a.id} className="flex items-start gap-3 border-b border-line/60 px-5 py-3 last:border-b-0">
            <Avatar user={a.actor} />
            <p className="pt-1 text-sm leading-5 text-zinc-500">
              <b className="font-semibold text-ink">{a.actor?.name || "Enter AI"}</b> {describeActivity(a)}
            </p>
            {index === 0 && <span className="ml-auto mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500" />}
          </div>
        ))}
        {!activity.length && <p className="px-5 py-6 text-sm text-zinc-500">Activity will appear here as work moves forward.</p>}
      </Card>
    </>
  );
}
export function Portfolio({ projects: all, open, create }: any) {
  const [showArchived, setShowArchived] = useState(false);
  const [query, setQuery] = useState("");
  const archivedCount = all.filter((p: Project) => p.status === "archived").length;
  const projects = all.filter((p: Project) => {
    const matchesArchive = showArchived || p.status !== "archived";
    const text = `${p.name} ${p.code} ${p.team || ""}`.toLowerCase();
    return matchesArchive && text.includes(query.toLowerCase());
  });
  return (
    <>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="page-kicker mb-2">Portfolio</p>
          <h1 className="page-title">Projects</h1>
          <p className="page-description mt-2">Plan, track, and keep delivery moving across your teams.</p>
          {archivedCount > 0 && (
            <button
              className="mt-3 text-xs font-semibold text-accent underline-offset-2 hover:underline"
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
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-zinc-500"><span className="font-semibold text-ink">{projects.length}</span> {projects.length === 1 ? "project" : "projects"}</p>
        <label className="flex h-10 w-full max-w-sm items-center gap-2 rounded-lg border border-line bg-panel px-3 text-zinc-500 shadow-sm focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-100">
          <Search size={16} />
          <input
            aria-label="Search projects"
            className="w-full bg-transparent text-sm text-ink outline-none placeholder:text-slate-400"
            placeholder="Search projects or teams"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
      </div>
      {!projects.length ? (
        <Card className="p-10 text-center">
          <span className="mx-auto grid h-12 w-12 place-items-center rounded-xl bg-blue-50 text-blue-700"><FolderKanban size={21} /></span>
          <p className="mt-4 font-semibold text-ink">{query ? "No matching projects" : "No projects yet"}</p>
          <p className="mt-1 text-sm text-zinc-500">{query ? "Try a different project name or team." : "Create a project or upload a brief to start planning."}</p>
          {!query && <Button className="mt-5" onClick={create}><Plus size={15} /> New project</Button>}
        </Card>
      ) : (
      <Card className="overflow-x-auto">
        <div className="min-w-[680px]">
          <div className="grid grid-cols-[1.7fr_.9fr_.65fr_.5fr] gap-4 border-b border-line bg-slate-50/80 px-5 py-3 text-[10px] font-bold uppercase tracking-[.12em] text-slate-500">
            <span>Project</span><span>Team</span><span>Health</span><span>Progress</span>
          </div>
          {projects.map((p: Project) => (
            <button
              key={p.id}
              onClick={() => open(p)}
              className="grid w-full grid-cols-[1.7fr_.9fr_.65fr_.5fr] items-center gap-4 border-b border-line/70 px-5 py-4 text-left transition last:border-0 hover:bg-blue-50/35"
            >
              <span className="flex min-w-0 items-center gap-3">
                <i className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-xs font-bold text-white shadow-sm" style={{ background: p.color }}>{p.code.slice(0, 1)}</i>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold text-ink">{p.name}</span>
                  <span className="mt-0.5 block text-[10px] font-semibold uppercase tracking-wide text-slate-400">{p.code} {p.status === "archived" && "· Archived"}</span>
                </span>
              </span>
              <span className="text-sm text-zinc-400">{p.team || "-"}</span>
              <Health health={p.health} />
              <span className="flex items-center gap-2 text-xs font-semibold text-slate-600">
                <span className="h-1.5 w-14 overflow-hidden rounded-full bg-slate-100"><span className="block h-full rounded-full bg-blue-600" style={{ width: `${p.progress}%` }} /></span>
                {p.progress}%
              </span>
            </button>
          ))}
        </div>
      </Card>
      )}
    </>
  );
}
export function Work({ tasks, open, toggle }: any) {
  return (
    <>
      <div className="mb-6">
        <p className="page-kicker mb-2">Personal execution</p>
        <h1 className="page-title">My work</h1>
        <p className="page-description mt-2">Your assigned tasks, sorted by what needs attention.</p>
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
  const unread = notices.filter((n: InboxNotice) => !n.read).length;
  return (
    <>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="page-kicker mb-2">Updates</p>
          <h1 className="page-title">Inbox</h1>
          <p className="page-description mt-2">Stay current on the work happening across your workspace.</p>
        </div>
        <Badge tone={unread ? "blue" : "default"}>{unread} unread</Badge>
      </div>
      <Card>
        {notices.length ? (
          notices.map((n: InboxNotice) => (
            <button
              key={n.id}
              onClick={() => open(n)}
              className={cn(
                "flex w-full items-start gap-4 border-b border-line/70 px-5 py-4 text-left transition last:border-0 hover:bg-blue-50/35",
                !n.read && "bg-blue-50/40",
              )}
            >
              <i className={cn("mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full", n.read ? "border border-slate-300 bg-white" : "bg-blue-600 shadow-[0_0_0_3px_rgba(49,95,206,.10)]")} />
              <span className="min-w-0 flex-1">
                <b className={cn("block text-sm", n.read ? "font-medium text-slate-700" : "font-semibold text-ink")}>{n.title}</b>
                <span className="mt-1 block text-sm leading-5 text-zinc-400">{n.body}</span>
              </span>
              <ArrowUpRight size={15} className="mt-1 shrink-0 text-slate-400" />
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
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="page-kicker mb-2">People and ownership</p>
          <h1 className="page-title">Teams</h1>
          <p className="page-description mt-2">Organize ownership and see each team’s active projects.</p>
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
              "app-card rounded-card border p-5 text-left transition duration-150 hover:-translate-y-0.5 hover:shadow-card",
              selected?.id === t.id
                ? "border-blue-300 ring-2 ring-blue-100"
                : "hover:border-blue-200",
            )}
          >
            <div className="flex items-start justify-between gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-xl bg-indigo-50 text-indigo-700"><Users size={18} /></span>
              <Badge tone="blue">{t.projects} {t.projects === 1 ? "project" : "projects"}</Badge>
            </div>
            <h2 className="mt-4 font-semibold text-ink">{t.name}</h2>
            <p className="mt-1 text-sm text-zinc-500">{t.description || "No description"}</p>
            <p className="mt-4 flex items-center gap-1 text-xs font-semibold text-accent">View team <ArrowUpRight size={13} /></p>
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
                  className="app-card rounded-xl border p-4 text-left transition hover:border-blue-200 hover:shadow-card"
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
          className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
        >
          <span>This project is archived. It is hidden from the portfolio but nothing has been deleted.</span>
          {canManage && (
            <Button size="sm" variant="outline" onClick={() => archive(false)}>
              Restore project
            </Button>
          )}
        </div>
      )}
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="page-kicker mb-2">{project.code}</p>
          <div className="mt-1 flex flex-wrap items-center gap-3">
            <i className="h-3 w-3 rounded-full ring-4 ring-white" style={{ background: project.color }} />
            <h1 className="page-title">{project.name}</h1>
            <Health health={project.health} />
          </div>
          <p className="page-description mt-2 max-w-2xl">{project.description}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={ai}>
            <Sparkles size={15} />
            Ask Enter AI
          </Button>
          <Button variant="outline" size="sm" onClick={edit}>
            Edit project
          </Button>
          {canManage && !archived && (
            <Button variant="outline" size="sm" onClick={() => archive(true)}>
              <Archive size={15} />
              Archive
            </Button>
          )}
          {canManage && (
            <Button
              variant="danger"
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
          <div className="flex rounded-lg border border-line bg-slate-100 p-1">
            <button
              aria-label="Board view"
              className={cn("rounded-md p-1.5 text-slate-500 transition hover:text-ink", mode === "board" && "bg-white text-blue-700 shadow-sm")}
              onClick={() => setMode("board")}
            >
              <Grid2X2 size={15} />
            </button>
            <button
              aria-label="List view"
              className={cn("rounded-md p-1.5 text-slate-500 transition hover:text-ink", mode === "list" && "bg-white text-blue-700 shadow-sm")}
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
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-blue-50 text-blue-700"><FileText size={16} /></span>
          <span className="text-sm font-semibold text-ink">Project documents</span>
          {project.documents?.map((d: Doc) => (
            <a
              key={d.id}
              href={`${API_BASE}/project-documents/${d.id}/download`}
              className="rounded-md border border-blue-100 bg-blue-50 px-2.5 py-1.5 text-xs font-medium text-blue-800 hover:border-blue-200 hover:bg-blue-100"
            >
              {d.file_name}
            </a>
          ))}
          <label className="ml-auto cursor-pointer rounded-lg px-3 py-2 text-xs font-semibold text-accent transition hover:bg-blue-50">
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
          <div className="mb-2 flex items-center justify-between rounded-t-lg border-b-2 border-slate-200 px-2 pb-2 text-[11px] font-bold uppercase tracking-[.1em] text-slate-500">
            <span className="capitalize">{s.replace("_", " ")}</span>
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">{tasks.filter((t: Task) => t.status === s).length}</span>
          </div>
          <div
            className={cn(
              "min-h-40 space-y-2 rounded-xl border border-dashed border-slate-200 bg-slate-100/70 p-2 transition-colors",
              over === s && "border-blue-300 bg-blue-50 ring-2 ring-blue-100",
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
                      "cursor-grab rounded-xl border border-line bg-white p-3.5 shadow-sm transition hover:-translate-y-0.5 hover:border-blue-200 hover:shadow-card active:cursor-grabbing",
                      t.status === "done" && "border-emerald-200 bg-emerald-50/70",
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
                          className="rounded p-1 text-slate-400 transition hover:bg-blue-50 hover:text-blue-700 disabled:opacity-30"
                          onClick={() => move(t, -1)}
                        >
                          <ChevronLeft size={16} />
                        </button>
                        <button
                          aria-label={"Advance " + t.title}
                          disabled={t.status === states[states.length - 1]}
                          className="rounded p-1 text-slate-400 transition hover:bg-blue-50 hover:text-blue-700 disabled:opacity-30"
                          onClick={() => move(t, 1)}
                        >
                          <ChevronRight size={16} />
                        </button>
                      </span>
                    </div>
                    <button
                      onClick={() => open(t)}
                      className={cn(
                        "block w-full text-left text-sm font-medium leading-5 text-ink hover:text-accent hover:underline",
                        t.status === "done" && "text-emerald-700/70 line-through",
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
                    <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-2.5">
                      <span className="text-[10px] font-medium text-slate-500">{when(t.due_date)}</span>
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
