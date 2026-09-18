import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";

function unique(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** A URL-safe token of the same shape the backend mints. */
function rawToken() {
  return Array.from({ length: 43 }, () => "abcdefghijklmnopqrstuvwxyz0123456789-_"[Math.floor(Math.random() * 38)]).join("");
}

/** Seeds a row only a real email would otherwise carry.
 *
 *  The database stores nothing but a hash of these tokens, on purpose, so a test
 *  cannot read one back out -- it has to choose the token and insert its hash, the
 *  way the application does before sending the mail. Guarded to a disposable
 *  database, exactly as admin.spec.ts does. */
function seed(script: string, args: string[]) {
  const db = process.env.E2E_DATABASE_URL;
  if (!db || !db.startsWith("sqlite:////tmp/")) {
    throw new Error("Auth E2E requires E2E_DATABASE_URL=sqlite:////tmp/<disposable database>, matching the running API");
  }
  const backend = resolve(__dirname, "../../../backend");
  return execFileSync(process.env.E2E_PYTHON || resolve(backend, ".venv/bin/python"), ["-c", script, ...args], {
    cwd: backend, env: { ...process.env, DATABASE_URL: db }, encoding: "utf8",
  }).trim();
}

async function registerWorkspace(request: Page["request"], key: string, name: string) {
  const response = await request.post(`${API}/auth/register`, {
    data: { organization_name: name, name: "Owner", email: `${key}@example.com`, password: "owner-password-1" },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

test("a visitor can create a workspace and lands in it as its first admin", async ({ page }) => {
  const key = unique("signup");
  await page.goto("/");
  await page.getByRole("button", { name: "Create a workspace" }).click();
  await expect(page.getByRole("heading", { name: "Create your workspace" })).toBeVisible();

  await page.getByLabel("Organization name").fill(`Workspace ${key}`);
  await page.getByLabel("Your name").fill("First Admin");
  await page.getByLabel("Work email").fill(`${key}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill("a-strong-password-1");
  await page.getByRole("button", { name: "Create workspace" }).click();

  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  // The first admin is met by onboarding and a prompt to confirm their address.
  await expect(page.getByText(`Finish setting up Workspace ${key}`)).toBeVisible();
  await expect(page.getByText("Confirm your email address to secure your account.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send invite" })).toBeVisible();
});

test("forgot password never reveals whether an address has an account", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Forgot your password?" }).click();
  await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
  await page.getByLabel("Email").fill(`${unique("ghost")}@example.com`);
  await page.getByRole("button", { name: "Email me a link" }).click();
  await expect(page.getByRole("status")).toContainText("If that address has an account");
});

test("an invited teammate accepts from the emailed link and is signed in", async ({ page }) => {
  const key = unique("invite");
  const account = await registerWorkspace(page.request, key, `Invite Org ${key}`);
  const token = rawToken();
  const invitee = `${key}-mate@example.com`;

  seed(`
import sys
from datetime import datetime, timedelta
from app.database import SessionLocal
from app.models import Invite, Organization
from app.tokens import hash_token
with SessionLocal() as db:
    org = db.get(Organization, sys.argv[1])
    assert org and org.name.startswith('Invite Org '), 'Refusing non-test organization'
    db.add(Invite(organization_id=org.id, email=sys.argv[2], role='manager',
                  token_hash=hash_token(sys.argv[3]),
                  expires_at=datetime.utcnow() + timedelta(days=7)))
    db.commit()
    print('seeded')
`, [account.organization.id, invitee, token]);

  await page.goto(`/?invite=${token}`);
  await expect(page.getByRole("heading", { name: `Join Invite Org ${key}` })).toBeVisible();
  await expect(page.getByText("You'll join as")).toContainText("manager");

  await page.getByLabel("Your name").fill("Invited Mate");
  await page.getByLabel("Choose a password").fill("invited-password-1");
  await page.getByRole("button", { name: "Accept invitation" }).click();

  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  // The single-use token is cleared from the address bar rather than left in history.
  expect(page.url()).not.toContain("invite=");
});

test("a reset link lets its owner choose a new password and sign in with it", async ({ page }) => {
  const key = unique("reset");
  const account = await registerWorkspace(page.request, key, `Reset Org ${key}`);
  const token = rawToken();

  seed(`
import sys
from datetime import datetime, timedelta
from app.database import SessionLocal
from app.models import AuthToken, User
from app.tokens import PASSWORD_RESET, hash_token
with SessionLocal() as db:
    user = db.get(User, sys.argv[1])
    assert user and user.email.endswith('@example.com'), 'Refusing non-test user'
    db.add(AuthToken(user_id=user.id, purpose=PASSWORD_RESET, token_hash=hash_token(sys.argv[2]),
                     expires_at=datetime.utcnow() + timedelta(minutes=60)))
    db.commit()
    print('seeded')
`, [account.user.id, token]);

  await page.goto(`/?reset=${token}`);
  await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
  await page.getByLabel("New password", { exact: true }).fill("rotated-password-9");
  await page.getByLabel("Confirm new password").fill("rotated-password-9");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("status")).toContainText("password has been changed");
  expect(page.url()).not.toContain("reset=");

  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("Email").fill(`${key}@example.com`);
  await page.getByLabel("Password", { exact: true }).fill("rotated-password-9");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
});

test("a bad reset link fails safe rather than silently doing nothing", async ({ page }) => {
  await page.goto(`/?reset=${rawToken()}`);
  await expect(page.getByRole("heading", { name: "Choose a new password" })).toBeVisible();
  await page.getByLabel("New password", { exact: true }).fill("attempted-password-1");
  await page.getByLabel("Confirm new password").fill("attempted-password-1");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("invalid or has expired");
});

test("the reset screen catches mismatched passwords before submitting", async ({ page }) => {
  await page.goto(`/?reset=${rawToken()}`);
  await page.getByLabel("New password", { exact: true }).fill("one-password-here");
  await page.getByLabel("Confirm new password").fill("another-password-xyz");
  await page.getByRole("button", { name: "Change password" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("do not match");
});

test("a verification link confirms the address and clears the banner", async ({ page }) => {
  const key = unique("verify");
  const account = await registerWorkspace(page.request, key, `Verify Org ${key}`);
  const token = rawToken();

  seed(`
import sys
from datetime import datetime, timedelta
from app.database import SessionLocal
from app.models import AuthToken, User
from app.tokens import EMAIL_VERIFICATION, hash_token
with SessionLocal() as db:
    user = db.get(User, sys.argv[1])
    assert user and user.email.endswith('@example.com'), 'Refusing non-test user'
    db.add(AuthToken(user_id=user.id, purpose=EMAIL_VERIFICATION, token_hash=hash_token(sys.argv[2]),
                     expires_at=datetime.utcnow() + timedelta(hours=24)))
    db.commit()
    print('seeded')
`, [account.user.id, token]);

  await page.goto(`/?verify=${token}`);
  await expect(page.getByRole("status")).toContainText("email address is confirmed");

  await page.addInitScript(value => localStorage.setItem("enter-token", value), account.token);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  await expect(page.getByText("Confirm your email address to secure your account.")).toHaveCount(0);
});

test("auth screens have no serious accessibility violations", async ({ page }) => {
  for (const path of ["/", `/?reset=${rawToken()}`, `/?invite=${rawToken()}`]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const results = await new AxeBuilder({ page }).analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, `${path}: ${serious.map((v) => v.id).join(", ")}`).toEqual([]);
  }
});

test("auth screens fit a phone without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  for (const path of ["/", `/?invite=${rawToken()}`]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, `${path} overflows by ${overflow}px`).toBeLessThanOrEqual(1);
  }
});
