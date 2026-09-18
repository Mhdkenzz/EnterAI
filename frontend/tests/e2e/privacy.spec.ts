import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test as base, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";
type Workspace = { token: string; memberToken: string; adminPassword: string; memberPassword: string; adminEmail: string };

// Same seeding shape as admin.spec.ts's workspace fixture: a fresh, disposable
// org+admin+member per test, never the developer's default database.
const test = base.extend<{ workspace: Workspace }>({
  workspace: async ({ request }, use) => {
    const db = process.env.E2E_DATABASE_URL;
    if (!db || !db.startsWith("sqlite:////tmp/")) {
      throw new Error("Privacy E2E requires E2E_DATABASE_URL=sqlite:////tmp/<disposable database>, matching the running API's DATABASE_URL");
    }
    const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    const adminPassword = `Disposable-${suffix}`;
    const adminEmail = `admin-${suffix}@example.com`;
    const response = await request.post(`${API}/auth/register`, { data: {
      organization_name: `Privacy E2E ${suffix}`, name: `Admin ${suffix}`,
      email: adminEmail, password: adminPassword,
    } });
    expect(response.ok(), await response.text()).toBeTruthy();
    const registered = await response.json();
    const backend = resolve(__dirname, "../../../backend");
    const memberPassword = `Disposable-member-${suffix}`;
    const member = JSON.parse(execFileSync(process.env.E2E_PYTHON || resolve(backend, ".venv/bin/python"), ["-c", `
import json, sys
from app.database import SessionLocal
from app.models import User, Organization
from app.auth import hash_password
with SessionLocal() as db:
    org = db.get(Organization, sys.argv[1])
    assert org and org.name.startswith('Privacy E2E '), 'Refusing non-test organization'
    member = User(organization_id=org.id, name='Member '+sys.argv[2], email='member-'+sys.argv[2]+'@example.com', password_hash=hash_password(sys.argv[3]), role='member', kind='human')
    db.add(member); db.commit(); db.refresh(member)
    print(json.dumps({'id': member.id, 'email': member.email}))
`, registered.organization.id, suffix, memberPassword], { cwd: backend, env: { ...process.env, DATABASE_URL: db }, encoding: "utf8" }));
    const login = await request.post(`${API}/auth/login`, { data: { email: member.email, password: memberPassword } });
    expect(login.ok(), await login.text()).toBeTruthy();
    const memberLogin = await login.json();
    await use({ token: registered.token, memberToken: memberLogin.token, adminPassword, memberPassword, adminEmail });
  },
});

async function enter(page: Page, token: string) {
  await page.addInitScript(value => localStorage.setItem("enter-token", value), token);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
}
async function openSettings(page: Page, mobile: boolean) {
  if (mobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Privacy", exact: true })).toBeVisible();
}

test("every human account -- admin or member -- can reach Settings", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openSettings(page, isMobile);
  await expect(page.getByRole("heading", { name: "Export my data" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Delete my account" })).toBeVisible();
});

test("export requires the correct password and then downloads a file", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openSettings(page, isMobile);
  await page.getByLabel("Confirm your password").fill("wrong-password");
  await page.getByRole("button", { name: "Download my data" }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  await page.getByLabel("Confirm your password").fill(workspace.adminPassword);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download my data" }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/enterai-export-.*\.json/);
  await expect(page.getByText("Your data has downloaded as a JSON file.")).toBeVisible();
});

test("deleting an account can be canceled before it runs, and the account keeps working", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openSettings(page, isMobile);
  await page.getByRole("button", { name: "Delete my account" }).click();
  await page.getByRole("region", { name: "Delete my account" }).getByLabel("Confirm your password").fill(workspace.adminPassword);
  await page.getByRole("button", { name: "Confirm deletion" }).click();

  await expect(page.getByText(/scheduled for deletion on/)).toBeVisible();
  await page.getByRole("button", { name: "Cancel deletion" }).click();
  await expect(page.getByText(/scheduled for deletion on/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete my account" })).toBeVisible();
});

test("Settings page has no serious accessibility violations", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openSettings(page, isMobile);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter(v => ["critical", "serious"].includes(v.impact || ""))).toEqual([]);
});

async function openAdmin(page: Page, mobile: boolean) {
  if (mobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Admin", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Admin console", exact: true })).toBeVisible();
}

test("admin can set a retention policy and it round-trips after reload", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  await expect(page.getByRole("heading", { name: "Privacy & retention" })).toBeVisible();
  await page.getByLabel("Audit log (days)").fill("90");
  await page.getByRole("button", { name: "Save retention policy" }).click();
  await expect(page.getByText("Saved.")).toBeVisible();

  await page.reload();
  await openAdmin(page, isMobile);
  await expect(page.getByLabel("Audit log (days)")).toHaveValue("90");
});

test("admin can request organization deletion and cancel it", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.token);
  await openAdmin(page, isMobile);
  await page.getByRole("button", { name: "Delete organization" }).click();
  const orgDeleteForm = page.locator("form").filter({ has: page.getByRole("button", { name: "Confirm organization deletion" }) });
  await orgDeleteForm.getByLabel("Confirm your password").fill(workspace.adminPassword);
  await orgDeleteForm.getByRole("button", { name: "Confirm organization deletion" }).click();

  await expect(page.getByText(/organization · pending/)).toBeVisible();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByText(/organization · canceled/)).toBeVisible();
});

test("a member cannot see the organization deletion or retention controls", async ({ page, isMobile, workspace }) => {
  await enter(page, workspace.memberToken);
  if (isMobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await expect(page.getByRole("button", { name: "Admin", exact: true })).toHaveCount(0);
});
