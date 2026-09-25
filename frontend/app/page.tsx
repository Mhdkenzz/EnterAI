"use client";
import { useEffect, useRef, useState } from "react";
import { Bell, Menu, Plus, Search, X } from "lucide-react";
import { api, ApiError, upload } from "@/lib/api";
import { AdminConsole } from "@/components/admin-console";
import { PrivacySettings } from "@/components/privacy-settings";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AuthScreens } from "@/components/workspace/auth-screens";
import { Sidebar } from "@/components/workspace/sidebar";
import { Onboarding, VerifyBanner } from "@/components/workspace/onboarding";
import { Home, Portfolio, Work, InboxView, Teams, ProjectView } from "@/components/workspace/views";
import { AI, Command } from "@/components/workspace/assistant";
import { ProjectForm, ProjectEditForm, TaskForm, TeamForm, Drawer } from "@/components/workspace/forms";
import { Agents, AgentInspector } from "@/components/workspace/agents";
import type {
  User,
  Doc,
  Project,
  Task,
  InboxNotice,
  Action,
  Agent,
  HierarchyConfigT,
  AgentMsg,
} from "@/lib/types";

export default function EnterAI() {
  // "checking" until /me answers, so a signed-in reload never flashes the login screen.
  const [session, setSession] = useState<"checking" | "in" | "out">("checking"),
    [user, setUser] = useState<User | null>(null),
    [view, setView] = useState("Home"),
    [collapsed, setCollapsed] = useState(false),
    [mobile, setMobile] = useState(false),
    [dashboard, setDashboard] = useState<any>(null),
    [projects, setProjects] = useState<Project[]>([]),
    [users, setUsers] = useState<User[]>([]),
    [teams, setTeams] = useState<any[]>([]),
    [notices, setNotices] = useState<InboxNotice[]>([]),
    [activity, setActivity] = useState<any[]>([]),
    [project, setProject] = useState<Project | null>(null),
    [tasks, setTasks] = useState<Task[]>([]),
    [task, setTask] = useState<Task | null>(null),
    [comments, setComments] = useState<any[]>([]),
    [command, setCommand] = useState(false),
    [projectForm, setProjectForm] = useState(false),
    [editProject, setEditProject] = useState<Project | null>(null),
    [taskForm, setTaskForm] = useState(false),
    [teamForm, setTeamForm] = useState(false),
    [selectedTeam, setSelectedTeam] = useState<any>(null),
    [teamProjects, setTeamProjects] = useState<Project[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [scopedProject, setScopedProject] = useState<Project | null>(null);
  const [onboarding, setOnboarding] = useState<any>(null);
  const [agents, setAgents] = useState<Agent[]>([]),
    [hierarchyConfig, setHierarchyConfig] = useState<HierarchyConfigT | null>(null),
    [agentInspect, setAgentInspect] = useState<Agent | null>(null),
    [agentMessages, setAgentMessages] = useState<AgentMsg[]>([]),
    [agentChatBusy, setAgentChatBusy] = useState(false),
    [agentChatError, setAgentChatError] = useState("");
  async function refresh() {
    try {
      const [me, d, p, u, t, n, a] = await Promise.all([
        api<any>("/me"),
        api<any>("/dashboard"),
        api<Project[]>("/projects"),
        api<User[]>("/users"),
        api<any[]>("/teams"),
        api<InboxNotice[]>("/notifications"),
        api<any[]>("/activity"),
      ]);
      setUser(me.user);
      setDashboard(d);
      setProjects(p);
      setUsers(u);
      setTeams(t);
      setNotices(n);
      setActivity(a);
      setSession("in");
      loadOnboarding();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) setSession("out");
      else setError(e.message);
    }
  }
  async function loadOnboarding() {
    try {
      setOnboarding(await api<any>("/onboarding"));
    } catch {
      /* onboarding is advisory; never block the workspace on it */
    }
  }
  async function sendInvite(email: string, role: string) {
    await api("/auth/invites", { method: "POST", body: JSON.stringify({ email, role }) });
  }
  async function completeOnboarding() {
    await api("/onboarding/complete", { method: "POST" });
  }
  async function resendVerification() {
    if (user?.email)
      await api("/auth/resend-verification", { method: "POST", body: JSON.stringify({ email: user.email }) });
  }
  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {
    const fn = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        setCommand(true);
      }
    };
    window.addEventListener("keydown", fn);
    return () => window.removeEventListener("keydown", fn);
  }, []);
  async function login(email: string, password: string) {
    setBusy(true);
    setError("");
    try {
      await api("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function openProject(p: Project) {
    try {
      setProject(p);
      setView("Project");
      setMobile(false);
      setTasks(await api<Task[]>("/projects/" + p.id + "/tasks"));
    } catch (e: any) {
      setError(e.message);
    }
  }
  async function openTask(t: Task) {
    try {
      setTask(t);
      setComments(await api<any[]>("/tasks/" + t.id + "/comments"));
    } catch (e: any) {
      setError(e.message);
    }
  }
  async function changeTask(t: Task, values: Record<string, unknown>) {
    try {
      const next = await api<Task>("/tasks/" + t.id, { method: "PATCH", body: JSON.stringify(values) });
      setTasks((all) => all.map((x) => (x.id === t.id ? next : x)));
      setDashboard((d: any) =>
        d ? { ...d, my_tasks: (d.my_tasks || []).map((x: Task) => (x.id === t.id ? next : x)) } : d,
      );
      setTask((x) => (x?.id === t.id ? next : x));
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }
  async function openNotice(n: InboxNotice) {
    try {
      await api("/notifications/" + n.id + "/read", { method: "PATCH" });
      setNotices((all) => all.map((x) => (x.id === n.id ? { ...x, read: true } : x)));
      const id = n.href?.startsWith("project:") ? n.href.slice(8) : n.href?.match(/projects\/([^/]+)/)?.[1];
      const p = projects.find((x) => x.id === id);
      if (p) await openProject(p);
      else if (n.href) nav("Portfolio");
    } catch (e: any) {
      setError(e.message);
    }
  }
  const nav = (next: string) => {
    setView(next);
    setMobile(false);
    if (next === "Agents") loadAgents();
  };
  const openAI = (scope?: Project | null) => {
    setScopedProject(scope || null);
    setView("AI");
    setMobile(false);
  };
  async function loadAgents() {
    try {
      const [a, cfg] = await Promise.all([
        api<Agent[]>("/agents"),
        api<HierarchyConfigT>("/hierarchy-config"),
      ]);
      setAgents(a);
      setHierarchyConfig(cfg);
    } catch (e: any) {
      setError(e.message);
    }
  }
  async function saveHierarchyConfig(values: Partial<HierarchyConfigT>) {
    const next = await api<HierarchyConfigT>("/hierarchy-config", {
      method: "PATCH",
      body: JSON.stringify(values),
    });
    setHierarchyConfig(next);
    setAgents(await api<Agent[]>("/agents"));
  }
  // Loading a transcript is slower than typing into it. Both the load and a send
  // claim this token, so a history response that arrives after the reader has
  // already sent something is dropped instead of overwriting their message.
  const transcript = useRef(0);
  async function inspectAgent(a: Agent) {
    const claim = ++transcript.current;
    setAgentInspect(a);
    setAgentMessages([]);
    setAgentChatError("");
    try {
      const history = await api<AgentMsg[]>("/agents/" + a.id + "/messages");
      if (transcript.current === claim) setAgentMessages(history);
    } catch (e: any) {
      if (transcript.current === claim) setAgentChatError(e.message);
    }
  }
  async function sendAgentMessage(agentId: string, text: string) {
    transcript.current++;
    setAgentChatBusy(true);
    setAgentChatError("");
    setAgentMessages((x) => [...x, { id: "tmp-u-" + Date.now(), role: "user", body: text }]);
    try {
      const r = await api<any>("/agents/" + agentId + "/messages", {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      setAgentMessages((x) => [
        ...x,
        { id: "tmp-a-" + Date.now(), role: "agent", body: r.reply, actions: r.actions },
      ]);
    } catch (e: any) {
      setAgentChatError(e.message || "The agent could not respond right now.");
    } finally {
      setAgentChatBusy(false);
    }
  }
  async function confirmAgentAction(a: Action) {
    try {
      await api("/ai/confirm", {
        method: "POST",
        body: JSON.stringify({ confirmation_token: a.confirmation_token }),
      });
      setAgentMessages((x) => [...x, { id: "tmp-c-" + Date.now(), role: "agent", body: "Done. " + a.label }]);
      const fresh = await api<Agent[]>("/agents");
      setAgents(fresh);
      setAgentInspect((current) => (current ? fresh.find((x) => x.id === current.id) || current : current));
    } catch (e: any) {
      setAgentChatError(e.message || "Confirmation failed. Please ask for a fresh proposal.");
    }
  }
  if (session === "checking") return <div className="min-h-screen bg-canvas" />;
  if (session === "out") return <AuthScreens login={login} busy={busy} error={error} signedIn={refresh} />;
  const main =
    view === "Admin" ? (
      user?.role === "admin" && user?.kind === "human" ? (
        <AdminConsole currentUserId={user.id} />
      ) : (
        <p role="alert">Human administrator access required.</p>
      )
    ) : view === "Settings" ? (
      <PrivacySettings userId={user?.id || ""} />
    ) : view === "Home" ? (
      <Home
        data={dashboard}
        activity={activity}
        project={openProject}
        task={openTask}
        toggle={(t: Task) => changeTask(t, { status: t.status === "done" ? "todo" : "done" })}
        all={() => nav("My work")}
        ai={() => openAI()}
      />
    ) : view === "Portfolio" ? (
      <Portfolio projects={projects} open={openProject} create={() => setProjectForm(true)} />
    ) : view === "My work" ? (
      <Work
        tasks={dashboard?.my_tasks || []}
        open={openTask}
        toggle={(t: Task) => changeTask(t, { status: t.status === "done" ? "todo" : "done" })}
      />
    ) : view === "Inbox" ? (
      <InboxView notices={notices} open={openNotice} />
    ) : view === "Teams" ? (
      <Teams
        teams={teams}
        selected={selectedTeam}
        projects={teamProjects}
        open={openProject}
        create={() => setTeamForm(true)}
        select={async (t: any) => {
          setSelectedTeam(t);
          setTeamProjects(await api<Project[]>("/teams/" + t.id + "/projects"));
        }}
      />
    ) : view === "Agents" ? (
      <Agents
        agents={agents}
        config={hierarchyConfig}
        save={saveHierarchyConfig}
        isAdmin={user?.role === "admin"}
        inspect={inspectAgent}
      />
    ) : view === "AI" ? (
      <AI
        project={scopedProject}
        clear={() => setScopedProject(null)}
        onAction={async (a: Action) => {
          await api("/ai/confirm", {
            method: "POST",
            body: JSON.stringify({ confirmation_token: a.confirmation_token }),
          });
          await refresh();
        }}
      />
    ) : (
      <ProjectView
        project={project}
        tasks={tasks}
        open={openTask}
        update={changeTask}
        edit={() => setEditProject(project)}
        create={() => setTaskForm(true)}
        ai={() => openAI(project)}
        upload={async (f: File) => {
          if (!project) return;
          try {
            const d = await upload<Doc>("/projects/" + project.id + "/documents", f);
            const next = { ...project, documents: [...(project.documents || []), d] };
            setProject(next);
            setProjects((all) => all.map((x) => (x.id === next.id ? next : x)));
          } catch (e: any) {
            setError(e.message);
          }
        }}
      />
    );
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <Sidebar
        collapsed={collapsed}
        mobile={mobile}
        view={view}
        user={user}
        notices={notices}
        toggle={() => setCollapsed(!collapsed)}
        close={() => setMobile(false)}
        nav={nav}
        ai={() => openAI()}
      />
      {mobile && (
        <button
          aria-label="Close menu"
          className="fixed inset-0 z-10 bg-black/60 lg:hidden"
          onClick={() => setMobile(false)}
        />
      )}
      <main className={cn("min-h-screen lg:pl-64", collapsed && "lg:pl-[72px]")}>
        <header className="sticky top-0 z-10 flex h-16 items-center gap-2 border-b border-line bg-canvas/95 px-4 backdrop-blur sm:px-6">
          <button
            aria-label="Open menu"
            className="rounded-lg p-2 text-zinc-400 lg:hidden"
            onClick={() => setMobile(true)}
          >
            <Menu size={19} />
          </button>
          <button
            aria-label="Search or ask Enter AI"
            onClick={() => setCommand(true)}
            className="flex h-9 min-w-0 max-w-xl flex-1 items-center gap-2 rounded-lg border border-line bg-panel px-3 text-left text-sm text-zinc-500"
          >
            <Search size={15} />
            <span className="flex-1 truncate">Search or ask Enter AI</span>
            <kbd className="hidden rounded border border-line px-1.5 py-0.5 text-[10px] sm:block">Ctrl K</kbd>
          </button>
          <button
            aria-label="Open inbox"
            className="relative rounded-lg p-2 text-zinc-400"
            onClick={() => nav("Inbox")}
          >
            <Bell size={18} />
            {notices.some((x) => !x.read) && (
              <i className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-sky-400" />
            )}
          </button>
          <Button aria-label="New project" size="sm" onClick={() => setProjectForm(true)}>
            <Plus size={15} />
            <span className="hidden sm:inline">New project</span>
          </Button>
        </header>
        <div className="grid-bg mx-auto max-w-[1600px] p-4 sm:p-6">
          {error && (
            <div
              role="alert"
              className="mb-4 flex justify-between rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200"
            >
              {error}
              <button onClick={() => setError("")}>
                <X size={16} />
              </button>
            </div>
          )}
          <VerifyBanner user={user} resend={resendVerification} />
          {view === "Home" && (
            <Onboarding
              state={onboarding}
              reload={loadOnboarding}
              invite={sendInvite}
              done={completeOnboarding}
            />
          )}
          {main}
        </div>
      </main>
      {command && (
        <Command
          projects={projects}
          close={() => setCommand(false)}
          open={(p: Project) => {
            setCommand(false);
            openProject(p);
          }}
          ai={() => {
            setCommand(false);
            openAI();
          }}
        />
      )}
      {projectForm && (
        <ProjectForm
          teams={teams}
          close={() => setProjectForm(false)}
          create={async (data: any) => {
            const p = await api<Project>("/projects", { method: "POST", body: JSON.stringify(data) });
            setProjects((x) => [p, ...x]);
            setProjectForm(false);
            await openProject(p);
          }}
        />
      )}
      {editProject && (
        <ProjectEditForm
          project={editProject}
          teams={teams}
          close={() => setEditProject(null)}
          save={async (data: any) => {
            const next = await api<Project>("/projects/" + editProject.id, {
              method: "PATCH",
              body: JSON.stringify(data),
            });
            setProject(next);
            setProjects((all) => all.map((x) => (x.id === next.id ? next : x)));
            setEditProject(null);
            await refresh();
          }}
        />
      )}
      {taskForm && project && (
        <TaskForm
          project={project}
          users={users}
          close={() => setTaskForm(false)}
          create={async (data: any) => {
            const t = await api<Task>("/tasks", {
              method: "POST",
              body: JSON.stringify({ ...data, project_id: project.id }),
            });
            setTasks((x) => [...x, t]);
            setTaskForm(false);
            await refresh();
          }}
        />
      )}
      {teamForm && (
        <TeamForm
          close={() => setTeamForm(false)}
          create={async (data: any) => {
            await api("/teams", { method: "POST", body: JSON.stringify(data) });
            setTeamForm(false);
            await refresh();
          }}
        />
      )}
      {task && (
        <Drawer
          task={task}
          comments={comments}
          close={() => setTask(null)}
          update={changeTask}
          comment={async (body: string) => {
            const c = await api<any>("/tasks/" + task.id + "/comments", {
              method: "POST",
              body: JSON.stringify({ body }),
            });
            setComments((x) => [...x, { ...c, author: user }]);
          }}
        />
      )}
      {agentInspect && (
        <AgentInspector
          agent={agents.find((x) => x.id === agentInspect.id) || agentInspect}
          reports={agents.filter((x) => x.parent_agent_id === agentInspect.id)}
          messages={agentMessages}
          busy={agentChatBusy}
          error={agentChatError}
          close={() => setAgentInspect(null)}
          send={(t: string) => sendAgentMessage(agentInspect.id, t)}
          confirm={confirmAgentAction}
        />
      )}
    </div>
  );
}
