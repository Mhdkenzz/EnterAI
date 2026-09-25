import { expect, test, type Page } from "@playwright/test";
import { signIn } from "./session";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";
const unique = (p: string) => `${p}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

/** A fresh workspace per test: its owner is an admin, so manager-level controls show. */
async function workspace(page: Page) {
  const key = unique("nav");
  const r = await page.request.post(`${API}/auth/register`, {
    data: { organization_name: `Nav ${key}`, name: "Owner", email: `${key}@example.com`, password: "owner-passphrase-1" },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  const account = await r.json();
  await page.context().clearCookies();
  await signIn(page.context(), account.token);
  const headers = { Authorization: `Bearer ${account.token}` };
  const project = await (await page.request.post(`${API}/projects`, { headers, data: { name: `Roadmap ${key}`, code: "RDM" } })).json();
  const task = await (await page.request.post(`${API}/tasks`, { headers, data: { project_id: project.id, title: "Draft the plan" } })).json();
  return { account, headers, project, task };
}

test("views have real URLs: deep links, refresh and Back all work", async ({ page, isMobile }) => {
  const { project, task } = await workspace(page);
  await page.goto(`/projects/${project.id}?task=${task.id}`);
  await expect(page.getByRole("heading", { name: project.name })).toBeVisible();
  await expect(page.getByRole("dialog", { name: "Task: Draft the plan" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(new RegExp(`/projects/${project.id}$`));
  await page.reload();
  await expect(page.getByRole("heading", { name: project.name })).toBeVisible();

  if (isMobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Inbox", exact: true }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/projects/${project.id}$`));
  await expect(page.getByRole("heading", { name: project.name })).toBeVisible();
  await page.goForward();
  await expect(page).toHaveURL(/\/inbox$/);
});

test("cards move between columns by drag and drop and by the keyboard buttons", async ({ page, isMobile }) => {
  test.skip(isMobile, "drag and drop is a pointer interaction; the buttons are covered on desktop");
  const { project } = await workspace(page);
  await page.goto(`/projects/${project.id}`);
  const card = page.getByRole("article").filter({ hasText: "Draft the plan" });
  await card.dragTo(page.getByRole("region", { name: "review" }));
  await expect(page.getByRole("region", { name: "review" }).getByText("Draft the plan")).toBeVisible();
  await page.getByRole("button", { name: "Move back Draft the plan" }).click();
  await expect(page.getByRole("region", { name: "in progress" }).getByText("Draft the plan")).toBeVisible();
});

test("subtasks and labels are managed from the task drawer", async ({ page }) => {
  const { project, task } = await workspace(page);
  await page.goto(`/projects/${project.id}?task=${task.id}`);
  const drawer = page.getByRole("dialog", { name: "Task: Draft the plan" });
  await drawer.getByLabel("New subtask").fill("Collect requirements");
  await drawer.getByRole("button", { name: "Add" }).click();
  await expect(drawer.getByRole("button", { name: "Collect requirements" })).toBeVisible();
  await drawer.getByLabel("Complete Collect requirements").check();
  await expect(drawer.getByText("1/1 done")).toBeVisible();
  await drawer.getByLabel("Labels").fill("planning, q4");
  await drawer.getByLabel("Labels").blur();
  await page.keyboard.press("Escape");
  const card = page.getByRole("article").filter({ hasText: "Draft the plan" });
  await expect(card).toContainText("planning");
  await expect(card).toContainText("1 subtask");
  // The subtask lives in its parent, not as a separate card on the board.
  await expect(page.getByRole("article").filter({ hasText: "Collect requirements" })).toHaveCount(0);
});

test("a project can be archived, restored and deleted", async ({ page }) => {
  const { project } = await workspace(page);
  await page.goto(`/projects/${project.id}`);
  await page.getByRole("button", { name: "Archive" }).click();
  await expect(page.getByText("This project is archived.")).toBeVisible();
  await page.getByRole("button", { name: "Restore project" }).click();
  await expect(page.getByText("This project is archived.")).toHaveCount(0);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByText(project.name)).toHaveCount(0);
});

test("the AI confirmation shows where a task will land and can be rejected", async ({ page }) => {
  const { project } = await workspace(page);
  await page.goto("/ai");
  await page.getByPlaceholder("Ask about work or propose a task").fill(`create task Book the venue for ${project.name}`);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText(project.name, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByText("Rejected. Nothing was changed.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm and run" })).toHaveCount(0);
});

// Removing another member is covered end to end in backend/tests/test_project_governance.py.
test("the members list explains the roles and never offers removing yourself", async ({ page }) => {
  await workspace(page);
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Members and roles" })).toBeVisible();
  // Only the owner exists yet, and nobody can remove themselves.
  await expect(page.getByRole("button", { name: /^Remove Owner/ })).toHaveCount(0);
});
