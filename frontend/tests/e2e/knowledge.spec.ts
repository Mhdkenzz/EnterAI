import { expect } from "@playwright/test";
import { test } from "./fixtures";
import { signIn } from "./session";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";
const unique = (prefix: string) => `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;

async function workspace(page: import("@playwright/test").Page) {
  const key = unique("knowledge");
  const response = await page.request.post(`${API}/auth/register`, {
    data: { organization_name: `Knowledge ${key}`, name: "Knowledge Owner", email: `${key}@example.com`, password: "knowledge-passphrase-1" },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  const account = await response.json();
  await signIn(page.context(), account.token);
  const headers = { Authorization: `Bearer ${account.token}` };
  const projectResponse = await page.request.post(`${API}/projects`, { headers, data: { name: `Shared brief ${key}`, code: unique("KNOW").slice(0, 12).toUpperCase() } });
  expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
  const project = await projectResponse.json();
  const taskResponse = await page.request.post(`${API}/tasks`, { headers, data: { project_id: project.id, title: `Review decision ${key}` } });
  expect(taskResponse.ok(), await taskResponse.text()).toBeTruthy();
  const task = await taskResponse.json();
  return { account, headers, project, task };
}

test("workspace search groups knowledge sources and opens a task with the keyboard", async ({ page }) => {
  const { project, task } = await workspace(page);
  const createdAt = "2026-10-14T10:30:00Z";
  await page.route("**/api/search?*", (route) => route.fulfill({ json: {
    projects: [{ id: project.id, title: project.name, created_at: createdAt, project_id: project.id, source_ref: `/projects/${project.id}` }],
    tasks: [{ id: task.id, title: task.title, created_at: createdAt, project_id: project.id, task_id: task.id, source_ref: `/projects/${project.id}?task=${task.id}` }],
    documents: [{ id: "document-1", title: "Launch brief.pdf", snippet: "Approved rollout dates", created_at: createdAt, project_id: project.id, source_ref: `/projects/${project.id}` }],
    attachments: [{ id: "attachment-1", title: "Risk register.csv", snippet: "Three open risks", created_at: createdAt, project_id: project.id, task_id: task.id, source_ref: `/projects/${project.id}?task=${task.id}` }],
    discussions: [{ id: "discussion-1", title: "Owner clarified the deadline", snippet: "Move review to Friday", created_at: createdAt, project_id: project.id, task_id: task.id, source_ref: `/projects/${project.id}?task=${task.id}` }],
    decisions: [{ id: "decision-1", title: "Ship after review", snippet: "Release only after QA signs off", created_at: createdAt, project_id: project.id, task_id: task.id, source_ref: `/projects/${project.id}?task=${task.id}` }],
  } }));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await page.getByRole("button", { name: "Search or ask Enter AI" }).click();
  const dialog = page.getByRole("dialog", { name: "Search workspace knowledge" });
  const input = dialog.getByRole("combobox", { name: "Search projects, tasks, files, discussions, and decisions" });
  await input.fill("review");
  await expect(dialog.getByRole("group", { name: "Projects" })).toContainText(project.name);
  await expect(dialog.getByRole("group", { name: "Tasks" })).toContainText(task.title);
  await expect(dialog.getByRole("group", { name: "Files" })).toContainText("Launch brief.pdf");
  await expect(dialog.getByRole("group", { name: "Attachments" })).toContainText("Risk register.csv");
  await expect(dialog.getByRole("group", { name: "Discussions" })).toContainText("Owner clarified the deadline");
  await expect(dialog.getByRole("group", { name: "Decisions" })).toContainText("Ship after review");
  await expect(dialog.getByRole("option").first()).toContainText("Projects ·");
  await expect(dialog.getByRole("option").first()).toContainText("Source:");

  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(page.getByRole("dialog", { name: `Task: ${task.title}` })).toBeVisible();
});

test("a comment can be recorded and displayed as a linked decision", async ({ page }) => {
  const { project, task, headers } = await workspace(page);
  const commentResponse = await page.request.post(`${API}/tasks/${task.id}/comments`, { headers, data: { body: "Keep the Friday review as the release gate." } });
  expect(commentResponse.ok(), await commentResponse.text()).toBeTruthy();
  const comment = await commentResponse.json();

  await page.goto(`/projects/${project.id}?task=${task.id}`);
  const drawer = page.getByRole("dialog", { name: `Task: ${task.title}` });
  const commentRow = drawer.getByText("Keep the Friday review as the release gate.").locator("..")
    .locator("..");
  await commentRow.getByRole("button", { name: /Record .* comment as a decision/ }).click();
  await expect(commentRow.getByRole("link", { name: /Saved decision: Keep the Friday review/ })).toBeVisible();
  await expect(commentRow.getByText("Recorded by")).toBeVisible();

  const saved = await page.request.get(`${API}/tasks/${task.id}/decisions`);
  expect(saved.ok(), await saved.text()).toBeTruthy();
  expect((await saved.json()).some((decision: { source_comment_id: string }) => decision.source_comment_id === comment.id)).toBeTruthy();
});

test("Copilot answers show dated, linked source references", async ({ page }) => {
  const { project } = await workspace(page);
  await page.route("**/api/ai/plan", (route) => route.fulfill({ json: {
    reply: "The release is planned after the QA review.",
    actions: [],
    sources: [{ id: "decision-1", type: "decision", title: "Release gate", created_at: "2026-10-14T10:30:00Z", source_ref: `/projects/${project.id}`, project_id: project.id, task_id: null }],
  } }));
  await page.goto("/ai");
  await page.getByPlaceholder("Ask about work or propose a task").fill("When is release planned?");
  await page.getByRole("button", { name: "Send message" }).click();
  const sources = page.getByRole("region", { name: "Sources used for this answer" });
  await expect(sources.getByRole("link", { name: "Release gate" })).toHaveAttribute("href", `/projects/${project.id}`);
  await expect(sources).toContainText("decision");
  await expect(sources.locator("time")).toHaveAttribute("dateTime", "2026-10-14T10:30:00Z");
});
