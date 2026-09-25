export const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";

// The session lives in an httpOnly cookie the API sets at sign-in, so page script
// never holds the token. Cookie-authenticated writes must carry this header; a
// cross-site form cannot add it, which is the API's CSRF check.
const CLIENT_HEADER = { "X-EnterAI-Client": "1" };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

// Fired when a signed-in request comes back 401 (expired, revoked or signed out
// elsewhere); the app listens for it and returns to the sign-in screen.
export const SESSION_EXPIRED_EVENT = "enterai:session-expired";

/** FastAPI validation errors arrive as a list of {loc, msg}; show the first one as a
 *  sentence instead of a generic "Request failed". */
function describe(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { loc?: unknown[]; msg?: string };
    const field = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1] ?? "") : "";
    const label = field && field !== "body" ? field.replace(/_/g, " ") + ": " : "";
    if (first.msg) return label + first.msg.replace(/^Value error, /, "");
  }
  return fallback;
}

/** A 401 is not always a dead session: re-entering a wrong password to export data or
 *  delete an account is also a 401. Only sign the person out when /me agrees. */
let sessionCheck: Promise<void> | null = null;
function checkSession(path: string) {
  if (typeof window === "undefined" || path.startsWith("/auth/")) return;
  const expired = () => window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  if (path === "/me") return expired();
  // A signed-out page load fails several requests at once; one check answers for all.
  sessionCheck ??= fetch(`${API_BASE}/me`, { credentials: "include" })
    .then((r) => {
      if (r.status === 401) expired();
    })
    .catch(() => {})
    .finally(() => {
      sessionCheck = null;
    });
}

async function failure(response: Response, fallback: string, path: string): Promise<ApiError> {
  if (response.status === 401) checkSession(path);
  const body = await response.json().catch(() => ({ detail: fallback }));
  return new ApiError(describe(body.detail, fallback), response.status);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...CLIENT_HEADER, ...options.headers },
  });
  if (!response.ok) throw await failure(response, "Request failed", path);
  return response.status === 204 ? (undefined as T) : response.json();
}

export async function upload<T>(path: string, file: File): Promise<T> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body,
    credentials: "include",
    headers: CLIENT_HEADER,
  });
  if (!response.ok) throw await failure(response, "Upload failed", path);
  return response.json();
}
