import { expect } from "@playwright/test";
import { test } from "./fixtures";
import { signIn } from "./session";

const API = process.env.E2E_API_URL || "http://127.0.0.1:8000/api";

test("a measurable north star connects a milestone to owned project work", async ({ page, request }) => {
  const key = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const registration = await request.post(`${API}/auth/register`, { data: {
    organization_name: `Coordination ${key}`, name: "Coordination Owner",
    email: `coord-${key}@example.com`, password: "Coordination-passphrase-2026",
  } });
  expect(registration.ok(), await registration.text()).toBeTruthy();
  const account = await registration.json();
  const projectResponse = await request.post(`${API}/projects`, {
    headers: { Authorization: `Bearer ${account.token}` },
    data: { name: `Activation ${key}`, code: `C${key.replaceAll("-", "").slice(-8).toUpperCase()}` },
  });
  expect(projectResponse.ok(), await projectResponse.text()).toBeTruthy();
  const project = await projectResponse.json();

  await signIn(page.context(), account.token);
  await page.goto(`/projects/${project.id}`);
  await page.getByRole("button", { name: "Set goal" }).click();
  const edit = page.getByRole("dialog", { name: "Edit project" });
  await edit.getByLabel("North-star outcome").fill("Help new customers activate in their first week");
  await edit.getByLabel("Success metric").fill("Weekly activated customers");
  await edit.getByLabel("Metric unit").fill("customers");
  await edit.getByLabel("Metric baseline value").fill("10");
  await edit.getByLabel("Metric current value").fill("20");
  await edit.getByLabel("Metric target value").fill("50");
  await edit.getByLabel("Launch date").fill("2027-03-19");
  await edit.getByLabel("Launch freeze rule").fill("Freeze changes 48 hours before launch; only approved fixes may ship.");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Help new customers activate in their first week")).toBeVisible();
  await expect(page.getByText("25% of the way to target")).toBeVisible();
  await page.getByRole("button", { name: "Edit project" }).click();
  const reopened = page.getByRole("dialog", { name: "Edit project" });
  await expect(reopened.getByLabel("Launch date")).toHaveValue("2027-03-19");
  await expect(reopened.getByLabel("Launch freeze rule")).toHaveValue("Freeze changes 48 hours before launch; only approved fixes may ship.");
  await reopened.getByRole("button", { name: "Cancel" }).click();

  await page.getByLabel("Milestone due date").fill("2027-03-16");
  await page.getByLabel("New milestone title").fill("First week activation");
  await page.getByRole("button", { name: "Add milestone" }).click();
  await expect(page.getByText("First week activation", { exact: true })).toBeVisible();
  const savedProject = await request.get(`${API}/projects/${project.id}`, { headers: { Authorization: `Bearer ${account.token}` } });
  expect((await savedProject.json()).milestones[0].due_date).toMatch(/^2027-03-16/);

  await page.getByRole("button", { name: "New task" }).click();
  const taskForm = page.getByRole("dialog", { name: `New task in ${project.name}` });
  await taskForm.getByLabel("Title", { exact: true }).fill("Review activation funnel");
  await taskForm.getByLabel("Milestone").selectOption({ label: "First week activation" });
  await taskForm.getByLabel("Due date", { exact: true }).fill("2027-03-17");
  await taskForm.getByLabel("Description", { exact: true }).fill("Check the metric and report the result.");
  await taskForm.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByText("Review activation funnel", { exact: true })).toBeVisible();
  const savedTasks = await request.get(`${API}/projects/${project.id}/tasks`, { headers: { Authorization: `Bearer ${account.token}` } });
  const createdTask = (await savedTasks.json()).find((task: { title: string }) => task.title === "Review activation funnel");
  expect(createdTask?.due_date).toMatch(/^2027-03-17/);
  const milestone = page.getByText("First week activation", { exact: true }).locator("xpath=../..");
  await expect(milestone).toContainText("0/1 tasks complete");
  await expect(milestone).toContainText("Coordination Owner");
});
