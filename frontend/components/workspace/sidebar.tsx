"use client";
import {
  ChevronLeft,
  ChevronRight,
  Circle,
  FolderKanban,
  GitBranch,
  Grid2X2,
  Inbox,
  LogOut,
  PlugZap,
  Settings2,
  Shield,
  Sparkles,
  MessageSquareText,
  Users,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Avatar } from "@/components/workspace/shared";

export function Sidebar({ collapsed, mobile, view, user, notices, newRequests = 0, plansAwaitingApproval = 0, unreadNotifications = 0, ceoUnreadMessages = 0, toggle, close, nav, ai }: any) {
  const items = [
    [Grid2X2, "Home"],
    [FolderKanban, "Portfolio"],
    [Circle, "My work"],
    [Inbox, "Inbox"],
    [MessageSquareText, "Requests"],
    ...(user?.kind === "human" ? [[PlugZap, "Integrations"] as const] : []),
    [Users, "Teams"],
    [GitBranch, "Agents"],
    ...(user?.kind === "human" ? [[Shield, "Settings"] as const] : []),
    ...(user?.role === "admin" && user?.kind === "human" ? [[Settings2, "Admin"] as const] : []),
  ] as const;
  const logout = () => {
    api("/auth/logout", { method: "POST" })
      .catch(() => {})
      .finally(() => window.location.reload());
  };
  return (
    <aside
      className={cn(
        "app-sidebar fixed inset-y-0 left-0 z-20 flex w-[264px] flex-col border-r border-white/[.07] transition-[width,transform] duration-200 lg:visible lg:translate-x-0",
        mobile ? "visible translate-x-0" : "invisible -translate-x-full",
        collapsed && "lg:w-[76px]",
      )}
    >
      <div className="flex h-[76px] items-center gap-3 border-b border-white/[.07] px-5">
        <div className="relative grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-blue-500 font-black tracking-tight text-white shadow-[0_4px_12px_rgba(49,95,206,.32)]">
          E
          <i className="absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border-2 border-nav bg-cyan-300" />
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <p className="truncate text-[15px] font-bold tracking-[-.02em] text-white">Enter AI</p>
            <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-[.16em] text-slate-400">Work OS</p>
          </div>
        )}
        <button
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="ml-auto hidden rounded-md p-1.5 text-slate-400 transition hover:bg-white/10 hover:text-white lg:block"
          onClick={toggle}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
        <button aria-label="Close navigation" className="ml-auto rounded-md p-1.5 text-slate-300 lg:hidden" onClick={close}>
          <X size={17} />
        </button>
      </div>

      {!collapsed && (
        <div className="px-4 pt-5">
          <div className="flex items-center gap-3 rounded-xl border border-white/[.08] bg-white/[.045] px-3 py-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-lg bg-indigo-400/15 text-xs font-bold text-indigo-200">W</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-semibold text-slate-100">Workspace</span>
              <span className="mt-0.5 block text-[10px] text-slate-400">Team plan</span>
            </span>
            <ChevronRight size={14} className="text-slate-500" />
          </div>
        </div>
      )}

      <nav aria-label="Main navigation" className="mt-6 flex-1 overflow-y-auto px-3">
        {!collapsed && <p className="mb-2 px-3 text-[10px] font-bold uppercase tracking-[.16em] text-slate-500">Workspace</p>}
        {items.map(([Icon, label]) => {
          const active = view === label;
          const unreadCount = Number(unreadNotifications) || notices.filter((x: any) => !x.read).length;
          const unread = label === "Inbox" && unreadCount > 0;
          const requestCount = label === "Requests" ? Number(newRequests) + Number(plansAwaitingApproval) : 0;
          const accessibleLabel = label === "Requests" && requestCount
            ? `Requests, ${newRequests} new requests and ${plansAwaitingApproval} plans awaiting approval`
            : label === "Inbox" && unreadCount ? `Inbox, ${unreadCount} unread notifications` : label;
          return (
            <button
              key={label}
              aria-label={accessibleLabel}
              aria-current={active ? "page" : undefined}
              title={collapsed ? accessibleLabel : undefined}
              onClick={() => nav(label)}
              className={cn(
                "group relative mb-1 flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-[13px] font-medium text-slate-300 transition-colors hover:bg-white/[.07] hover:text-white",
                active && "bg-blue-500/20 text-white shadow-[inset_0_0_0_1px_rgba(125,165,255,.17)] hover:bg-blue-500/20",
                collapsed && "lg:justify-center lg:px-0",
              )}
            >
              {active && <i className="absolute bottom-2 left-0 top-2 w-[3px] rounded-r bg-blue-300" />}
              <Icon size={17} strokeWidth={active ? 2.2 : 1.8} className={cn(active ? "text-blue-200" : "text-slate-400 group-hover:text-slate-200")} />
              {!collapsed && <span className="flex-1 text-left">{label}</span>}
              {label === "Requests" && requestCount > 0 && !collapsed && (
                <span aria-hidden="true" className="min-w-5 rounded-full bg-blue-400/20 px-1.5 py-0.5 text-center text-[10px] font-semibold text-blue-100">
                  {requestCount > 99 ? "99+" : requestCount}
                </span>
              )}
              {label === "Requests" && requestCount > 0 && collapsed && (
                <i aria-hidden="true" className="absolute right-3 top-3 h-2 w-2 rounded-full bg-cyan-300" />
              )}
              {unread && !collapsed && (
                <span aria-hidden="true" className="min-w-5 rounded-full bg-cyan-400/20 px-1.5 py-0.5 text-center text-[10px] font-semibold text-cyan-100">
                  {unreadCount > 99 ? "99+" : unreadCount}
                </span>
              )}
              {unread && collapsed && (
                <i aria-hidden="true" className="absolute right-3 top-3 h-2 w-2 rounded-full bg-cyan-300" />
              )}
            </button>
          );
        })}
        <div className="my-5 h-px bg-white/[.08]" />
        <button
          aria-label={ceoUnreadMessages > 0 ? `Ask Enter AI, ${ceoUnreadMessages} unread messages` : "Ask Enter AI"}
          onClick={ai}
          title={collapsed ? "Ask Enter AI" : undefined}
          className={cn(
            "relative flex min-h-11 w-full items-center gap-3 rounded-lg bg-blue-700 px-3 text-[13px] font-semibold text-white shadow-[0_6px_18px_rgba(37,99,235,.22)] transition hover:bg-blue-600 focus-visible:outline-white",
            collapsed && "lg:justify-center lg:px-0",
          )}
        >
          <Sparkles size={17} />
          {!collapsed && <span className="flex-1 text-left">Ask Enter AI</span>}
          {ceoUnreadMessages > 0 && !collapsed && <span aria-hidden="true" className="min-w-5 rounded-full bg-red-600 px-1.5 py-0.5 text-center text-[10px] font-bold text-white">{ceoUnreadMessages > 99 ? "99+" : ceoUnreadMessages}</span>}
          {ceoUnreadMessages > 0 && collapsed && <i aria-hidden="true" className="absolute right-3 top-3 h-2.5 w-2.5 rounded-full bg-red-500 ring-2 ring-blue-500" />}
        </button>
        {!collapsed && <p className="mt-2 px-1 text-center text-[10px] text-slate-500">Workspace intelligence, on demand</p>}
      </nav>

      <div className="border-t border-white/[.08] p-3">
        <div className="flex min-w-0 items-center gap-3 rounded-lg px-2 py-2">
          <Avatar user={user} />
          {!collapsed && (
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs font-semibold text-slate-100">{user?.name || "Enter AI Admin"}</span>
              <span className="mt-0.5 block truncate text-[10px] capitalize text-slate-400">{user?.role || "Workspace member"}</span>
            </span>
          )}
        </div>
        <button
          aria-label="Sign out"
          onClick={logout}
          title={collapsed ? "Sign out" : undefined}
          className={cn("mt-1 flex min-h-10 w-full items-center gap-3 rounded-lg px-3 text-xs font-medium text-slate-300 transition hover:bg-white/[.07] hover:text-white", collapsed && "lg:justify-center lg:px-0")}
        >
          <LogOut size={16} />
          {!collapsed && "Sign out"}
        </button>
      </div>
    </aside>
  );
}
