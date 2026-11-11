import { expect } from "@playwright/test";
import { test } from "./fixtures";
import { signIn } from "./session";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";

test("high-priority task creation opens the acceptance and review rubric fields", async ({ page, request }) => {
  const key = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const registration = await request.post(`${API}/auth/register`, { data: {
    organization_name: `Priority QA ${key}`, name: "Priority Owner",
    email: `priority-${key}@example.com`, password: "Unrelated-long-passphrase-6842",
  } });
  expect(registration.ok(), await registration.text()).toBeTruthy();
  const account = await registration.json();
  const headers = { Authorization: `Bearer ${account.token}` };
  const projectResponse = await request.post(`${API}/projects`, { headers, data: {
    name: `High priority ${key}`, code: `P${key.replaceAll("-", "").slice(-10).toUpperCase()}`,
  } });
  expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
  const project = await projectResponse.json();
  await signIn(page.context(), account.token);
  await page.goto(`/projects/${project.id}`);
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Title", { exact: true }).fill("High-impact task");
  await page.getByLabel("Priority", { exact: true }).selectOption("high");
  const dialog = page.getByRole("dialog", { name: /New task in/ });
  await expect(page.getByLabel("Acceptance criteria, one per line")).toBeVisible();
  await expect(page.getByLabel("Review rubric, one per line")).toBeVisible();
  await expect(page.getByText(/Require acceptance criteria/)).toBeVisible();
  await expect(dialog.getByRole("checkbox").nth(1)).toBeChecked();
});

test("QA criteria, evidence, automated checks, and human approval gate task delivery", async ({ page, request }) => {
  const key = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const registration = await request.post(`${API}/auth/register`, { data: {
    organization_name: `QA workflow ${key}`, name: "QA Owner",
    email: `qa-${key}@example.com`, password: "Unrelated-long-passphrase-9157",
  } });
  expect(registration.ok(), await registration.text()).toBeTruthy();
  const account = await registration.json();
  const headers = { Authorization: `Bearer ${account.token}` };
  const projectResponse = await request.post(`${API}/projects`, { headers, data: {
    name: `QA delivery ${key}`, code: `Q${key.replaceAll("-", "").slice(-10).toUpperCase()}`,
  } });
  expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
  const project = await projectResponse.json();
  const taskResponse = await request.post(`${API}/tasks`, { headers, data: {
    project_id: project.id, title: "Ship reviewed deliverable", status: "review",
    qa_required: true, requires_human_approval: true,
    acceptance_criteria: ["The requested behavior is delivered"],
    review_rubric: ["Evidence supports the acceptance criterion"],
  } });
  expect(taskResponse.ok(), await taskResponse.text()).toBeTruthy();
  const task = await taskResponse.json();
  await signIn(page.context(), account.token);
  await page.goto(`/projects/${project.id}?task=${task.id}`);

  const drawer = page.getByRole("dialog", { name: `Task: ${task.title}` });
  const qa = drawer.getByRole("region", { name: "Quality assurance before delivery" });
  await expect(qa.getByLabel("Acceptance criteria")).toHaveValue("The requested behavior is delivered");
  await qa.getByLabel("Delivery evidence").fill("Validation output confirms the behavior and matches the acceptance criterion.");
  await qa.getByRole("button", { name: "Save evidence" }).click();
  await expect(qa.getByLabel("Delivery evidence")).toHaveValue(/Validation output/);
  await qa.getByRole("button", { name: "Run QA checks" }).click();
  const results = qa.getByRole("region", { name: "QA check results" });
  await expect(results.getByText(/Passed · evidence/)).toBeVisible();
  await expect(results.getByText(/Passed · acceptance criteria/)).toBeVisible();

  await drawer.getByLabel("Status").selectOption("done");
  await expect(drawer.getByText("Completed", { exact: true })).toBeVisible();
});
