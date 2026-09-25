import type { BrowserContext } from "@playwright/test";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";
export const SESSION_COOKIE = "enterai_session";

/** Puts a session token where the API's own sign-in would: an httpOnly cookie on
 *  the API host. Replaces whatever session the context already holds. */
export async function signIn(context: BrowserContext, token: string) {
  const api = new URL(API);
  await context.clearCookies({ name: SESSION_COOKIE });
  await context.addCookies([
    { name: SESSION_COOKIE, value: token, domain: api.hostname, path: "/api", httpOnly: true, secure: false, sameSite: "Lax" },
  ]);
}

export async function sessionCookie(context: BrowserContext) {
  return (await context.cookies()).find(cookie => cookie.name === SESSION_COOKIE);
}
