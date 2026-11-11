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

type NorthStarDraft = { goal: string; metric: string; unit: string; baseline: string; current: string; target: string };
function projectNorthStarPayload(value: NorthStarDraft) {
  const active = !!(value.goal.trim() || value.metric.trim() || value.target.trim());
  if (!active) return { north_star_goal: null, success_metric: null, metric_unit: null, baseline_value: null, current_value: null, target_value: null };
  if (!value.goal.trim() || !value.metric.trim() || !value.unit.trim() || !value.baseline.trim() || !value.current.trim() || !value.target.trim())
    throw new Error("Complete the goal, metric, unit, baseline, current value, and target to set a measurable north star.");
  const baseline_value = Number(value.baseline), current_value = Number(value.current), target_value = Number(value.target);
  if (![baseline_value, current_value, target_value].every(Number.isFinite) || baseline_value === target_value)
    throw new Error("Enter finite metric values and make the target different from the baseline.");
  return { north_star_goal: value.goal.trim(), success_metric: value.metric.trim(), metric_unit: value.unit.trim(), baseline_value, current_value, target_value };
}

function NorthStarFields({ value, onChange }: { value: NorthStarDraft; onChange: (value: NorthStarDraft) => void }) {
  const set = (key: keyof NorthStarDraft, next: string) => onChange({ ...value, [key]: next });
  return <section aria-label="Project north-star goal" className="rounded-xl border border-blue-200 bg-blue-50/40 p-4">
    <h3 className="text-sm font-semibold text-ink">North-star goal and success measure</h3>
    <p className="mt-1 text-xs text-slate-500">State the project outcome and how the team will measure it. Values can be updated as results change.</p>
    <label className="mt-3 block text-xs text-slate-600">Outcome
      <textarea aria-label="North-star outcome" className={cn(field, "mt-1 min-h-16")} value={value.goal} onChange={(event) => set("goal", event.target.value)} placeholder="What measurable outcome should this project achieve?" />
    </label>
    <div className="mt-3 grid gap-3 sm:grid-cols-2">
      <label className="block text-xs text-slate-600">Success metric
        <input aria-label="Success metric" className={cn(field, "mt-1")} value={value.metric} onChange={(event) => set("metric", event.target.value)} placeholder="e.g. weekly active users" />
      </label>
      <label className="block text-xs text-slate-600">Unit
        <input aria-label="Metric unit" className={cn(field, "mt-1")} value={value.unit} onChange={(event) => set("unit", event.target.value)} placeholder="users, %, hours…" />
      </label>
    </div>
    <div className="mt-3 grid grid-cols-3 gap-2">
      {(["baseline", "current", "target"] as const).map((key) => <label key={key} className="block text-xs capitalize text-slate-600">{key} value
        <input aria-label={`Metric ${key} value`} type="number" step="any" className={cn(field, "mt-1")} value={value[key]} onChange={(event) => set(key, event.target.value)} />
      </label>)}
    </div>
  </section>;
}

