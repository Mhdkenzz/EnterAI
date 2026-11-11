import { expect } from "@playwright/test";
import { test } from "./fixtures";
test("core workspace flows stay interactive", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  const projectName = `Playwright ${test.info().project.name} ${Date.now()}`;
  await page.getByRole("button", { name: "New project" }).click();
  await page.getByLabel("Project name").fill(projectName);
  await page.getByLabel("Code").fill(`PW${Date.now().toString(36).toUpperCase()}`);
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible({ timeout: 15000 });
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Title", { exact: true }).fill("Playwright task");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByText("Playwright task")).toBeVisible();
  await page.getByRole("button", { name: "List view" }).click();
  const complete = page.locator("button[aria-label='Complete Playwright task']");
  await expect(complete).toBeVisible();
  await complete.click();
  await expect(page.locator("button[aria-label='Reopen Playwright task']")).toBeVisible({ timeout: 15000 });
});
test("responsive navigation opens Copilot and closes on mobile", async ({ page, isMobile }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  const navigation = page.getByRole("complementary");
  if (isMobile) {
    await expect(navigation).toBeHidden();
    await expect(page.getByRole("button", { name: "Ask Enter AI", exact: true }).last()).toBeVisible();
    await page.getByRole("button", { name: "Open menu", exact: true }).click();
  }
  await expect(navigation).toBeVisible();
  await expect(navigation.getByRole("button", { name: "Teams", exact: true })).toBeVisible();
  await navigation.getByRole("button", { name: "Ask Enter AI", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Enter AI Copilot" })).toBeVisible();
  if (isMobile) await expect(navigation).toBeHidden();
});
test("project brief upload drafts editable fields", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await page.locator("header button").last().click();
  await page.locator('input[type="file"]').setInputFiles({ name: "brief.txt", mimeType: "text/plain", buffer: Buffer.from("Urgent launch\nPrepare rollout checklist") });
  await expect(page.getByText("Document read.")).toBeVisible();
  await expect(page.getByText("AI suggestions")).toBeVisible();
  await expect(page.getByLabel("Project name")).not.toHaveValue("");
});

test("Copilot answers workspace questions and shows a confirmation before writing", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await page.goto("/ai");
  await expect(page.getByRole("heading", { name: "Enter AI Copilot" })).toBeVisible();
  await page.getByPlaceholder("Ask about work or propose a task").fill("What should I work on today?");
  await page.getByRole("button", { name: "Send message" }).click();
  // The isolated workspace may have no assigned tasks. In that case the
  // deterministic provider correctly recommends reviewing risks/milestones.
  await expect(page.getByText(/Today, focus on|You have no open assigned tasks/)).toBeVisible();
  await page.getByPlaceholder("Ask about work or propose a task").fill("Create task prepare Copilot review for AI Workspace");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("button", { name: "Confirm and run" })).toBeVisible();
});
