"use client";
import { useEffect, useRef, useState } from "react";
import { Download, Paperclip, Plus, Send, X } from "lucide-react";
import { API_BASE, api, upload } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { states, Avatar, Modal, field } from "@/components/workspace/shared";
import type { Task, User } from "@/lib/types";

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
    <p role="alert" className="text-sm text-red-300">
      {saveError}
    </p>
  ) : null;
  return { saving, run, failure };
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
        {error && <p className="text-sm text-red-300">{error}</p>}
        {failure}
        {draft && (
          <div className="rounded-lg border border-sky-400/20 bg-sky-400/[.04] p-3 text-xs text-zinc-300">
            <p className="font-medium text-sky-200">AI suggestions</p>
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
            <select className={field} value={health} onChange={(e) => setHealth(e.target.value)}>
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
            <select className={field} value={health} onChange={(e) => setHealth(e.target.value)}>
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
export function TaskForm({ project, users, close, create }: any) {
  const [title, setTitle] = useState(""),
    [description, setDescription] = useState(""),
    [priority, setPriority] = useState("medium"),
    [assignee, setAssignee] = useState("");
  const { saving, run, failure } = useSubmit();
  return (
    <Modal title={"New task in " + project.name} close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (title.trim())
            run(() => create({ title: title.trim(), description, priority, assignee_id: assignee || null }));
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
            <select className={field} value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option>low</option>
              <option>medium</option>
              <option>high</option>
            </select>
          </label>
          <label className="text-xs text-zinc-400">
            Assignee
            <select className={field} value={assignee} onChange={(e) => setAssignee(e.target.value)}>
              <option value="">Unassigned</option>
              {users.map((u: User) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        {failure}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving}>
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

export function Drawer({
  task,
  users,
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
    [attachError, setAttachError] = useState("");
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
    api<AttachmentRow[]>("/tasks/" + task.id + "/attachments")
      .then((rows) => live && setAttachments(rows))
      .catch(() => {});
    return () => {
      live = false;
    };
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
  return (
    <div className="fixed inset-0 z-30 bg-black/45" onMouseDown={close}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Task: ${task.title}`}
        className="absolute inset-y-0 right-0 w-full max-w-xl overflow-y-auto border-l border-line bg-[#0e0e12] p-5 sm:p-6"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-6 flex justify-between">
          <Badge tone={task.status === "done" ? "green" : "blue"}>
            {task.status === "done" ? "Completed" : "Task"}
          </Badge>
          <button aria-label="Close task" onClick={close}>
            <X className="text-zinc-500" />
          </button>
        </div>
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
          defaultValue={task.description}
          onBlur={(e) => e.target.value !== task.description && update(task, { description: e.target.value })}
          className={cn(field, "min-h-24")}
          placeholder="Add a description"
        />
        <div className="mt-5 grid grid-cols-2 gap-3">
          <label className="text-xs text-zinc-500">
            Status
            <select
              className={field}
              value={task.status}
              onChange={(e) => update(task, { status: e.target.value })}
            >
              {states.map((s) => (
                <option key={s} value={s}>
                  {s.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-zinc-500">
            Priority
            <select
              className={field}
              value={task.priority}
              onChange={(e) => update(task, { priority: e.target.value })}
            >
              <option>low</option>
              <option>medium</option>
              <option>high</option>
            </select>
          </label>
          <label className="text-xs text-zinc-500">
            Assignee
            <select
              className={field}
              value={task.assignee?.id || ""}
              onChange={(e) => update(task, { assignee_id: e.target.value || null })}
            >
              <option value="">Unassigned</option>
              {(users || []).map((u: User) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </select>
          </label>
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
        {parent && (
          <p className="mt-4 text-xs text-zinc-400">
            Subtask of{" "}
            <button className="text-sky-300 hover:underline" onClick={() => openTask(parent)}>
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
                    className="inline-flex items-center gap-2 text-sm text-sky-300 hover:underline"
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
            {comments.map((c: any) => (
              <div key={c.id} className="flex gap-2">
                <Avatar user={c.author} />
                <div className="rounded-lg bg-white/[.045] p-3 text-sm">
                  <b className="mb-1 block text-xs">{c.author?.name}</b>
                  {c.body}
                </div>
              </div>
            ))}
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
          <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-lg border border-line bg-panel px-3 text-xs">
            <Paperclip size={14} />
            {uploading ? "Uploading..." : "Attach file"}
            <input type="file" className="hidden" onChange={(e) => attach(e.target.files?.[0])} />
          </label>
          {attachError && (
            <span role="alert" className="self-center text-xs text-red-300">
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
