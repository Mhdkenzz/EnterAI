"use client";
import { useEffect, useId, useRef, useState } from "react";
import { Download, Paperclip, Plus, Send, X } from "lucide-react";
import { API_BASE, api, upload } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { states, Avatar, Modal, field } from "@/components/workspace/shared";
import type { AccountabilityEvent, Agent, Task, TaskAccountability, User } from "@/lib/types";

function agentLabel(agent: Agent, agents: Agent[]) {
  const parent = agents.find((item) => item.id === agent.parent_agent_id);
  return `${agent.name}${parent ? ` — reports to ${parent.name}` : ""} (${agent.id.slice(0, 8)})`;
}

/** Every save goes through this: the button is disabled while it runs (no duplicate
 *  records from a double click) and a failure is shown instead of vanishing. */
function useSubmit() {
  const [saving, setSaving] = useState(false),
    [saveError, setSaveError] = useState("");
  async function run(action: () => Promise<unknown>) {
    if (saving) return;
    setSaving(true);
    setSaveError("");
    try {
      await action();
    } catch (e: any) {
      setSaveError(e?.message || "Could not save. Please try again.");
    } finally {
      setSaving(false);
    }
  }
  const failure = saveError ? (
    <p role="alert" className="text-sm text-red-700">
      {saveError}
    </p>
  ) : null;
  return { saving, run, failure };
}

const statusLabels: Record<string, string> = {
  backlog: "Backlog", todo: "To do", in_progress: "In progress", blocked: "Blocked", review: "In review", done: "Done",
};

/** Keep a selection visible while its save is in flight, and restore it on failure. */
function TaskSelect({ label, value, tone, options, onSave, searchable = false }: {
  label: string;
  value: string;
  tone?: string;
  options: { value: string; label: string; disabled?: boolean }[];
  onSave: (value: string) => Promise<string | null>;
  searchable?: boolean;
}) {
  const id = useId();
  const [selected, setSelected] = useState(value);
  const [query, setQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { setSelected(value); }, [value]);
  return (
    <div className="text-xs text-zinc-500">
      <label htmlFor={id}>{label}</label>
      {searchable && <input className={field} aria-label={`Search ${label.toLowerCase()}s`} placeholder="Search people or agents" value={query} onChange={(event) => setQuery(event.target.value)} />}
      <select
        id={id}
        className={cn(field, "task-select")}
        data-tone={tone ? `${tone}-${selected}` : undefined}
        value={selected}
        disabled={saving}
        onChange={async (event) => {
          const next = event.target.value;
          setSelected(next);
          setSaving(true);
          setError("");
          try {
            const failure = await onSave(next);
            if (failure) { setSelected(value); setError(failure); }
          } catch {
            setSelected(value);
            setError("Could not save this change. Please try again.");
          } finally {
            setSaving(false);
          }
        }}
      >
        {options.filter((option) => !query || option.value === selected || option.label.toLowerCase().includes(query.toLowerCase())).map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}
      </select>
      {saving && <span className="mt-1 block">Saving…</span>}
      {error && <span role="alert" className="task-select-error mt-1 block">{error}</span>}
    </div>
  );
}

