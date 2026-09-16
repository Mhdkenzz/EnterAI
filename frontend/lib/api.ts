const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = typeof window === "undefined" ? null : localStorage.getItem("enter-token");
  const response = await fetch(`${BASE}${path}`, { ...options, headers: { "Content-Type":"application/json", ...(token ? {Authorization:`Bearer ${token}`} : {}), ...options.headers } });
  if (!response.ok) throw new Error((await response.json().catch(()=>({detail:"Request failed"}))).detail || "Request failed");
  return response.status === 204 ? (undefined as T) : response.json();
}
