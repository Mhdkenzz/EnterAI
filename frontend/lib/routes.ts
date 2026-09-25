/** The workspace is one client page; these map its views to real URLs so Back and
 *  Forward work, a refresh keeps your place, and a project or task can be shared as
 *  a link. An open task is `?task=<id>` on top of whichever view it was opened from. */
export type Route = { view: string; projectId?: string; taskId?: string };

const VIEW_PATHS: [string, string][] = [
  ["Home", "/"],
  ["Portfolio", "/projects"],
  ["My work", "/my-work"],
  ["Inbox", "/inbox"],
  ["Teams", "/teams"],
  ["Agents", "/agents"],
  ["AI", "/ai"],
  ["Settings", "/settings"],
  ["Admin", "/admin"],
];

export function routeToUrl(route: Route): string {
  const base =
    route.view === "Project" && route.projectId
      ? `/projects/${encodeURIComponent(route.projectId)}`
      : (VIEW_PATHS.find(([view]) => view === route.view)?.[1] ?? "/");
  return route.taskId ? `${base}?task=${encodeURIComponent(route.taskId)}` : base;
}

export function urlToRoute(pathname: string, search: string): Route {
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  const taskId = new URLSearchParams(search).get("task") || undefined;
  if (parts[0] === "projects" && parts[1]) return { view: "Project", projectId: parts[1], taskId };
  const match = VIEW_PATHS.find(([, path]) => path === "/" + (parts[0] ?? ""));
  return { view: match ? match[0] : "Home", taskId };
}