export function ProjectForm({ teams, close, create }: any) {
  const [name, setName] = useState(""),
    [code, setCode] = useState(""),
    [description, setDescription] = useState(""),
    [freezeRule, setFreezeRule] = useState(""),
    [team, setTeam] = useState(""),
    [health, setHealth] = useState("on_track"),
    [doc, setDoc] = useState(""),
    [draft, setDraft] = useState<any>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [northStar, setNorthStar] = useState({ goal: "", metric: "", unit: "count", baseline: "0", current: "0", target: "" });
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
          const form = new FormData(e.currentTarget);
          if (name.trim() && code.trim())
            run(() =>
              create({
                name: name.trim(),
                code: code.trim(),
                description,
                launch_date: String(form.get("launch_date") || "") || null,
                launch_freeze_rule: String(form.get("launch_freeze_rule") || "").trim() || null,
                team_id: team || null,
                health,
                color: "#38bdf8",
                ...projectNorthStarPayload(northStar),
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
          <label className="block text-xs text-zinc-400">
            Launch date
            <input aria-label="Launch date" name="launch_date" type="date" className={field} />
          </label>
          <label className="block text-xs text-zinc-400 sm:col-span-2">
            Launch freeze rule
            <textarea aria-label="Launch freeze rule" name="launch_freeze_rule" maxLength={2000} className={cn(field, "min-h-20")} placeholder="Example: Freeze code changes 48 hours before launch; only approved fixes may ship." value={freezeRule} onChange={(e) => setFreezeRule(e.target.value)} />
          </label>
        </div>
        <NorthStarFields value={northStar} onChange={setNorthStar} />
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
    [freezeRule, setFreezeRule] = useState(project.launch_freeze_rule || ""),
    [health, setHealth] = useState(project.health),
    [team, setTeam] = useState(teams.find((t: any) => t.name === project.team)?.id || "");
  const [northStar, setNorthStar] = useState({
    goal: project.north_star?.goal || "", metric: project.north_star?.metric || "", unit: project.north_star?.unit || "count",
    baseline: project.north_star?.baseline == null ? "0" : String(project.north_star.baseline),
    current: project.north_star?.current == null ? "0" : String(project.north_star.current),
    target: project.north_star?.target == null ? "" : String(project.north_star.target),
  });
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title="Edit project" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          const form = new FormData(e.currentTarget);
          run(() =>
            save({
              name: name.trim(),
              code: code.trim().toUpperCase(),
              description,
              launch_date: String(form.get("launch_date") || "") || null,
              launch_freeze_rule: String(form.get("launch_freeze_rule") || "").trim() || null,
              health,
              team_id: team || null,
              ...projectNorthStarPayload(northStar),
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
          <label className="block text-xs text-zinc-400">
            Launch date
            <input aria-label="Launch date" name="launch_date" type="date" className={field} defaultValue={project.launch_date || ""} />
          </label>
          <label className="block text-xs text-zinc-400 sm:col-span-2">
            Launch freeze rule
            <textarea aria-label="Launch freeze rule" name="launch_freeze_rule" maxLength={2000} className={cn(field, "min-h-20")} placeholder="Example: Freeze code changes 48 hours before launch; only approved fixes may ship." value={freezeRule} onChange={(e) => setFreezeRule(e.target.value)} />
          </label>
        </div>
        <NorthStarFields value={northStar} onChange={setNorthStar} />
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
    [milestoneId, setMilestoneId] = useState(""),
    [assignee, setAssignee] = useState(currentUserId || ""),
    [assigneeSearch, setAssigneeSearch] = useState(""),
    [qaRequired, setQaRequired] = useState(false),
    [humanApproval, setHumanApproval] = useState(false),
    [qaDetailsOpen, setQaDetailsOpen] = useState(false),
    [criteriaText, setCriteriaText] = useState(""),
    [rubricText, setRubricText] = useState("Meets the acceptance criteria\nEvidence is accurate and traceable\nNo unresolved blockers or hidden issues");
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title={"New task in " + project.name} close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          const form = new FormData(e.currentTarget);
          if (!agentsLoading && title.trim())
            run(() => create({
              title: title.trim(), description, priority, assignee_id: assignee || null,
              milestone_id: milestoneId || null,
              due_date: String(form.get("due_date") || "") ? `${String(form.get("due_date"))}T00:00:00` : null,
              qa_required: qaRequired || humanApproval,
              requires_human_approval: humanApproval,
              acceptance_criteria: criteriaText.split("\n").map((item) => item.trim()).filter(Boolean),
              review_rubric: rubricText.split("\n").map((item) => item.trim()).filter(Boolean),
            }));
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
          <div className="text-xs text-zinc-400">
            <label htmlFor="new-task-priority">Priority</label>
            <select id="new-task-priority" className={cn(field, "task-select")} data-tone={`priority-${priority}`} value={priority} onChange={(e) => { setPriority(e.target.value); if (e.target.value === "high") { setQaRequired(true); setHumanApproval(true); setQaDetailsOpen(true); } }}>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </div>
          <div className="text-xs text-zinc-400">
            <label htmlFor="new-task-assignee">Assignee</label>
            {agentsLoading && <span role="status">Loading AI agents…</span>}
            <input className={field} aria-label="Search assignees" placeholder="Search people or agents" value={assigneeSearch} onChange={(e) => setAssigneeSearch(e.target.value)} />
            <select id="new-task-assignee" className={field} value={assignee} onChange={(e) => { setAssignee(e.target.value); if (agents.some((agent: Agent) => agent.id === e.target.value)) { setQaRequired(true); setQaDetailsOpen(true); } }}>
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
        {!!project.milestones?.length && <label className="block text-xs text-zinc-400">Milestone
          <select aria-label="Milestone" className={field} value={milestoneId} onChange={(event) => setMilestoneId(event.target.value)}>
            <option value="">No milestone</option>
            {project.milestones.map((milestone: any) => <option key={milestone.id} value={milestone.id}>{milestone.title}</option>)}
          </select>
        </label>}
        <label className="block text-xs text-zinc-400">
          Due date
          <input name="due_date" type="date" className={field} />
        </label>
        <details open={qaDetailsOpen} onToggle={(e) => setQaDetailsOpen(e.currentTarget.open)} className="rounded-lg border border-line p-3">
          <summary className="cursor-pointer text-sm font-semibold text-ink">Delivery QA (optional)</summary>
          <div className="mt-3 space-y-3">
            <label className="flex items-start gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={qaRequired} onChange={(e) => setQaRequired(e.target.checked)} />
              <span>Require acceptance criteria, evidence, automated checks, and human review before delivery.</span>
            </label>
            <label className="flex items-start gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={humanApproval} onChange={(e) => { setHumanApproval(e.target.checked); if (e.target.checked) setQaRequired(true); }} />
              <span>Require explicit human approval. Use for high-impact or sensitive work.</span>
            </label>
            {(qaRequired || humanApproval) && <>
              <label className="block text-xs text-slate-600">Acceptance criteria, one per line
                <textarea required className={cn(field, "mt-1 min-h-20")} value={criteriaText} onChange={(e) => setCriteriaText(e.target.value)} />
              </label>
              <label className="block text-xs text-slate-600">Review rubric, one per line
                <textarea required className={cn(field, "mt-1 min-h-20")} value={rubricText} onChange={(e) => setRubricText(e.target.value)} />
              </label>
            </>}
          </div>
        </details>
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
  agent_submitted_for_review: "Agent submitted gated work for human review",
  task_completed: "Task marked complete",
  agent_action_proposed: "Agent requested human approval",
  agent_action_approved: "Human approved the proposed action",
  agent_action_rejected: "Human rejected the proposed action",
  agent_blocked: "Agent escalated a blocker",
  task_started: "Work started",
  task_reviewed: "Human reviewed the task",
  task_review_approved: "Human reviewed and approved the task",
  task_qa_checked: "Automated QA checks completed",
  task_peer_review_requested: "Independent agent review requested",
  task_peer_review_submitted: "Independent agent review submitted",
  task_peer_review_decided: "Human decided the peer review",
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
  canManageQA = false,
  canRecordEvidence = false,
  canManageMilestones = false,
  qaReviewers = [],
  onTaskUpdate,
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
    [accountabilityRevision, setAccountabilityRevision] = useState(0),
    [qaCriteria, setQaCriteria] = useState((task.acceptance_criteria || []).join("\n")),
    [qaRubric, setQaRubric] = useState((task.review_rubric || []).join("\n")),
    [qaEvidence, setQaEvidence] = useState((task.qa_evidence || []).join("\n")),
    [qaRequired, setQaRequired] = useState(!!task.qa_required),
    [humanApprovalRequired, setHumanApprovalRequired] = useState(!!task.requires_human_approval),
    [peerReviewerId, setPeerReviewerId] = useState(""),
    [peerDecision, setPeerDecision] = useState(""),
    [milestones, setMilestones] = useState<any[]>([]);
  const { saving, run, failure } = useSubmit();
  useEffect(() => {
    setQaCriteria((task.acceptance_criteria || []).join("\n"));
    setQaRubric((task.review_rubric || []).join("\n"));
    setQaEvidence((task.qa_evidence || []).join("\n"));
    setQaRequired(!!task.qa_required);
    setHumanApprovalRequired(!!task.requires_human_approval);
    setPeerDecision("");
  }, [task.id, task.acceptance_criteria, task.review_rubric, task.qa_evidence,
    task.qa_required, task.requires_human_approval]);
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
    let live = true;
    loadSiblings();
    api<any>("/projects/" + task.project_id).then((project) => live && setMilestones(project.milestones || [])).catch(() => live && setMilestones([]));
    return () => { live = false; };
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
  const qaItems = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);
  const qaPlanSaved = task.qa_required === (qaRequired || humanApprovalRequired)
    && task.requires_human_approval === humanApprovalRequired
    && JSON.stringify(task.acceptance_criteria || []) === JSON.stringify(qaItems(qaCriteria))
    && JSON.stringify(task.review_rubric || []) === JSON.stringify(qaItems(qaRubric))
    && JSON.stringify(task.qa_evidence || []) === JSON.stringify(qaItems(qaEvidence));
  async function saveQaPlan() {
    await run(async () => {
      const error = await update(task, {
        qa_required: qaRequired || humanApprovalRequired,
        requires_human_approval: humanApprovalRequired,
        acceptance_criteria: qaItems(qaCriteria), review_rubric: qaItems(qaRubric),
        qa_evidence: qaItems(qaEvidence),
      });
      if (error) throw new Error(error);
      await tasksChanged(task.project_id);
    });
  }
  async function saveEvidence() {
    await run(async () => {
      const error = await update(task, { qa_evidence: qaItems(qaEvidence) });
      if (error) throw new Error(error);
      await tasksChanged(task.project_id);
    });
  }
  async function runQaChecks() {
    await run(async () => {
      const next = await api<Task>(`/tasks/${task.id}/qa/run`, { method: "POST" });
      onTaskUpdate?.(next);
      await tasksChanged(task.project_id);
    });
  }
  async function requestPeerReview() {
    await run(async () => {
      const result = await api<{ task: Task; review_task: Task }>(`/tasks/${task.id}/qa/peer-review`, {
        method: "POST", body: JSON.stringify({ reviewer_agent_id: peerReviewerId }),
      });
      onTaskUpdate?.(result.task);
      await tasksChanged(task.project_id);
    });
  }
  async function decidePeerReview(outcome: "approved" | "changes_requested") {
    await run(async () => {
      const next = await api<Task>(`/tasks/${task.id}/qa/peer-review/decision`, {
        method: "POST", body: JSON.stringify({ outcome, summary: peerDecision }),
      });
      onTaskUpdate?.(next);
      await tasksChanged(task.project_id);
    });
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
        {task.status === "blocked" && task.blocked_reason && <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-800">
          <p className="font-semibold">{task.escalation_category === "strategic_decision" ? "Strategic decision needed" : task.escalation_category === "priority_conflict" ? "Priority conflict needs a human" : task.escalation_category === "dependency" ? "Dependency blocker" : task.escalation_category === "access" ? "Access needed" : task.escalation_category === "external_wait" ? "Waiting on an external step" : "Blocked"}</p>
          <p className="mt-1">{task.blocked_reason}</p>
        </div>}
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
          {!!milestones.length && (canManageMilestones ? <TaskSelect label="Milestone" value={task.milestone_id || ""}
            options={[{ value: "", label: "No milestone" }, ...milestones.map((milestone: any) => ({ value: milestone.id, label: `${milestone.title} · ${milestone.status.replace("_", " ")}` }))]}
            onSave={(milestone_id) => update(task, { milestone_id: milestone_id || null })} /> : <div className="text-xs text-slate-500"><span className="block">Milestone</span><span className="mt-2 block font-medium text-ink">{task.milestone_title || "Not linked"}</span></div>)}
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
        <section aria-label="Quality assurance before delivery" className="mt-5 rounded-xl border border-line bg-white p-4">
          <h3 className="text-sm font-semibold text-ink">Quality assurance before delivery</h3>
          <p className="mt-1 text-xs leading-5 text-slate-500">Set the acceptance criteria and review rubric, attach evidence, run consistency checks, and record an independent review before delivery.</p>
          {canManageQA ? <>
            <label className="mt-3 flex items-start gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={qaRequired} onChange={(e) => setQaRequired(e.target.checked)} />
              <span>Require QA checks and human review before this task can be completed.</span>
            </label>
            <label className="mt-2 flex items-start gap-2 text-xs text-slate-600">
              <input type="checkbox" checked={humanApprovalRequired} onChange={(e) => { setHumanApprovalRequired(e.target.checked); if (e.target.checked) setQaRequired(true); }} />
              <span>Require explicit human approval for high-impact or sensitive work.</span>
            </label>
            <label className="mt-3 block text-xs text-slate-600">Acceptance criteria, one per line
              <textarea aria-label="Acceptance criteria" className={cn(field, "mt-1 min-h-20")} value={qaCriteria} onChange={(e) => setQaCriteria(e.target.value)} placeholder="The delivered work meets the agreed scope" />
            </label>
            <label className="mt-3 block text-xs text-slate-600">Review rubric, one per line
              <textarea aria-label="Review rubric" className={cn(field, "mt-1 min-h-20")} value={qaRubric} onChange={(e) => setQaRubric(e.target.value)} placeholder="Meets each acceptance criterion" />
            </label>
          </> : <div className="mt-3 space-y-2 text-xs text-slate-600">
            <p>{task.qa_required ? "QA gate required" : "QA gate not required"}{task.requires_human_approval ? " · Human approval required" : ""}</p>
            <p><strong>Acceptance criteria:</strong> {(task.acceptance_criteria || []).join(" · ") || "Not set"}</p>
            <p><strong>Review rubric:</strong> {(task.review_rubric || []).join(" · ") || "Not set"}</p>
          </div>}
          <label className="mt-3 block text-xs text-slate-600">Delivery evidence, one item per line
            <textarea aria-label="Delivery evidence" className={cn(field, "mt-1 min-h-20")} value={qaEvidence} onChange={(e) => setQaEvidence(e.target.value)} placeholder="Test output, artifact link, decision, or validation result" />
          </label>
          <div className="mt-3 flex flex-wrap gap-2">
            {canRecordEvidence && <Button type="button" size="sm" variant="outline" disabled={saving} onClick={saveEvidence}>Save evidence</Button>}
            {canManageQA && <>
              <Button type="button" size="sm" variant="outline" disabled={saving} onClick={saveQaPlan}>Save QA plan</Button>
              <Button type="button" size="sm" disabled={saving || !(qaRequired || humanApprovalRequired) || !qaPlanSaved} onClick={runQaChecks}>Run QA checks</Button>
            </>}
          </div>
          {failure}
          {task.qa_checked_at && <div role="region" aria-label="QA check results" className="mt-3 rounded-lg border border-line bg-slate-50 p-3">
            <p className="text-xs font-semibold text-ink">Checks run {new Date(task.qa_checked_at).toLocaleString()}</p>
            <ul className="mt-2 space-y-1">
              {(task.qa_checks || []).map((check: NonNullable<Task["qa_checks"]>[number]) => <li key={check.name} className={cn("text-xs", check.passed ? "text-emerald-800" : "text-rose-800")}>
                {check.passed ? "Passed" : "Needs attention"} · {check.name.replaceAll("_", " ")} — {check.detail}
              </li>)}
            </ul>
          </div>}
          {canManageQA && task.qa_required && task.status === "review" && <div className="mt-3 rounded-lg border border-line p-3">
            <h4 className="text-xs font-semibold text-ink">Independent agent review</h4>
            <p className="mt-1 text-xs text-slate-500">A different agent receives the criteria, rubric, and submitted evidence. A human still decides whether to approve its recommendation.</p>
            <p className="mt-2 text-xs text-slate-600">Status: {(task.peer_review_status || "not_requested").replaceAll("_", " ")}{task.peer_review_agent ? ` · ${task.peer_review_agent.name}` : ""}</p>
            {task.peer_review_summary && <p className="mt-2 whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-700">Reviewer report: {task.peer_review_summary}</p>}
            {task.peer_review_status === "submitted" ? <>
              <label className="mt-2 block text-xs text-slate-600">Human decision note
                <textarea aria-label="Peer review decision note" className={cn(field, "mt-1 min-h-16")} value={peerDecision} onChange={(e) => setPeerDecision(e.target.value)} />
              </label>
              <div className="mt-2 flex flex-wrap gap-2">
                <Button type="button" size="sm" disabled={saving || peerDecision.trim().length < 8} onClick={() => decidePeerReview("approved")}>Approve peer review</Button>
                <Button type="button" size="sm" variant="outline" disabled={saving || peerDecision.trim().length < 8} onClick={() => decidePeerReview("changes_requested")}>Request changes</Button>
              </div>
            </> : task.peer_review_status !== "approved" && <div className="mt-2 flex gap-2">
              <select aria-label="Independent reviewer agent" className={cn(field, "min-w-0 flex-1")} value={peerReviewerId} onChange={(e) => setPeerReviewerId(e.target.value)}>
                <option value="">Choose a different agent</option>
                {qaReviewers.filter((agent: Agent) => agent.id !== task.assignee?.id).map((agent: Agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
              </select>
              <Button type="button" size="sm" disabled={saving || !peerReviewerId} onClick={requestPeerReview}>Request review</Button>
            </div>}
            {task.peer_review_decision && <p role="status" className="mt-2 text-xs text-slate-600">Human decision: {task.peer_review_decision}</p>}
          </div>}
          {task.requires_human_approval && task.status === "review" && <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">This work needs explicit human approval. Run passing QA checks, review the evidence, then mark the task done.</p>}
        </section>
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
