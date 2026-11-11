"use client";
import { useEffect, useRef, useState } from "react";
import { Bell, LoaderCircle, Menu, MessageCircle, Plus, Search, X } from "lucide-react";
import { api, ApiError, SESSION_EXPIRED_EVENT, upload } from "@/lib/api";
import { AdminConsole } from "@/components/admin-console";
import { PrivacySettings } from "@/components/privacy-settings";
import { Integrations } from "@/components/integrations";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { AuthScreens } from "@/components/workspace/auth-screens";
import { Sidebar } from "@/components/workspace/sidebar";
import { Onboarding, VerifyBanner } from "@/components/workspace/onboarding";
import { Home, Portfolio, Work, InboxView, RequestsView, Teams, ProjectView, AgentWorkView } from "@/components/workspace/views";
import { AI, AI_GREETING, Command } from "@/components/workspace/assistant";
import { routeToUrl, urlToRoute, type Route } from "@/lib/routes";
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
  AgentProposal,
  HierarchyConfigT,
  AgentMsg,
  InboundMessage,
  InboundPlan,
} from "@/lib/types";

export default function EnterAI() {
  // "checking" until /me answers, so a signed-in reload never flashes the login screen.
  const [session, setSession] = useState<"checking" | "in" | "out" | "unreachable">("checking"),
    [user, setUser] = useState<User | null>(null),
    [view, setView] = useState("Home"),
    [collapsed, setCollapsed] = useState(false),
    [mobile, setMobile] = useState(false),
    [dashboard, setDashboard] = useState<any>(null),
    [queueSummary, setQueueSummary] = useState<any>({ new_requests: 0, plans_awaiting_approval: 0, projects_awaiting_review: 0, unread_notifications: 0 }),
    [projects, setProjects] = useState<Project[]>([]),
    [users, setUsers] = useState<User[]>([]),
    [teams, setTeams] = useState<any[]>([]),
    [notices, setNotices] = useState<InboxNotice[]>([]),
    [inboundMessages, setInboundMessages] = useState<InboundMessage[]>([]),
    [inboundPlans, setInboundPlans] = useState<InboundPlan[]>([]),
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
  const [aiMessages, setAiMessages] = useState<any[]>([AI_GREETING]);
  const [agents, setAgents] = useState<Agent[]>([]),
    [agentsLoading, setAgentsLoading] = useState(true),
    [agentProposals, setAgentProposals] = useState<AgentProposal[]>([]),
    [hierarchyConfig, setHierarchyConfig] = useState<HierarchyConfigT | null>(null),
    [agentInspect, setAgentInspect] = useState<Agent | null>(null),
    [agentMessages, setAgentMessages] = useState<AgentMsg[]>([]),
    [agentChatBusy, setAgentChatBusy] = useState(false),
    [agentChatError, setAgentChatError] = useState(""),
    [ceoChatLoading, setCeoChatLoading] = useState(false),
    [ceoUnavailable, setCeoUnavailable] = useState(false),
    [ceoUnreadMessages, setCeoUnreadMessages] = useState(0);
  const ceoWelcomeTriggered = useRef(false);
  useEffect(() => {
    if ((!taskForm && !task) || user?.role !== "admin" || user?.kind !== "human") return;
    let active = true;
    setAgentsLoading(true);
    api<Agent[]>("/agents")
      .then((result) => { if (active) setAgents(result); })
      .catch(() => { if (active) { setAgents([]); setError("Could not load AI agents for assignment. Close the task form and try again."); } })
      .finally(() => { if (active) setAgentsLoading(false); });
    return () => { active = false; };
  }, [taskForm, task?.id, user?.role, user?.kind]);
  async function refresh() {
    try {
      const [me, d, p, u, t, n, a, incoming, plans, summary] = await Promise.all([
        api<any>("/me"),
        api<any>("/dashboard"),
        api<Project[]>("/projects"),
        api<User[]>("/users"),
        api<any[]>("/teams"),
        api<InboxNotice[]>("/notifications"),
        api<any[]>("/activity"),
        api<InboundMessage[]>("/inbound-messages"),
        api<InboundPlan[]>("/inbound-plans"),
        api<any>("/work-queue-summary"),
      ]);
      setUser(me.user);
      setDashboard(d);
      setProjects(p);
      setUsers(u);
      setTeams(t);
      setNotices(n);
      setInboundMessages(incoming);
      setInboundPlans(plans);
      setQueueSummary(summary);
      setActivity(a);
      setSession("in");
      loadOnboarding();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 401) setSession("out");
      else {
        setError(e.message);
        // First load never got an answer: say so, rather than leaving a blank page.
        setSession((current) => (current === "checking" ? "unreachable" : current));
      }
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
    // Stripe Checkout and the billing portal return here; show the admin console,
    // which reads and clears the ?billing= outcome.
    if (new URLSearchParams(window.location.search).has("billing")) setView("Admin");
    refresh();
    const expired = () => setSession("out");
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }, []);
  useEffect(() => {
    if (session !== "in") return;
    const refreshSummary = () => api<any>("/work-queue-summary").then(setQueueSummary).catch(() => {});
    const timer = window.setInterval(refreshSummary, 30000);
    return () => window.clearInterval(timer);
  }, [session]);
  useEffect(() => {
    if (session !== "in" || !user?.id) return;
    const readKey = `enterai:ceo-chat-read:${user.id}`;
    if (!window.localStorage.getItem(readKey)) window.localStorage.setItem(readKey, new Date().toISOString());
    const refreshCeoUnread = () => {
      if (agentInspect?.hierarchy_level === "ceo") {
        const now = new Date().toISOString();
        window.localStorage.setItem(readKey, now);
        setCeoUnreadMessages(0);
        return;
      }
      const since = encodeURIComponent(window.localStorage.getItem(readKey) || "");
      api<{ unread_count: number }>(`/ceo-chat/unread?since=${since}`)
        .then((result) => setCeoUnreadMessages(result.unread_count || 0))
        .catch(() => {});
    };
    refreshCeoUnread();
    const timer = window.setInterval(refreshCeoUnread, 20000);
    return () => window.clearInterval(timer);
  }, [session, user?.id, agentInspect?.hierarchy_level]);
  useEffect(() => {
    if (session !== "in" || !user?.id || ceoWelcomeTriggered.current) return;
    const welcomeKey = `enterai:ceo-chat-welcomed:${user.id}`;
    if (window.localStorage.getItem(welcomeKey)) return;
    ceoWelcomeTriggered.current = true;
    void openCeoChat().then((opened) => {
      if (opened) window.localStorage.setItem(welcomeKey, "1");
      else ceoWelcomeTriggered.current = false;
    });
  }, [session, user?.id]);
  useEffect(() => {
    if (session !== "in" || view !== "Requests") return;
    const timer = window.setInterval(() => {
      Promise.all([
        api<InboundMessage[]>("/inbound-messages"),
        api<InboundPlan[]>("/inbound-plans"),
        api<InboxNotice[]>("/notifications"),
      ]).then(([messages, plans, notificationData]) => {
        setInboundMessages(messages);
        setInboundPlans(plans);
        setNotices(notificationData);
      }).catch((cause: any) => setError(cause?.message || "Could not refresh incoming requests."));
    }, 30000);
    return () => window.clearInterval(timer);
  }, [session, view]);
  useEffect(() => {
    if (session !== "in" || view !== "Inbox") return;
    const loadNotices = () => api<InboxNotice[]>("/notifications").then(setNotices).catch(() => {});
    void loadNotices();
    const timer = window.setInterval(loadNotices, 30000);
    return () => window.clearInterval(timer);
  }, [session, view]);
  useEffect(() => {
    if (session !== "in" || !["Home", "Portfolio", "Work", "Agents", "Project"].includes(view)) return;
    const timer = window.setInterval(() => {
      if (view === "Agents") {
        void loadAgents();
      } else if (view === "Project" && project?.id) {
        api<Task[]>(`/projects/${project.id}/tasks`).then(setTasks).catch((cause: any) => setError(cause?.message || "Could not refresh project tasks."));
      } else {
        void refresh();
      }
    }, 30000);
    return () => window.clearInterval(timer);
  }, [session, view, project?.id]);
  // --- URLs -----------------------------------------------------------------
  // The address bar mirrors (view, project, open task). `routed` stays false until
  // the URL the person arrived on has been applied, so that first render cannot
  // overwrite a deep link with "/". `fromHistory` marks state that came from
  // Back/Forward, which must replace rather than push a new entry.
  const routed = useRef(false),
    fromHistory = useRef(false);
  async function applyRoute(route: Route) {
    try {
      if (route.view === "Project" && route.projectId) {
        await openProject(await api<Project>("/projects/" + route.projectId));
      } else {
        setView(route.view);
        if (route.view === "Agents") loadAgents();
        if (route.view === "Agent work" && user?.role === "admin") loadAgents();
        if (route.view === "AI") setScopedProject(null);
      }
      if (route.taskId) await openTask(await api<Task>("/tasks/" + route.taskId));
      else setTask(null);
    } catch (e: any) {
      // A link to something deleted or from another workspace: land somewhere sensible.
      setError(e.message);
      setView(route.view === "Project" ? "Portfolio" : route.view);
    }
  }
  useEffect(() => {
    if (session !== "in" || routed.current) return;
    const route = urlToRoute(window.location.pathname, window.location.search);
    if (new URLSearchParams(window.location.search).has("billing")) route.view = "Admin";
    fromHistory.current = true; // the arrival URL is already in the address bar
    applyRoute(route).finally(() => {
      routed.current = true;
      // After React has rendered what the route set, later navigation pushes again.
      setTimeout(() => (fromHistory.current = false), 0);
    });
  }, [session]);
  useEffect(() => {
    const back = () => {
      fromHistory.current = true;
      applyRoute(urlToRoute(window.location.pathname, window.location.search)).finally(() =>
        setTimeout(() => (fromHistory.current = false), 0),
      );
    };
    window.addEventListener("popstate", back);
    return () => window.removeEventListener("popstate", back);
  }, []);
  useEffect(() => {
    if (session !== "in" || !routed.current) return;
    const url = routeToUrl({
      view,
      projectId: view === "Project" ? project?.id : undefined,
      taskId: task?.id,
    });
    if (url !== window.location.pathname + window.location.search) {
      if (fromHistory.current) window.history.replaceState(null, "", url);
      else window.history.pushState(null, "", url);
    }
    fromHistory.current = false;
  }, [view, project?.id, task?.id, session]);
  // Email links land here even when someone is already signed in (signing up signs
  // you in, so the verification link usually arrives that way).
  useEffect(() => {
    if (session !== "in") return;
    const params = new URLSearchParams(window.location.search);
    const verify = params.get("verify");
    if (verify) {
      window.history.replaceState({}, "", window.location.pathname);
      api("/auth/verify-email", { method: "POST", body: JSON.stringify({ token: verify }) })
        .then(() => refresh())
        .catch((e: any) => setError(e.message));
    } else if (params.get("invite") || params.get("reset")) {
      // These belong to a different (or signed-out) person: end this session so the
      // sign-in screens can use the link, which is still in the address bar.
      api("/auth/logout", { method: "POST" })
        .catch(() => {})
        .finally(() => setSession("out"));
    }
  }, [session]);
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
      setError("");
      setTasks((all) => all.map((x) => (x.id === t.id ? next : x)));
      setDashboard((d: any) =>
        d ? { ...d, my_tasks: (d.my_tasks || []).map((x: Task) => (x.id === t.id ? next : x)) } : d,
      );
      setTask((x) => (x?.id === t.id ? next : x));
      await refresh();
      if (project?.id === next.project_id) {
        try { setProject(await api<Project>("/projects/" + project.id)); }
        catch { setError("Task saved, but the project could not be refreshed."); }
      }
      return null;
    } catch (e: any) {
      setError(e.message);
      return e.message || "Could not save this change. Please try again.";
    }
  }
  async function openNotice(n: InboxNotice) {
    try {
      await api("/notifications/" + n.id + "/read", { method: "PATCH" });
      setNotices((all) => all.map((x) => (x.id === n.id ? { ...x, read: true } : x)));
      const id = n.href?.startsWith("project:") ? n.href.slice(8) : n.href?.match(/projects\/([^/]+)/)?.[1];
      const p = projects.find((x) => x.id === id);
      if (n.href === "requests") nav("Requests");
      else if (p) await openProject(p);
      else if (n.href) nav("Portfolio");
    } catch (e: any) {
      setError(e.message);
    }
  }
  const nav = (next: string) => {
    setView(next);
    setMobile(false);
    if (next === "Agents") loadAgents();
    if (next === "Agent work" && user?.role === "admin") loadAgents();
    if (next === "Requests") loadInboundMessages();
  };
  async function loadInboundMessages() {
    try {
      const [messages, plans] = await Promise.all([
        api<InboundMessage[]>("/inbound-messages"),
        api<InboundPlan[]>("/inbound-plans"),
      ]);
      setInboundMessages(messages);
      setInboundPlans(plans);
    } catch (e: any) {
      setError(e.message || "Could not load incoming requests.");
    }
  }
  async function captureRequest(value: { source: "manual"; sender?: string; subject?: string; body: string; source_url?: string }) {
    await api("/inbound-messages", { method: "POST", body: JSON.stringify(value) });
    await loadInboundMessages();
  }
  async function setInboundStatus(message: InboundMessage, status: "new" | "ignored") {
    try {
      await api("/inbound-messages/" + message.id, { method: "PATCH", body: JSON.stringify({ status }) });
      setInboundMessages((items) => items.map((item) => item.id === message.id ? { ...item, status } : item));
    } catch (e: any) {
      setError(e.message || "Could not update this request.");
    }
  }
  async function draftInboundPlan(message: InboundMessage) {
    const plan = await api<InboundPlan>("/inbound-messages/" + message.id + "/plans", { method: "POST" });
    setInboundPlans((items) => [plan, ...items.filter((item) => item.id !== plan.id && item.inbound_message_id !== message.id)]);
  }
  async function saveInboundPlan(plan: InboundPlan, summary: string, proposal: InboundPlan["proposal"]) {
    const saved = await api<InboundPlan>("/inbound-plans/" + plan.id, {
      method: "PATCH", body: JSON.stringify({ summary, proposal }),
    });
    setInboundPlans((items) => items.map((item) => item.id === saved.id ? saved : item));
  }
  async function approveInboundPlan(plan: InboundPlan) {
    await api("/inbound-plans/" + plan.id + "/approve", { method: "POST" });
    await refresh();
  }
  async function rejectInboundPlan(plan: InboundPlan) {
    const saved = await api<InboundPlan>("/inbound-plans/" + plan.id + "/reject", { method: "POST" });
    setInboundPlans((items) => items.map((item) => item.id === saved.id ? saved : item));
  }
  const openAI = (scope?: Project | null) => {
    setScopedProject(scope || null);
    setView("AI");
    setMobile(false);
  };
  async function loadAgents() {
    try {
      const [a, cfg, proposals] = await Promise.all([
        api<Agent[]>("/agents"),
        api<HierarchyConfigT>("/hierarchy-config"),
        api<AgentProposal[]>("/agent-proposals"),
      ]);
      setAgents(a);
      setHierarchyConfig(cfg);
      setAgentProposals(proposals);
    } catch (e: any) {
      setError(e.message);
    }
  }
  async function openCeoChat(): Promise<boolean> {
    if (ceoChatLoading) return false;
    setCeoChatLoading(true);
    try {
      await api("/ceo-chat", { method: "POST" });
      const fresh = await api<Agent[]>("/agents");
      setAgents(fresh);
      const ceo = fresh.find((agent) => agent.hierarchy_level === "ceo");
      if (!ceo) {
        setCeoUnavailable(true);
        return false;
      }
      setCeoUnavailable(false);
      await inspectAgent(ceo);
      if (user?.id) {
        window.localStorage.setItem(`enterai:ceo-chat-read:${user.id}`, new Date().toISOString());
        setCeoUnreadMessages(0);
      }
      return true;
    } catch (e: any) {
      setError(e.message || "Could not check the agent hierarchy.");
      return false;
    } finally {
      setCeoChatLoading(false);
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
  async function approveAgentProposal(proposal: AgentProposal) {
    try {
      await api("/ai/confirm", { method: "POST", body: JSON.stringify({ confirmation_token: proposal.confirmation_token }) });
      await Promise.all([loadAgents(), refresh()]);
    } catch (e: any) { setError(e.message || "Could not approve this agent action."); }
  }
  async function rejectAgentProposal(proposal: AgentProposal) {
    try {
      await api(`/agent-proposals/${proposal.id}/reject`, { method: "POST" });
      await Promise.all([loadAgents(), refresh()]);
    } catch (e: any) { setError(e.message || "Could not reject this agent action."); }
  }
  if (session === "checking") return <div className="min-h-screen bg-canvas" />;
  if (session === "unreachable")
    return (
      <main className="grid min-h-screen place-items-center bg-canvas p-6 text-ink">
        <div role="alert" className="max-w-sm space-y-4 text-center">
          <h1 className="text-lg font-semibold">Can’t reach Enter AI</h1>
          <p className="text-sm text-zinc-400">Check your connection and try again. {error}</p>
          <Button
            onClick={() => {
              setError("");
              setSession("checking");
              refresh();
            }}
          >
            Try again
          </Button>
        </div>
      </main>
    );
  if (session === "out") return <AuthScreens login={login} busy={busy} error={error} signedIn={refresh} />;
  const main =
    view === "Admin" ? (
      user?.role === "admin" && user?.kind === "human" ? (
        <AdminConsole currentUserId={user.id} />
      ) : (
        <p role="alert">Human administrator access required.</p>
      )
    ) : view === "Settings" ? (
      <PrivacySettings userId={user?.id || ""} isAdmin={user?.kind === "human" && user?.role === "admin"} />
    ) : view === "Integrations" ? (
      user?.kind === "human" ? <Integrations isAdmin={user.role === "admin"} /> : <p role="alert">Human workspace access is required.</p>
    ) : view === "Home" ? (
      <Home
        data={dashboard}
        activity={activity}
        project={openProject}
        task={openTask}
        toggle={(t: Task) => changeTask(t, { status: t.status === "done" || t.status === "blocked" ? "todo" : "done" })}
        all={() => nav("My work")}
        ceoUnreadMessages={ceoUnreadMessages}
        ai={() => { void openCeoChat(); }}
      />
    ) : view === "Portfolio" ? (
      <Portfolio projects={projects} open={openProject} create={() => setProjectForm(true)} />
    ) : view === "My work" ? (
      <Work
        tasks={dashboard?.my_tasks || []}
        open={openTask}
        toggle={(t: Task) => changeTask(t, { status: t.status === "done" || t.status === "blocked" ? "todo" : "done" })}
      />
    ) : view === "Inbox" ? (
      <InboxView notices={notices} open={openNotice} />
    ) : view === "Requests" ? (
      <RequestsView messages={inboundMessages} plans={inboundPlans} create={captureRequest}
        updateStatus={setInboundStatus} draftPlan={draftInboundPlan} savePlan={saveInboundPlan}
        approvePlan={approveInboundPlan} rejectPlan={rejectInboundPlan} />
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
        proposals={agentProposals}
        approveProposal={approveAgentProposal}
        rejectProposal={rejectAgentProposal}
      />
    ) : view === "Agent work" ? (
      user?.kind === "human" ? <AgentWorkView isAdmin={user.role === "admin"} users={users} agents={agents} openTask={openTask} /> : <p role="alert">Human workspace access is required.</p>
    ) : view === "AI" ? (
      <AI
        project={scopedProject}
        messages={aiMessages}
        setMessages={setAiMessages}
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
        currentUserId={user?.id}
        tasks={tasks}
        open={openTask}
        update={changeTask}
        edit={() => setEditProject(project)}
        create={() => { setAgentsLoading(true); setTaskForm(true); }}
        ai={() => openAI(project)}
        coordinationChanged={async () => {
          if (!project) return;
          const fresh = await api<Project>("/projects/" + project.id);
          setProject(fresh);
          setProjects((all) => all.map((item) => item.id === fresh.id ? fresh : item));
          setTasks(await api<Task[]>("/projects/" + project.id + "/tasks"));
          await refresh();
        }}
        canManage={user?.kind === "human" && (user?.role === "admin" || user?.role === "manager")}
        canApproveCompletion={user?.kind === "human" && (user?.role === "admin" || user?.role === "manager" || project?.owner?.id === user.id)}
        approveCompletion={async () => {
          if (!project) return;
          try {
            const next = await api<Project>("/projects/" + project.id + "/approve-completion", { method: "POST" });
            setProject(next);
            setProjects((all) => all.map((x) => (x.id === next.id ? next : x)));
            await refresh();
          } catch (e: any) { setError(e.message); }
        }}
        reopen={async () => {
          if (!project) return;
          try {
            const next = await api<Project>("/projects/" + project.id + "/reopen", { method: "POST" });
            setProject(next);
            setProjects((all) => all.map((x) => (x.id === next.id ? next : x)));
            await refresh();
          } catch (e: any) { setError(e.message); }
        }}
        archive={async (on: boolean) => {
          if (!project) return;
          try {
            const next = await api<Project>("/projects/" + project.id, {
              method: "PATCH",
              body: JSON.stringify({ status: on ? "archived" : "active" }),
            });
            setProject(next);
            setProjects((all) => all.map((x) => (x.id === next.id ? next : x)));
          } catch (e: any) {
            setError(e.message);
          }
        }}
        remove={async () => {
          if (!project) return;
          try {
            await api("/projects/" + project.id, { method: "DELETE" });
            setProjects((all) => all.filter((x) => x.id !== project.id));
            setProject(null);
            nav("Portfolio");
            await refresh();
          } catch (e: any) {
            setError(e.message);
          }
        }}
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
        newRequests={queueSummary.new_requests}
        plansAwaitingApproval={queueSummary.plans_awaiting_approval}
        unreadNotifications={queueSummary.unread_notifications}
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
        <div className="app-content mx-auto max-w-[1600px] p-4 pb-24 sm:p-6 sm:pb-24">
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
      {ceoUnavailable && (
        <section
          aria-labelledby="ceo-setup-title"
          className="fixed bottom-[5.25rem] right-5 z-20 w-[min(22rem,calc(100vw-2.5rem))] rounded-2xl border border-line bg-white p-4 shadow-float sm:bottom-[5.75rem] sm:right-6"
        >
          <div className="flex items-start gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-blue-50 text-accent"><MessageCircle size={17} /></span>
            <div>
              <h2 id="ceo-setup-title" className="text-sm font-semibold text-ink">CEO chat isn’t set up yet</h2>
              <p className="mt-1 text-xs leading-5 text-slate-600">An administrator can create the CEO agent from the hierarchy settings.</p>
            </div>
          </div>
          <div className="mt-3 flex items-center justify-between gap-3 pl-12">
            <button className="text-xs font-semibold text-slate-500 hover:text-ink" onClick={() => setCeoUnavailable(false)}>Dismiss</button>
            <Button size="sm" onClick={() => { setCeoUnavailable(false); nav("Agents"); }}>Open agent setup</Button>
          </div>
        </section>
      )}
      <button
        type="button"
        onClick={openCeoChat}
        disabled={ceoChatLoading}
        className="fixed bottom-5 right-5 z-20 inline-flex min-h-12 items-center gap-2 rounded-full bg-accent px-4 text-sm font-semibold text-white shadow-[0_10px_28px_rgba(49,95,206,.28)] transition hover:-translate-y-0.5 hover:bg-accent-hover focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-blue-200 disabled:cursor-wait sm:bottom-6 sm:right-6"
      >
        {ceoChatLoading ? <LoaderCircle size={17} className="animate-spin" /> : <MessageCircle size={17} />}
        <span>{ceoChatLoading ? "Opening Ask Enter AI…" : "Ask Enter AI"}</span>
      </button>
      {command && (
        <Command
          projects={projects}
          close={() => setCommand(false)}
          open={async (p: any) => {
            setCommand(false);
            try {
              await openProject(await api<Project>("/projects/" + encodeURIComponent(p.project_id || p.id)));
            } catch (e: any) {
              setError(e?.message || "Could not open this project.");
            }
          }}
          openTask={async (item: any) => {
            setCommand(false);
            try {
              if (item.project_id) {
                const knownProject = projects.find((candidate) => candidate.id === item.project_id);
                await openProject(knownProject || await api<Project>("/projects/" + encodeURIComponent(item.project_id)));
              }
              await openTask(await api<Task>("/tasks/" + encodeURIComponent(item.task_id || item.id)));
            } catch (e: any) {
              setError(e?.message || "Could not open this task.");
            }
          }}
          ai={() => {
            setCommand(false);
            void openCeoChat();
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
          currentUserId={user?.id}
          agents={user?.role === "admin" && user?.kind === "human" ? agents : []}
          agentsLoading={user?.role === "admin" && user?.kind === "human" && agentsLoading}
          close={() => setTaskForm(false)}
          create={async (data: any) => {
            const t = await api<Task>("/tasks", {
              method: "POST",
              body: JSON.stringify({ ...data, project_id: project.id }),
            });
            setTasks((x) => [...x, t]);
            setTaskForm(false);
            await refresh();
            try {
              const freshProject = await api<Project>("/projects/" + project.id);
              setProject(freshProject);
              setProjects((all) => all.map((item) => item.id === freshProject.id ? freshProject : item));
            } catch {
              setError("Task created, but milestone progress could not refresh. Reload the project to see the latest totals.");
            }
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
          key={task.id}
          task={task}
          users={users}
          agents={user?.role === "admin" && user?.kind === "human" ? agents : []}
          qaReviewers={user?.kind === "human" && ["admin", "manager"].includes(user.role) ? agents : []}
          canManageQA={user?.kind === "human" && ["admin", "manager"].includes(user.role)}
          canRecordEvidence={user?.kind === "human"}
          canManageMilestones={user?.kind === "human" && (["admin", "manager"].includes(user.role) || project?.owner?.id === user.id)}
          onTaskUpdate={setTask}
          openTask={openTask}
          tasksChanged={async (projectId: string) => {
            if (project?.id === projectId) setTasks(await api<Task[]>("/projects/" + projectId + "/tasks"));
          }}
          deleted={(gone: Task) => {
            setTasks((all) => all.filter((x) => x.id !== gone.id));
            setDashboard((d: any) =>
              d ? { ...d, my_tasks: (d.my_tasks || []).filter((x: Task) => x.id !== gone.id) } : d,
            );
            refresh();
          }}
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
          parentAgent={agents.find((x) => x.id === agentInspect.parent_agent_id)}
          reports={agents.filter((x) => x.parent_agent_id === agentInspect.id)}
          messages={agentMessages}
          busy={agentChatBusy}
          error={agentChatError}
          close={() => setAgentInspect(null)}
          send={(t: string) => sendAgentMessage(agentInspect.id, t)}
          confirm={confirmAgentAction}
          connect={() => { setAgentInspect(null); nav("Integrations"); }}
        />
      )}
    </div>
  );
}
