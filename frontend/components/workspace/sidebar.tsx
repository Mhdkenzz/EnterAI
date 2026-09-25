"use client";
import {
  ChevronLeft,
  ChevronRight,
  Circle,
  FolderKanban,
  GitBranch,
  Grid2X2,
  Inbox,
  Settings2,
  Shield,
  Sparkles,
  Users,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Avatar } from "@/components/workspace/shared";

export function Sidebar({ collapsed, mobile, view, user, notices, toggle, close, nav, ai }: any) {
  const items = [
    [Grid2X2, "Home"],
    [FolderKanban, "Portfolio"],
    [Circle, "My work"],
    [Inbox, "Inbox"],
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
        "fixed inset-y-0 left-0 z-20 flex w-64 flex-col border-r border-line bg-[#0c0c0f] transition-transform lg:visible lg:translate-x-0",
        mobile ? "visible translate-x-0" : "invisible -translate-x-full",
        collapsed && "lg:w-[72px]",
      )}
    >
      <div className="flex h-16 items-center gap-3 px-4">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-accent font-black text-slate-950">
          E
        </div>
        {!collapsed && <span className="font-semibold">Enter AI</span>}
        <button
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="ml-auto hidden text-zinc-500 lg:block"
          onClick={toggle}
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
        <button aria-label="Close navigation" className="ml-auto lg:hidden" onClick={close}>
          <X size={17} />
        </button>
      </div>
      <nav className="px-3">
        {items.map(([Icon, label]) => (
          <button
            key={label}
            aria-label={label}
            aria-current={view === label ? "page" : undefined}
            onClick={() => nav(label)}
            className={cn(
              "mb-1 flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-zinc-400 hover:bg-white/5",
              view === label && "bg-sky-400/10 text-sky-300",
              collapsed && "lg:justify-center lg:px-0",
            )}
          >
            <Icon size={17} />
            {!collapsed && label}
            {label === "Inbox" && notices.some((x: any) => !x.read) && !collapsed && (
              <i className="ml-auto h-1.5 w-1.5 rounded-full bg-sky-400" />
            )}
          </button>
        ))}
        <div className="my-4 h-px" />
        <button
          onClick={ai}
          className="flex w-full gap-3 rounded-lg bg-sky-400/10 px-3 py-2.5 text-sm text-sky-300"
        >
          <Sparkles size={17} />
          {!collapsed && "Ask Enter AI"}
        </button>
      </nav>
      <div className="mt-auto border-t border-line p-3">
        <div className="flex gap-3 p-2">
          <Avatar user={user} />
          {!collapsed && <span className="text-sm">{user?.name || "Enter AI Admin"}</span>}
        </div>
        <button
          aria-label="Sign out"
          onClick={logout}
          className={cn(
            "mt-1 flex w-full items-center gap-3 rounded-lg px-2 py-2 text-sm text-zinc-400 hover:bg-white/5 hover:text-zinc-100",
            collapsed && "lg:justify-center",
          )}
        >
          <X size={16} />
          {!collapsed && "Sign out"}
        </button>
      </div>
    </aside>
  );
}
