"use client";
import { useState } from "react";
import { Paperclip, Plus, Send, X } from "lucide-react";
import { api, upload } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { states, Avatar, Modal, field } from "@/components/workspace/shared";
import type { User } from "@/lib/types";

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
  async function read(file?: File) {
    if (!file) return;
    setBusy(true);
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
            create({
              name: name.trim(),
              code: code.trim(),
              description,
              team_id: team || null,
              health,
              color: "#38bdf8",
              source_document_ids: doc ? [doc] : [],
            });
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
          <Button type="submit" disabled={busy}>
            <Plus size={15} />
            Create project
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
  return (
    <Modal title="Edit project" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save({
            name: name.trim(),
            code: code.trim().toUpperCase(),
            description,
            health,
            team_id: team || null,
          });
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
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit">Save changes</Button>
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
  return (
    <Modal title={"New task in " + project.name} close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (title) create({ title, description, priority, assignee_id: assignee || null });
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
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit">
            <Plus size={15} />
            Create task
          </Button>
        </div>
      </form>
    </Modal>
  );
}
export function TeamForm({ close, create }: any) {
  const [name, setName] = useState(""),
    [description, setDescription] = useState("");
  return (
    <Modal title="New team" close={close}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name) create({ name, description });
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
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={close}>
            Cancel
          </Button>
          <Button type="submit">Create team</Button>
        </div>
      </form>
    </Modal>
  );
}
export function Drawer({ task, comments, close, update, comment }: any) {
  const [body, setBody] = useState(""),
    [uploading, setUploading] = useState(false),
    [uploaded, setUploaded] = useState("");
  async function attach(file?: File) {
    if (!file) return;
    setUploading(true);
    try {
      const r = await upload<any>("/tasks/" + task.id + "/attachments", file);
      setUploaded(r.file_name);
    } finally {
      setUploading(false);
    }
  }
  return (
    <div className="fixed inset-0 z-30 bg-black/45" onMouseDown={close}>
      <aside
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
          defaultValue={task.title}
          onBlur={(e) => e.target.value !== task.title && update(task, { title: e.target.value })}
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
        </div>
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
              if (body) {
                comment(body);
                setBody("");
              }
            }}
          >
            <input
              className="min-w-0 flex-1 rounded-lg border border-line bg-canvas p-2.5 text-sm outline-none focus:border-accent"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="Write a comment"
            />
            <Button size="sm">
              <Send size={14} />
            </Button>
          </form>
        </div>
        <div className="mt-5 flex flex-wrap gap-2">
          <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-lg border border-line bg-panel px-3 text-xs">
            <Paperclip size={14} />
            {uploading ? "Uploading..." : "Attach file"}
            <input type="file" className="hidden" onChange={(e) => attach(e.target.files?.[0])} />
          </label>
          {uploaded && <span className="self-center text-xs text-emerald-300">Uploaded {uploaded}</span>}
          <Button
            variant="danger"
            size="sm"
            onClick={async () => {
              await api("/tasks/" + task.id, { method: "DELETE" });
              close();
            }}
          >
            Delete
          </Button>
        </div>
      </aside>
    </div>
  );
}