export function ProjectForm({ teams, close, create }: any) {
  const [name, setName] = useState(""),
    [code, setCode] = useState(""),
    [description, setDescription] = useState(""),
    [team, setTeam] = useState(""),
    [health, setHealth] = useState("on_track"),
    [doc, setDoc] = useState(""),
    [draft, setDraft] = useState<any>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const { saving, run, failure } = useSubmit();
  async function read(file?: File) {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const r = await upload<any>("/project-drafts/assist", file);
      setName(r.draft.name);
      setCode(r.draft.code);
      setDescription(r.draft.description);
      setHealth(r.draft.health);
      setDoc(r.document.id);
      setDraft(r.draft);
      const suggested = teams.find((t: any) => t.name === r.draft.suggested_team);
      if (suggested) setTeam(suggested.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="New project" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim() && code.trim())
            run(() =>
              create({
                name: name.trim(),
                code: code.trim(),
                description,
                team_id: team || null,
                health,
                color: "#38bdf8",
                source_document_ids: doc ? [doc] : [],
              }),
            );
        }}
      >
        <label className="block rounded-lg border border-dashed border-line p-3 text-sm text-zinc-400">
          {busy
            ? "Reading document..."
            : doc
              ? "Document read. Review details below."
              : "Upload a brief and Enter AI will draft the project"}
          <input
            className="mt-2 block w-full text-xs"
            type="file"
            accept=".pdf,.doc,.docx,.txt,.xlsx,.csv,.json"
            onChange={(e) => read(e.target.files?.[0])}
          />
        </label>
        {error && <p className="text-sm text-red-700">{error}</p>}
        {failure}
        {draft && (
          <div className="rounded-lg border border-sky-400/20 bg-sky-400/[.04] p-3 text-xs text-zinc-300">
            <p className="font-medium text-blue-800">AI suggestions</p>
            <p className="mt-1">
              Priority: <b>{draft.priority}</b> · Suggested team: <b>{draft.suggested_team}</b>
            </p>
            <ul className="mt-2 list-disc pl-4">
              {draft.suggested_tasks?.map((t: any) => (
                <li key={t.title}>{t.title}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-[1fr_.45fr]">
          <label className="block text-xs text-zinc-400">
            Project name
            <input required className={field} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block text-xs text-zinc-400">
            Code
            <input
              required
              className={field}
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
            />
          </label>
        </div>
        <label className="block text-xs text-zinc-400">
          Description
          <textarea
            className={cn(field, "min-h-24")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-zinc-400">
            Team
            <select className={field} value={team} onChange={(e) => setTeam(e.target.value)}>
              <option value="">No team</option>
              {teams.map((t: any) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-zinc-400">
            Health
            <select className={cn(field, "task-select")} data-tone={`health-${health}`} value={health} onChange={(e) => setHealth(e.target.value)}>
              <option value="on_track">On track</option>
              <option value="at_risk">At risk</option>
              <option value="off_track">Off track</option>
            </select>
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy || saving}>
            <Plus size={15} />
            {saving ? "Creating…" : "Create project"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
export function ProjectEditForm({ project, teams, close, save }: any) {
  const [name, setName] = useState(project.name),
    [code, setCode] = useState(project.code),
    [description, setDescription] = useState(project.description || ""),
    [health, setHealth] = useState(project.health),
    [team, setTeam] = useState(teams.find((t: any) => t.name === project.team)?.id || "");
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title="Edit project" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          run(() =>
            save({
              name: name.trim(),
              code: code.trim().toUpperCase(),
              description,
              health,
              team_id: team || null,
            }),
          );
        }}
      >
        <div className="grid gap-3 sm:grid-cols-[1fr_.45fr]">
          <label className="block text-xs text-zinc-400">
            Project name
            <input required className={field} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block text-xs text-zinc-400">
            Code
            <input
              required
              className={field}
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
            />
          </label>
        </div>
        <label className="block text-xs text-zinc-400">
          Description
          <textarea
            className={cn(field, "min-h-24")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-zinc-400">
            Team
            <select className={field} value={team} onChange={(e) => setTeam(e.target.value)}>
              <option value="">No team</option>
              {teams.map((t: any) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-zinc-400">
            Health
            <select className={cn(field, "task-select")} data-tone={`health-${health}`} value={health} onChange={(e) => setHealth(e.target.value)}>
              <option value="on_track">On track</option>
              <option value="at_risk">At risk</option>
              <option value="off_track">Off track</option>
            </select>
          </label>
        </div>
        {failure}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
export function TaskForm({ project, users, agents = [], agentsLoading = false, currentUserId, close, create }: any) {
  const [title, setTitle] = useState(""),
    [description, setDescription] = useState(""),
    [priority, setPriority] = useState("medium"),
    [assignee, setAssignee] = useState(currentUserId || ""),
    [assigneeSearch, setAssigneeSearch] = useState(""),
    [dueDate, setDueDate] = useState("");
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title={"New task in " + project.name} close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!agentsLoading && title.trim())
            run(() => create({ title: title.trim(), description, priority, assignee_id: assignee || null, due_date: dueDate ? dueDate + "T00:00:00" : null }));
        }}
      >
        <label className="block text-xs text-zinc-400">
          Title
          <input required className={field} value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label className="block text-xs text-zinc-400">
          Description
          <textarea
            className={cn(field, "min-h-20")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-zinc-400">
            Priority
            <select className={cn(field, "task-select")} data-tone={`priority-${priority}`} value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>
          <div className="text-xs text-zinc-400">
            <label htmlFor="new-task-assignee">Assignee</label>
            {agentsLoading && <span role="status">Loading AI agents…</span>}
            <input className={field} aria-label="Search assignees" placeholder="Search people or agents" value={assigneeSearch} onChange={(e) => setAssigneeSearch(e.target.value)} />
            <select id="new-task-assignee" className={field} value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              {users.filter((u: User) => u.id === assignee || u.name.toLowerCase().includes(assigneeSearch.toLowerCase())).map((u: User) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
              {agents.length > 0 && <optgroup label="AI agents">{agents.filter((agent: Agent) => agent.id === assignee || agentLabel(agent, agents).toLowerCase().includes(assigneeSearch.toLowerCase())).map((agent: Agent) => <option key={agent.id} value={agent.id}>{agentLabel(agent, agents)}</option>)}</optgroup>}
            </select>
            <span className="mt-1 block text-[11px] text-zinc-500">Every task needs an owner. New tasks are assigned to you unless you choose someone else.</span>
          </div>
        </div>
        <label className="block text-xs text-zinc-400">
          Due date
          <input type="date" className={field} value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
        </label>
        {failure}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving || agentsLoading}>
            <Plus size={15} />
            {saving ? "Creating…" : "Create task"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
export function TeamForm({ close, create }: any) {
  const [name, setName] = useState(""),
    [description, setDescription] = useState("");
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title="New team" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) run(() => create({ name: name.trim(), description }));
        }}
      >
        <label className="block text-xs text-zinc-400">
          Team name
          <input required className={field} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-xs text-zinc-400">
          Description
          <textarea
            className={cn(field, "min-h-20")}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        {failure}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving}>
            {saving ? "Creating…" : "Create team"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
type AttachmentRow = { id: string; file_name: string };
type DecisionRow = {
  id: string;
  title: string;
  body: string;
  created_at: string;
  project_id: string;
  task_id: string;
  source_comment_id: string | null;
  author?: { name: string } | null;
};

const accountabilityActionLabels: Record<string, string> = {
  created: "Task requested and created",
  created_from_inbound_plan: "Task created from an approved request",
  subtask_delegated: "Subtask assigned",
  delegated_to_agent: "Task assigned to an agent",
  task_reassigned: "Task owner changed",
  agent_claimed: "Agent claimed the task",
  agent_started: "Work started",
  agent_progress: "Progress recorded",
  agent_completed: "Agent submitted completed work",
  task_completed: "Task marked complete",
  agent_action_proposed: "Agent requested human approval",
  agent_action_approved: "Human approved the proposed action",
  agent_action_rejected: "Human rejected the proposed action",
  agent_blocked: "Agent escalated a blocker",
  task_started: "Work started",
  task_reviewed: "Human reviewed the task",
  task_review_approved: "Human reviewed and approved the task",
  updated: "Task details updated",
};

function accountabilityName(person?: TaskAccountability["owner"]) {
  if (!person) return "Not recorded";
  return `${person.name}${person.kind === "agent" ? ` · AI${person.title ? `, ${person.title}` : ""}` : " · human"}`;
}

export function Drawer({
  task,
  users,
  agents = [],
  comments,
  close,
  update,
  comment,
  deleted,
  openTask,
  tasksChanged,
}: any) {
  const [body, setBody] = useState(""),
    [uploading, setUploading] = useState(false),
    [attachments, setAttachments] = useState<AttachmentRow[]>([]),
    [attachError, setAttachError] = useState(""),
    [decisions, setDecisions] = useState<DecisionRow[]>([]),
    [decisionLoading, setDecisionLoading] = useState(false),
    [decisionSaving, setDecisionSaving] = useState<string | null>(null),
    [decisionError, setDecisionError] = useState(""),
    [accountability, setAccountability] = useState<TaskAccountability | null>(null),
    [accountabilityError, setAccountabilityError] = useState(""),
    [accountabilityRevision, setAccountabilityRevision] = useState(0);
  const { saving, run, failure } = useSubmit();
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // Subtasks and the parent come from the task's own project, whichever view opened it.
  const [siblings, setSiblings] = useState<Task[]>([]),
    [newSubtask, setNewSubtask] = useState("");
  const loadSiblings = () =>
    api<Task[]>("/projects/" + task.project_id + "/tasks")
      .then(setSiblings)
      .catch(() => {});
  useEffect(() => {
    loadSiblings();
  }, [task.id]);
  const subtasks = siblings.filter((t) => t.parent_id === task.id);
  const parent = task.parent_id ? siblings.find((t) => t.id === task.parent_id) : null;
  useEffect(() => {
    let live = true;
    setAccountability(null);
    setAccountabilityError("");
    api<TaskAccountability>(`/tasks/${task.id}/accountability`)
      .then((value) => live && setAccountability(value))
      .catch((e: any) => live && setAccountabilityError(e?.message || "Could not load task accountability."));
    return () => { live = false; };
  }, [task.id, accountabilityRevision]);
  useEffect(() => {
    let live = true;
    api<AttachmentRow[]>("/tasks/" + task.id + "/attachments")
      .then((rows) => live && setAttachments(rows))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [task.id]);
  useEffect(() => {
    let live = true;
    setDecisionLoading(true);
    setDecisionError("");
    api<DecisionRow[]>(`/tasks/${task.id}/decisions`)
      .then((rows) => live && setDecisions(rows))
      .catch((e: any) => live && setDecisionError(e?.message || "Could not load saved decisions."))
      .finally(() => live && setDecisionLoading(false));
    return () => { live = false; };
  }, [task.id]);
  async function attach(file?: File) {
    if (!file) return;
    setUploading(true);
    setAttachError("");
    try {
      const r = await upload<AttachmentRow>("/tasks/" + task.id + "/attachments", file);
      setAttachments((rows) => [...rows, r]);
    } catch (e: any) {
      setAttachError(e.message);
    } finally {
      setUploading(false);
    }
  }
  async function recordDecision(commentId: string) {
    if (decisionSaving) return;
    setDecisionSaving(commentId);
    setDecisionError("");
    try {
      const saved = await api<DecisionRow>(`/tasks/${task.id}/decisions/from-comment/${commentId}`, { method: "POST" });
      setDecisions((current) => current.some((decision) => decision.id === saved.id)
        ? current.map((decision) => decision.id === saved.id ? saved : decision)
        : [...current, saved]);
    } catch (e: any) {
      setDecisionError(e?.message || "Could not record this decision. Please try again.");
    } finally {
      setDecisionSaving(null);
    }
  }
  return (
    <div className="fixed inset-0 z-30 bg-black/45" onMouseDown={close}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Task: ${task.title}`}
        className="absolute inset-y-0 right-0 w-full max-w-xl overflow-y-auto border-l border-line bg-white p-5 text-ink shadow-float sm:p-6"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-6 flex justify-between">
          <Badge tone={task.status === "done" ? "green" : task.status === "blocked" ? "red" : "blue"}>
            {task.status === "done" ? "Completed" : task.status === "blocked" ? "Blocked" : "Task"}
          </Badge>
          <button aria-label="Close task" className="rounded-lg p-2 text-slate-500 transition hover:bg-slate-100 hover:text-ink" onClick={close}>
            <X className="text-zinc-500" />
          </button>
        </div>
        {task.status === "blocked" && task.blocked_reason && <p className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-800">{task.blocked_reason}</p>}
        <input
          aria-label="Task title"
          defaultValue={task.title}
          onBlur={(e) => {
            const next = e.target.value.trim();
            // A task needs a name: an emptied title goes back to what it was.
            if (!next) e.target.value = task.title;
            else if (next !== task.title) update(task, { title: next });
          }}
          className="w-full bg-transparent text-xl font-semibold outline-none"
        />
        <textarea
          aria-label="Task description"
          defaultValue={task.description}
          onBlur={(e) => e.target.value !== task.description && update(task, { description: e.target.value })}
          className={cn(field, "min-h-24")}
          placeholder="Add a description"
        />
        <div className="mt-5 grid grid-cols-2 gap-3">
          <TaskSelect label="Status" value={task.status} tone="status"
            options={states.map((s) => ({ value: s, label: statusLabels[s], disabled: task.status === "blocked" && s === "done" }))}
            onSave={(status) => update(task, { status })} />
          <TaskSelect label="Priority" value={task.priority} tone="priority"
            options={[{ value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" }]}
            onSave={(priority) => update(task, { priority })} />
          <TaskSelect label="Assignee" value={task.assignee?.id || ""} searchable
            options={[...(task.assignee && !(users || []).some((u: User) => u.id === task.assignee?.id) && !agents.some((a: Agent) => a.id === task.assignee?.id) ? [{ value: task.assignee.id, label: task.assignee.name }] : []), ...(users || []).map((u: User) => ({ value: u.id, label: u.name })), ...agents.map((agent: Agent) => ({ value: agent.id, label: `AI: ${agentLabel(agent, agents)}${agent.current_task ? ` · active: ${agent.current_task.title}` : " · available"}`, disabled: task.status === "in_progress" && !!agent.current_task && agent.current_task.id !== task.id }))]}
            onSave={async (assignee) => {
              const error = await update(task, { assignee_id: assignee });
              if (!error) setAccountabilityRevision((value) => value + 1);
              return error;
            }} />
          <label className="text-xs text-zinc-500">
            Due date
            <input
              type="date"
              className={field}
              value={task.due_date ? String(task.due_date).slice(0, 10) : ""}
              onChange={(e) =>
                update(task, { due_date: e.target.value ? e.target.value + "T00:00:00" : null })
              }
            />
          </label>
        </div>
        <section aria-label="Ownership and accountability" className="mt-5 rounded-xl border border-line bg-slate-50 p-4">
          <h3 className="text-sm font-semibold text-ink">Ownership & accountability</h3>
          {accountabilityError && <p role="alert" className="mt-2 text-xs text-red-700">{accountabilityError}</p>}
          {!accountability && !accountabilityError && <p role="status" className="mt-2 text-xs text-slate-500">Loading ownership history…</p>}
          {accountability && <>
            <dl className="mt-3 grid gap-x-3 gap-y-2 text-xs sm:grid-cols-2">
              <div><dt className="font-medium text-slate-500">Requested by</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.requested_by)}</dd></div>
              <div><dt className="font-medium text-slate-500">Current owner</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.owner)}</dd></div>
              <div><dt className="font-medium text-slate-500">Last assigned by</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.assigned_by)}</dd></div>
              <div><dt className="font-medium text-slate-500">Last executed by</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.executed_by)}</dd></div>
              <div><dt className="font-medium text-slate-500">Last reviewed by</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.reviewed_by)}</dd></div>
              <div><dt className="font-medium text-slate-500">Last approved by</dt><dd className="mt-0.5 text-ink">{accountabilityName(accountability.approved_by)}</dd></div>
            </dl>
            <details className="mt-3 border-t border-line pt-3">
              <summary className="cursor-pointer text-xs font-semibold text-slate-600">Ownership and work history ({accountability.events.length})</summary>
              <ol className="mt-2 space-y-2">
                {accountability.events.slice(-20).reverse().map((event: AccountabilityEvent) => <li key={event.id} className="text-xs text-slate-600">
                  <span className="font-medium text-ink">{accountabilityActionLabels[event.action] || event.action.replaceAll("_", " ")}</span>
                  {event.actor && <> · {event.actor.name}</>}
                  {event.initiator && event.initiator.id !== event.actor?.id && <> · initiated by {event.initiator.name}</>}
                  <time className="ml-1 text-slate-400" dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time>
                </li>)}
              </ol>
            </details>
          </>}
        </section>
        {task.status === "blocked" && <p className="mt-2 text-xs text-zinc-500">Reopen the task to clear its blocker before marking it done.</p>}
        <label className="mt-3 block text-xs text-zinc-500">
          Labels
          <input
            key={task.id + (task.labels || []).join(",")}
            defaultValue={(task.labels || []).join(", ")}
            placeholder="e.g. design, blocked"
            className={field}
            onBlur={(e) => {
              const labels = Array.from(
                new Set(
                  e.target.value
                    .split(",")
                    .map((l) => l.trim())
                    .filter(Boolean)
                    .map((l) => l.slice(0, 40)),
                ),
              ).slice(0, 10);
              if (labels.join(",") !== (task.labels || []).join(",")) update(task, { labels });
            }}
          />
        </label>
        <label className="mt-4 block text-xs text-zinc-500">
          Prerequisites
          <span className="mt-1 block text-[11px]">{(task.depends_on || []).length ? `Selected: ${(task.depends_on || []).map((dependency: any) => dependency.title).join(", ")}` : "No prerequisites selected."}</span>
          <select
            multiple
            className={cn(field, "mt-1 min-h-24")}
            value={(task.depends_on || []).map((dependency: any) => dependency.id)}
            onChange={(event) => {
              const depends_on = Array.from(event.currentTarget.selectedOptions, (option) => option.value);
              update(task, { depends_on });
            }}
          >
            {siblings.filter((candidate) => candidate.id !== task.id).map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.title} · {candidate.status.replace("_", " ")}
              </option>
            ))}
          </select>
          <span className="mt-1 block text-[11px] text-zinc-400">The list shows available tasks. Select those that must finish first; agents will wait for the selected tasks.</span>
          {(task.depends_on || []).some((dependency: any) => dependency.status !== "done") && (
            <span className="mt-1 block text-[11px] font-medium text-amber-700">Blocked until all prerequisites are complete.</span>
          )}
        </label>
        {parent && (
          <p className="mt-4 text-xs text-zinc-400">
            Subtask of{" "}
            <button className="text-accent hover:underline" onClick={() => openTask(parent)}>
              {parent.title}
            </button>
          </p>
        )}
        {!task.parent_id && (
          <div className="mt-5">
            <h3 className="text-sm font-medium">
              Subtasks{" "}
              {subtasks.length > 0 && (
                <span className="text-xs text-zinc-400">
                  {subtasks.filter((t) => t.status === "done").length}/{subtasks.length} done
                </span>
              )}
            </h3>
            <ul className="mt-2 space-y-1">
              {subtasks.map((t) => (
                <li key={t.id} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    aria-label={(t.status === "done" ? "Reopen " : "Complete ") + t.title}
                    checked={t.status === "done"}
                    onChange={async () => {
                      const status = t.status === "done" ? "todo" : "done";
                      // Tick immediately; the server call follows.
                      setSiblings((all) => all.map((x) => (x.id === t.id ? { ...x, status } : x)));
                      await update(t, { status });
                      await loadSiblings();
                    }}
                  />
                  <button
                    className={cn(
                      "text-left hover:underline",
                      t.status === "done" && "text-zinc-500 line-through",
                    )}
                    onClick={() => openTask(t)}
                  >
                    {t.title}
                  </button>
                </li>
              ))}
            </ul>
            <form
              className="mt-2 flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                const title = newSubtask.trim();
                if (title)
                  run(async () => {
                    await api("/tasks", {
                      method: "POST",
                      body: JSON.stringify({ project_id: task.project_id, parent_id: task.id, title }),
                    });
                    setNewSubtask("");
                    await loadSiblings();
                    tasksChanged?.(task.project_id);
                  });
              }}
            >
              <input
                aria-label="New subtask"
                className="min-w-0 flex-1 rounded-lg border border-line bg-canvas p-2 text-sm outline-none focus:border-accent"
                placeholder="Add a subtask"
                value={newSubtask}
                onChange={(e) => setNewSubtask(e.target.value)}
              />
              <Button size="sm" disabled={saving}>
                <Plus size={14} />
                Add
              </Button>
            </form>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="mt-5">
            <h3 className="text-sm font-medium">Attachments</h3>
            <ul className="mt-2 space-y-1">
              {attachments.map((a) => (
                <li key={a.id}>
                  <a
                    href={`${API_BASE}/attachments/${a.id}/download`}
                    className="inline-flex items-center gap-2 text-sm text-accent hover:underline"
                  >
                    <Download size={14} />
                    {a.file_name}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="mt-7 border-t border-line pt-5">
          <h3 className="text-sm font-medium">Comments</h3>
          <div className="mt-4 space-y-3">
            {comments.map((c: any) => {
              const decision = decisions.find((item) => item.source_comment_id === c.id);
              return (
                <div key={c.id} className="flex gap-2">
                  <Avatar user={c.author} />
                  <div className="min-w-0 flex-1 rounded-lg bg-white/[.045] p-3 text-sm">
                    <b className="mb-1 block text-xs">{c.author?.name}</b>
                    <p className="whitespace-pre-wrap">{c.body}</p>
                    {decision ? (
                      <div className="mt-3 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-950">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <a href={`/projects/${encodeURIComponent(task.project_id)}?task=${encodeURIComponent(task.id)}`} className="text-xs font-semibold text-emerald-800 underline-offset-2 hover:underline">Saved decision: {decision.title}</a>
                          <time className="text-[11px] text-emerald-800" dateTime={decision.created_at}>{new Date(decision.created_at).toLocaleString()}</time>
                        </div>
                        <p className="mt-1 whitespace-pre-wrap text-xs leading-5">{decision.body}</p>
                        <p className="mt-1 text-[11px] text-emerald-800">Recorded by {decision.author?.name || "a team member"}</p>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="mt-2 rounded text-xs font-semibold text-blue-700 underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                        disabled={decisionSaving !== null || decisionLoading}
                        aria-label={`Record ${c.author?.name || "this comment"}'s comment as a decision`}
                        onClick={() => recordDecision(c.id)}
                      >
                        {decisionSaving === c.id ? "Saving decision…" : "Record as decision"}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {decisionLoading && <p role="status" className="text-xs text-slate-500">Loading saved decisions…</p>}
            {decisionError && <p role="alert" className="text-xs text-red-700">{decisionError}</p>}
          </div>
          <form
            className="mt-5 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              const text = body.trim();
              if (text)
                run(async () => {
                  await comment(text);
                  setBody("");
                });
            }}
          >
            <input
              className="min-w-0 flex-1 rounded-lg border border-line bg-canvas p-2.5 text-sm outline-none focus:border-accent"
              aria-label="Write a comment"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="Write a comment"
            />
            <Button size="sm" aria-label="Post comment" disabled={saving}>
              <Send size={14} />
            </Button>
          </form>
          {failure}
        </div>
        <div className="mt-5 flex flex-wrap gap-2">
          <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-lg border border-line bg-panel px-3 text-xs focus-within:ring-2 focus-within:ring-accent">
            <Paperclip size={14} />
            {uploading ? "Uploading..." : "Attach file"}
            <input type="file" aria-label="Attach file" className="sr-only" onChange={(e) => attach(e.target.files?.[0])} />
          </label>
          {attachError && (
            <span role="alert" className="self-center text-xs text-red-700">
              {attachError}
            </span>
          )}
          <Button
            variant="danger"
            size="sm"
            onClick={async () => {
              if (!window.confirm(`Delete “${task.title}”? This cannot be undone.`)) return;
              try {
                await api("/tasks/" + task.id, { method: "DELETE" });
                deleted?.(task);
                close();
              } catch (e: any) {
                setAttachError(e.message);
              }
            }}
          >
            Delete
          </Button>
        </div>
      </aside>
    </div>
  );
}
