"use client";
import { useEffect, useState, type FormEvent } from "react";
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
  MessageSquareText,
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
import { states, when, Avatar, Health, Row, describeActivity, field, Modal } from "@/components/workspace/shared";
import type { Doc, Project, Task, InboxNotice, InboundMessage, InboundPlan } from "@/lib/types";

export function Home({ data, activity, project, task, toggle, all, ai }: any) {
  const tasks: Task[] = data?.my_tasks || [];
  const stats = [
    { label: "Active projects", value: data?.stats?.active_projects, icon: Layers3, tone: "blue" },
    { label: "At risk", value: data?.stats?.at_risk, icon: CircleAlert, tone: "rose" },
    { label: "Open tasks", value: data?.stats?.my_open_tasks, icon: ListTodo, tone: "amber" },
    { label: "Completed this week", value: data?.stats?.completed_this_week, icon: CheckCircle2, tone: "green" },
    { label: "New requests", value: data?.stats?.new_requests, icon: MessageSquareText, tone: "blue" },
    { label: "Plans awaiting approval", value: data?.stats?.plans_awaiting_approval, icon: CheckCircle2, tone: "amber" },
    { label: "Blocked tasks", value: data?.stats?.blocked_tasks, icon: CircleAlert, tone: "rose" },
    { label: "Projects awaiting review", value: data?.stats?.projects_awaiting_review, icon: CheckCircle2, tone: "amber" },
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
                  <span className="mt-0.5 block text-[10px] font-semibold uppercase tracking-wide text-slate-400">{p.code}{p.status === "archived" ? " · Archived" : p.status === "review" ? " · Review needed" : p.status === "completed" ? " · Complete" : ""}</span>
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

type RequestPlanCallbacks = {
  draftPlan: (message: InboundMessage) => Promise<void>;
  savePlan: (plan: InboundPlan, summary: string, proposal: InboundPlan["proposal"]) => Promise<void>;
  approvePlan: (plan: InboundPlan) => Promise<void>;
  rejectPlan: (plan: InboundPlan) => Promise<void>;
};

export function RequestsView({ messages, plans, create, updateStatus, draftPlan, savePlan, approvePlan, rejectPlan }: {
  messages: InboundMessage[];
  plans: InboundPlan[];
  create: (value: { source: "manual"; sender?: string; subject?: string; body: string; source_url?: string }) => Promise<void>;
  updateStatus: (message: InboundMessage, status: "new" | "ignored") => Promise<void>;
} & RequestPlanCallbacks) {
  const [capture, setCapture] = useState(false);
  const [sender, setSender] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [planError, setPlanError] = useState("");
  const active = messages.filter((message) => message.status === "new");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!body.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await create({ source: "manual", sender: sender.trim() || undefined,
        subject: subject.trim() || undefined, body: body.trim(), source_url: sourceUrl.trim() || undefined });
      setCapture(false);
      setSender(""); setSubject(""); setBody(""); setSourceUrl("");
    } catch (cause: any) {
      setError(cause?.message || "Could not save this request.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="page-kicker mb-2">Incoming work</p>
          <h1 className="page-title">Requests</h1>
          <p className="page-description mt-2">Review requests captured from your team’s work tools.</p>
        </div>
        <Button onClick={() => setCapture(true)}><Plus size={15} /> Capture request</Button>
      </div>
      <div className="mb-3 flex items-center gap-2 text-sm text-slate-500">
        <MessageSquareText size={15} /> {active.length} awaiting review
      </div>
      <Card>
        {messages.length ? messages.map((message) => {
          const plan = plans.find((item) => item.inbound_message_id === message.id);
          return (
          <article key={message.id} className="border-b border-line/70 px-5 py-4 last:border-0">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold text-ink">{message.subject || "Incoming request"}</h2>
                  <Badge tone={message.status === "new" ? "blue" : message.status === "planned" ? "green" : "default"}>{message.status === "new" ? "Needs review" : message.status === "planned" ? "Plan approved" : "Ignored"}</Badge>
                  <Badge>{message.source}</Badge>
                  {message.triage_status === "pending" || message.triage_status === "processing" ? <Badge tone="amber">CEO reviewing</Badge> : null}
                  {message.triage_status === "actionable" ? <Badge tone="green">Action requested</Badge> : null}
                  {message.triage_status === "not_actionable" ? <Badge>Informational</Badge> : null}
                  {message.triage_category && <Badge>{message.triage_category.replaceAll("_", " ")}</Badge>}
                  {message.triage_status === "retry" || message.triage_status === "failed" || message.triage_status === "needs_human" ? <Badge tone="amber">Human review needed</Badge> : null}
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  {message.sender || "No sender provided"} · {new Date(message.received_at).toLocaleString()}
                  {message.source_url && <> · <a href={message.source_url} target="_blank" rel="noreferrer" className="font-medium text-blue-700 hover:underline">Open source</a></>}
                </p>
                {message.triage_reason && <p className="mt-2 text-xs text-slate-600">CEO note: {message.triage_reason}</p>}
                {message.triage_summary && <p className="mt-2 text-sm font-medium text-ink">CEO summary: {message.triage_summary}</p>}
                {!!message.triage_concerns?.length && <p className="mt-1 text-xs text-slate-600">Concerns: {message.triage_concerns.join(", ")}</p>}
                <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">{message.body}</p>
              </div>
              {message.status !== "planned" && <div className="flex shrink-0 gap-2">
                {message.status === "new" && !plan?.project_id && <Button size="sm" onClick={() => { setPlanError(""); draftPlan(message).catch((cause: any) => setPlanError(cause?.message || "Could not draft a plan.")); }}>{plan?.status === "rejected" ? "Draft new plan" : plan ? "Open draft plan" : "Draft a plan"}</Button>}
                <Button variant="outline" size="sm" onClick={() => updateStatus(message, message.status === "new" ? "ignored" : "new")}>
                  {message.status === "new" ? "Ignore" : "Reopen"}
                </Button>
              </div>}
            </div>
            {plan?.status === "pending" && <RequestPlanReview plan={plan} save={savePlan} approve={approvePlan} reject={rejectPlan} />}
            {planError && <p role="alert" className="mt-3 text-sm text-red-700">{planError}</p>}
            {plan?.status === "rejected" && <p className="mt-3 text-xs text-slate-500">The last draft was rejected. You can create a fresh plan if the request is still relevant.</p>}
            {plan?.status === "approved" && plan.project_id && <p className="mt-3 text-sm text-emerald-800">Work was created from this approved plan. <a className="font-semibold underline" href={`/projects/${encodeURIComponent(plan.project_id)}`}>Open project</a></p>}
          </article>
          );
        }) : (
          <div className="p-8 text-center">
            <MessageSquareText className="mx-auto text-slate-300" size={22} />
            <p className="mt-3 text-sm font-medium text-ink">No requests yet</p>
            <p className="mt-1 text-sm text-slate-500">Requests from connected services will appear here. You can capture one manually while integrations are being set up.</p>
          </div>
        )}
      </Card>
      {capture && <Modal title="Capture a request" close={() => setCapture(false)}>
        <form className="space-y-4" onSubmit={submit}>
          <p className="text-sm text-slate-500">Save a request with its source details so the team can review it here.</p>
          {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
          <label className="block text-xs text-slate-500">Sender<input className={field} maxLength={500} value={sender} onChange={(event) => setSender(event.target.value)} /></label>
          <label className="block text-xs text-slate-500">Subject<input className={field} maxLength={500} value={subject} onChange={(event) => setSubject(event.target.value)} /></label>
          <label className="block text-xs text-slate-500">Source link<input className={field} type="url" placeholder="https://…" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} /></label>
          <label className="block text-xs text-slate-500">Request<textarea required className={`${field} min-h-32`} maxLength={50000} value={body} onChange={(event) => setBody(event.target.value)} /></label>
          <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={() => setCapture(false)}>Cancel</Button><Button type="submit" disabled={busy || !body.trim()}>{busy ? "Saving…" : "Save request"}</Button></div>
        </form>
      </Modal>}
    </>
  );
}

function RequestPlanReview({ plan, save, approve, reject }: {
  plan: InboundPlan;
  save: RequestPlanCallbacks["savePlan"];
  approve: RequestPlanCallbacks["approvePlan"];
  reject: RequestPlanCallbacks["rejectPlan"];
}) {
  const [summary, setSummary] = useState(plan.summary);
  const [proposal, setProposal] = useState(plan.proposal);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setSummary(plan.summary);
    setProposal(plan.proposal);
  }, [plan.id, plan.summary, plan.proposal]);
  async function run(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError("");
    try { await action(); }
    catch (cause: any) { setError(cause?.message || "Could not update the plan."); }
    finally { setBusy(false); }
  }
  const project = proposal.project;
  return (
    <form className="mt-5 rounded-xl border border-blue-200 bg-blue-50/40 p-4" onSubmit={(event) => { event.preventDefault(); run(() => save(plan, summary, proposal)); }}>
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div><h3 className="text-sm font-semibold text-ink">Project plan for review</h3><p className="mt-1 text-xs text-slate-500">{plan.planner_provider === "deterministic" ? "Auto drafted" : "AI drafted"} · Review before creating the project; eligible agents may start after approval</p></div>
        <Badge tone="amber">Pending approval</Badge>
      </div>
      <label className="block text-xs text-slate-500">Request summary<textarea className={`${field} min-h-20`} maxLength={2000} value={summary} onChange={(event) => setSummary(event.target.value)} /></label>
      <div className="mt-3 grid gap-3 sm:grid-cols-[1fr_150px]">
        <label className="block text-xs text-slate-500">Project name<input required className={field} maxLength={180} value={project.name} onChange={(event) => setProposal({ project: { ...project, name: event.target.value } })} /></label>
        <label className="block text-xs text-slate-500">Project code<input required className={field} maxLength={16} value={project.code} onChange={(event) => setProposal({ project: { ...project, code: event.target.value.toUpperCase() } })} /></label>
      </div>
      <label className="mt-3 block text-xs text-slate-500">Description<textarea className={`${field} min-h-20`} maxLength={6000} value={project.description} onChange={(event) => setProposal({ project: { ...project, description: event.target.value } })} /></label>
      <label className="mt-3 block text-xs text-slate-500">Health<select className={field} value={project.health} onChange={(event) => setProposal({ project: { ...project, health: event.target.value as typeof project.health } })}><option value="on_track">On track</option><option value="at_risk">At risk</option><option value="off_track">Off track</option></select></label>
      <div className="mt-4 space-y-3">
        <div className="flex items-center justify-between"><h4 className="text-xs font-semibold text-ink">Tasks ({project.tasks.length})</h4><Button type="button" size="sm" variant="outline" disabled={project.tasks.length >= 20} onClick={() => setProposal({ project: { ...project, tasks: [...project.tasks, { title: "", description: "", priority: "medium", depends_on: [] }] } })}><Plus size={13} /> Add task</Button></div>
        {project.tasks.map((task, index) => <div key={index} className="grid gap-2 rounded-lg border border-line bg-white p-3 sm:grid-cols-[1fr_120px_auto]">
          <div className="space-y-2"><input required className={field} placeholder="Task title" maxLength={240} value={task.title} onChange={(event) => { const tasks = [...project.tasks]; tasks[index] = { ...task, title: event.target.value }; setProposal({ project: { ...project, tasks } }); }} /><input className={field} placeholder="Description (optional)" maxLength={3000} value={task.description} onChange={(event) => { const tasks = [...project.tasks]; tasks[index] = { ...task, description: event.target.value }; setProposal({ project: { ...project, tasks } }); }} /></div>
          <select className={field} aria-label={`Priority for ${task.title || `task ${index + 1}`}`} value={task.priority} onChange={(event) => { const tasks = [...project.tasks]; tasks[index] = { ...task, priority: event.target.value as typeof task.priority }; setProposal({ project: { ...project, tasks } }); }}><option value="high">High priority</option><option value="medium">Medium</option><option value="low">Low</option></select>
          <Button type="button" size="sm" variant="ghost" disabled={project.tasks.length <= 1} onClick={() => { const tasks = project.tasks.filter((_, itemIndex) => itemIndex !== index).map((item) => ({ ...item, depends_on: (item.depends_on || []).filter((dependency) => dependency !== index).map((dependency) => dependency > index ? dependency - 1 : dependency) })); setProposal({ project: { ...project, tasks } }); }}>Remove</Button>
          <label className="block text-xs text-slate-500 sm:col-span-3">Must finish first
            <select multiple className={`${field} mt-1 min-h-16`} aria-label={`Prerequisites for ${task.title || `task ${index + 1}`}`} value={(task.depends_on || []).map(String)} onChange={(event) => { const tasks = [...project.tasks]; tasks[index] = { ...task, depends_on: Array.from(event.currentTarget.selectedOptions, (option) => Number(option.value)) }; setProposal({ project: { ...project, tasks } }); }}>
              {project.tasks.map((candidate, dependencyIndex) => dependencyIndex !== index && <option key={dependencyIndex} value={dependencyIndex}>{candidate.title || `Task ${dependencyIndex + 1}`}</option>)}
            </select>
            <span className="mt-1 block text-[11px] text-slate-400">Agents wait for these tasks to finish before starting this one.</span>
          </label>
        </div>)}
      </div>
      {error && <p role="alert" className="mt-3 text-sm text-red-700">{error}</p>}
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button type="button" variant="ghost" disabled={busy} onClick={() => run(() => reject(plan))}>Reject</Button>
        <Button type="submit" variant="outline" disabled={busy}>{busy ? "Saving…" : "Save edits"}</Button>
        <Button type="button" disabled={busy} onClick={() => run(() => save(plan, summary, proposal).then(() => approve(plan)))}>{busy ? "Processing…" : "Save and approve"}</Button>
      </div>
    </form>
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
  canApproveCompletion,
  approveCompletion,
  reopen,
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
      {project.status === "review" && (
        <div role="status" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950">
          <span>All tasks are complete. Review the results before approving this project as complete.</span>
          <div className="flex gap-2">
            {canManage && <Button size="sm" variant="outline" onClick={reopen}>Reopen work</Button>}
            {canApproveCompletion && <Button size="sm" onClick={approveCompletion}>Approve completion</Button>}
          </div>
        </div>
      )}
      {project.status === "completed" && (
        <div role="status" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-950">
          <span>Project completion approved.</span>
          {canManage && <Button size="sm" variant="outline" onClick={reopen}>Reopen project</Button>}
        </div>
      )}
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
              toggle={() => update(t, { status: t.status === "done" || t.status === "blocked" ? "todo" : "done" })}
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
  const flow = states.filter((status) => status !== "blocked");
  const move = (t: Task, step: number) => {
    const i = flow.indexOf(t.status) + step;
    if (t.status !== "blocked" && i >= 0 && i < flow.length) update(t, { status: flow[i] });
  };
  return (
    // Tracks never shrink below a readable card width; when six don't fit beside the
    // sidebar the board scrolls sideways instead of overlapping its columns.
    <div className="grid gap-3 overflow-x-auto pb-3 lg:grid-cols-[repeat(6,minmax(14rem,1fr))]">
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
            if (task && task.status !== s && !(task.status === "blocked" && s === "done")) update(task, { status: s });
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
                      <span className="flex items-center gap-1.5">
                        <Badge tone={t.priority === "high" ? "red" : t.priority === "medium" ? "amber" : "default"}>
                          {t.priority}
                        </Badge>
                        {t.status === "blocked" ? <Badge tone="red">Blocked</Badge> : t.depends_on?.some((dependency) => dependency.status !== "done") && <Badge tone="amber">Waiting</Badge>}
                      </span>
                      <span className="flex">
                        <button
                          aria-label={"Move back " + t.title}
                          disabled={t.status === "blocked" || t.status === flow[0]}
                          className="rounded p-1 text-slate-400 transition hover:bg-blue-50 hover:text-blue-700 disabled:opacity-30"
                          onClick={() => move(t, -1)}
                        >
                          <ChevronLeft size={16} />
                        </button>
                        <button
                          aria-label={"Advance " + t.title}
                          disabled={t.status === "blocked" || t.status === flow[flow.length - 1]}
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
                    {t.status === "blocked" && t.blocked_reason && <p className="mt-1 line-clamp-2 text-xs text-rose-700">{t.blocked_reason}</p>}
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
                      <span className="flex min-w-0 items-center gap-1.5" title={t.assignee?.name || "Unassigned"}>
                        <span className="max-w-28 truncate text-[11px] text-slate-500">{t.assignee?.name || "Unassigned"}</span>
                        <Avatar user={t.assignee} />
                      </span>
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
