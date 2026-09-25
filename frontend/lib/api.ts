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

async function failure(response: Response, fallback: string): Promise<ApiError> {
  const detail = (await response.json().catch(() => ({ detail: fallback }))).detail;
  return new ApiError(typeof detail === "string" ? detail : fallback, response.status);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...CLIENT_HEADER, ...options.headers },
  });
  if (!response.ok) throw await failure(response, "Request failed");
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
  if (!response.ok) throw await failure(response, "Upload failed");
  return response.json();
}
