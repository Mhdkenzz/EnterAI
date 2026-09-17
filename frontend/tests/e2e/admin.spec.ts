import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test as base, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";
type Workspace = { token: string; memberToken: string; adminId: string; memberId: string; adminName: string; memberName: string };

// Seed only a caller-provided disposable database, never the developer's default DB.
// Run the API with the same DATABASE_URL and set E2E_DATABASE_URL for this suite.
const test = base.extend<{ workspace: Workspace }>({
  workspace: async ({ request }, use) => {
    const db = process.env.E2E_DATABASE_URL;
    if (!db || !db.startsWith("sqlite:////tmp/")) {
      throw new Error("Admin E2E requires E2E_DATABASE_URL=sqlite:////tmp/<disposable database>, matching the running API's DATABASE_URL");
    }
    const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    const response = await request.post(`${API}/auth/register`, { data: {
      organization_name: `Admin E2E ${suffix}`, name: `Admin ${suffix}`,
      email: `admin-${suffix}@example.com`, password: `Disposable-${suffix}`,
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const registered = await response.json();
    // There is no public invitation/create-member API. Seed a second human in
    // this unique test org, then authenticate through the real login endpoint.
    const backend = resolve(__dirname, "../../../backend");
    const member = JSON.parse(execFileSync(process.env.E2E_PYTHON || resolve(backend, ".venv/bin/python"), ["-c", `
import json, sys
from app.database import SessionLocal
from app.models import User, Organization
from app.auth import hash_password
with SessionLocal() as db:
    org = db.get(Organization, sys.argv[1])
    assert org and org.name.startswith('Admin E2E '), 'Refusing non-test organization'
    member = User(organization_id=org.id, name='Member '+sys.argv[2], email='member-'+sys.argv[2]+'@example.com', password_hash=hash_password('Disposable-'+sys.argv[2]), role='member', kind='human')
    db.add(member); db.commit(); db.refresh(member)
    print(json.dumps({'id': member.id, 'name': member.name, 'email': member.email}))
`, registered.organization.id, suffix], { cwd: backend, env: { ...process.env, DATABASE_URL: db }, encoding: "utf8" }));
    const login = await request.post(`${API}/auth/login`, { data: { email: member.email, password: `Disposable-${suffix}` } });
    expect(login.ok(), await login.text()).toBeTruthy();
    const memberLogin = await login.json();
    await use({ token: registered.token, adminId: registered.user.id, adminName: registered.user.name, memberId: member.id, memberName: member.name, memberToken: memberLogin.token });
    // Unique org data is intentionally retained in the disposable DB for traces.
  },
});

async function enter(page: Page, token: string) {
  await page.addInitScript(value => localStorage.setItem("enter-token", value), token);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
}
async function openAdmin(page: Page, mobile: boolean) {
  if (mobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Admin", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Admin console", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Usage overview" })).toBeVisible();
}
const auth = (workspace: Workspace) => ({ Authorization: `Bearer ${workspace.token}` });

test("human admin sees usage, member has no Admin navigation", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  await expect(page.getByText(/Tracked since Phase 7/)).toBeVisible();
  await expect(page.getByText(/Provider mode:/)).toBeVisible();
  await expect(page.getByText("No agents configured.")).toBeVisible();
  await page.evaluate(token => localStorage.setItem("enter-token", token), workspace.memberToken);
  // Clear init script through a new page is unnecessary: reload does re-run it,
  // so replace storage after navigation using a dedicated context page instead.
  const memberPage = await page.context().newPage();
  await memberPage.goto("/");
  await expect(memberPage.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  if (isMobile) await memberPage.getByRole("button", { name: "Open menu", exact: true }).click();
  await expect(memberPage.getByRole("button", { name: "Admin", exact: true })).toHaveCount(0);
  await memberPage.close();
});

test("execution controls persist and roles protect the signed-in admin", async ({ page, isMobile, request, workspace }) => {
  const hierarchy = await request.patch(`${API}/hierarchy-config`, { headers: auth(workspace), data: { vp_count: 1, directors_per_vp: 0, managers_per_director: 0, workers_per_manager: 0 } });
  expect(hierarchy.ok()).toBeTruthy();
  const agentsResponse = await request.get(`${API}/agents`, { headers: auth(workspace) });
  const agents = await agentsResponse.json();
  expect(agents.length).toBeGreaterThan(0);
  const agent = agents.find((item: { hierarchy_level: string }) => item.hierarchy_level === "ceo");
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  const orgToggle = page.getByRole("switch", { name: "Organization execution", exact: true });
  const execution = page.getByRole("region", { name: "Execution controls", exact: true });
  const idle = execution.getByText("Status: idle · Effective execution: enabled", { exact: true });
  const disabled = execution.getByText("Status: disabled · Effective execution: disabled", { exact: true });
  await expect(idle).toHaveCount(agents.length);
  await expect(orgToggle).toBeChecked();
  await orgToggle.click();
  await expect(orgToggle).not.toBeChecked();
  await expect(disabled).toHaveCount(agents.length);
  await orgToggle.click();
  await expect(orgToggle).toBeChecked();
  await expect(idle).toHaveCount(agents.length);
  const agentToggle = page.getByRole("switch", { name: `Execution for ${agent.name}`, exact: true });
  await agentToggle.click();
  await expect(agentToggle).not.toBeChecked();
  await expect(disabled).toHaveCount(1);
  await expect(idle).toHaveCount(agents.length - 1);
  await agentToggle.click();
  await expect(agentToggle).toBeChecked();
  await expect(idle).toHaveCount(agents.length);
  await agentToggle.click();
  await expect(agentToggle).not.toBeChecked();
  await orgToggle.click();
  await expect(orgToggle).not.toBeChecked();
  await expect(disabled).toHaveCount(agents.length);
  const ownRole = page.getByRole("combobox", { name: `Role for ${workspace.adminName}`, exact: true });
  await expect(ownRole).toBeDisabled();
  await expect(page.getByText(/You cannot change your own role\./)).toBeVisible();
  const memberRole = page.getByRole("combobox", { name: `Role for ${workspace.memberName}`, exact: true });
  await memberRole.selectOption("manager");
  await page.getByRole("button", { name: `Save role for ${workspace.memberName}`, exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Role updated." })).toBeVisible();
  await page.reload();
  await openAdmin(page, isMobile);
  await expect(orgToggle).not.toBeChecked();
  await expect(agentToggle).not.toBeChecked();
  await expect(memberRole).toHaveValue("manager");
  const settings = await request.get(`${API}/admin/settings`, { headers: auth(workspace) });
  expect((await settings.json()).execution_enabled).toBe(false);
  const usage = await request.get(`${API}/admin/usage`, { headers: auth(workspace) });
  expect((await usage.json()).agents.find((item: { id: string }) => item.id === agent.id).execution_enabled).toBe(false);
});

test("audit filters, attribution and pagination use real recorded actions", async ({ page, isMobile, request, workspace }) => {
  for (let i = 0; i < 52; i++) {
    const response = await request.patch(`${API}/admin/settings`, { headers: auth(workspace), data: { execution_enabled: i % 2 === 1 } });
    expect(response.ok()).toBeTruthy();
  }
  const response = await request.get(`${API}/admin/audit?limit=1`, { headers: auth(workspace) });
  const latest = (await response.json()).items[0];
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  const audit = page.getByRole("region", { name: "Audit log", exact: true });
  await expect(audit.getByRole("button", { name: "Next audit page" })).toBeEnabled();
  await audit.getByRole("button", { name: "Next audit page" }).click();
  await expect(audit.getByText(/Showing 51–/)).toBeVisible();
  await audit.getByLabel("Action", { exact: true }).fill(latest.action);
  await audit.getByLabel("Source", { exact: true }).fill(latest.source);
  await audit.getByLabel("Actor ID", { exact: true }).fill(workspace.adminId);
  await audit.getByLabel("Entity type", { exact: true }).fill(latest.entity_type);
  await audit.getByRole("button", { name: "Apply audit filters" }).click();
  await expect(audit.getByText(/Showing 1–/)).toBeVisible();
  const entry = audit.getByRole("article").filter({ hasText: latest.id });
  await expect(entry).toContainText(`Actor: ${workspace.adminId}`);
  await expect(entry).toContainText(`Initiator: ${latest.initiator_id || "—"}`);
  await expect(entry).toContainText(`Source: ${latest.source}`);
  await audit.getByLabel("Search audit", { exact: true }).fill("no-such-audit-record-7f989e");
  await audit.getByRole("button", { name: "Apply audit filters" }).click();
  await expect(audit.getByText("No audit events match these filters.")).toBeVisible();
  await audit.getByRole("button", { name: "Clear audit filters" }).click();
  await expect(audit.getByRole("article")).not.toHaveCount(0);
});

test("admin load and save failures remain visible and retryable", async ({ page, isMobile, workspace }) => {
  await page.route("**/api/admin/usage", route => route.abort("failed"));
  await enter(page, workspace.token);
  if (isMobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Admin", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Could not load admin console" })).toBeVisible();
  await page.unroute("**/api/admin/usage");
  await page.getByRole("button", { name: "Retry admin console" }).click();
  await expect(page.getByRole("heading", { name: "Usage overview" })).toBeVisible();
  const toggle = page.getByRole("switch", { name: "Organization execution", exact: true });
  await expect(toggle).toBeChecked();
  await page.route("**/api/admin/settings", route => route.request().method() === "PATCH" ? route.abort("failed") : route.continue());
  await toggle.click();
  const saveError = page.getByRole("main").getByRole("alert").filter({ hasText: "Failed to fetch" });
  await expect(saveError).toBeVisible();
  await expect(toggle).toBeChecked();
  await expect(toggle).toBeEnabled();
  await page.unroute("**/api/admin/settings");
  await toggle.click();
  await expect(toggle).not.toBeChecked();
});

test("obsolete audit responses cannot replace the latest search", async ({ page, isMobile, request, workspace }) => {
  const response = await request.patch(`${API}/admin/settings`, { headers: auth(workspace), data: { execution_enabled: false } });
  expect(response.ok()).toBeTruthy();
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  const audit = page.getByRole("region", { name: "Audit log", exact: true });
  await expect(audit.getByRole("article")).not.toHaveCount(0);
  let release: () => void = () => {};
  let announce: () => void = () => {};
  const held = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { announce = resolve; });
  await page.route("**/api/admin/audit?**", async route => {
    if (new URL(route.request().url()).searchParams.get("q") === "execution_updated") {
      const actualResponse = await route.fetch();
      announce();
      await held;
      // Obsolete request may already have been aborted by the component.
      await route.fulfill({ response: actualResponse }).catch(() => {});
    } else await route.continue();
  });
  await audit.getByLabel("Search audit", { exact: true }).fill("execution_updated");
  await audit.getByRole("button", { name: "Apply audit filters" }).click();
  await started;
  await expect(audit.getByText("Loading audit events…")).toBeVisible();
  await audit.getByLabel("Search audit", { exact: true }).fill("no-such-audit-record-7f989e");
  await audit.getByRole("button", { name: "Apply audit filters" }).click();
  await expect(audit.getByText("No audit events match these filters.")).toBeVisible();
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(audit.getByText("No audit events match these filters.")).toBeVisible();
  await expect(audit.getByRole("article")).toHaveCount(0);
});

test("admin console is accessible without horizontal overflow", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  await expect(page.getByRole("combobox", { name: `Role for ${workspace.adminName}`, exact: true })).toBeVisible();
  // Registration and member login are audited; scan the populated console,
  // including attribution and expanded detail, rather than assuming an empty log.
  const audit = page.getByRole("region", { name: "Audit log", exact: true });
  const registration = audit.getByRole("article").filter({ has: page.getByRole("heading", { name: "registered", exact: true }) });
  await expect(registration).toBeVisible();
  await expect(registration).toContainText(`Actor: ${workspace.adminId}`);
  await registration.getByText("Event detail", { exact: true }).click();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter(v => ["critical", "serious"].includes(v.impact || ""))).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  if (isMobile) await expect(page.getByRole("complementary")).toBeHidden();
});
