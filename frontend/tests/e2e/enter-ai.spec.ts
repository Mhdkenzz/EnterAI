import { expect, test } from "@playwright/test";
test("core workspace flows stay interactive", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  const projectName = `Playwright ${test.info().project.name} ${Date.now()}`;
  await page.getByRole("button", { name: "New project" }).click();
  await page.getByLabel("Project name").fill(projectName);
  await page.getByLabel("Code").fill(`PW${test.info().project.name === "mobile" ? "M" : "D"}`);
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible({ timeout: 15000 });
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Title").fill("Playwright task");
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
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  const navigation = page.getByRole("complementary");
  if (isMobile) {
    await expect(navigation).toBeHidden();
    await expect(page.getByRole("button", { name: "Ask Enter AI", exact: true })).toHaveCount(1);
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
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  await page.locator("header button").last().click();
  await page.locator('input[type="file"]').setInputFiles({ name: "brief.txt", mimeType: "text/plain", buffer: Buffer.from("Urgent launch\nPrepare rollout checklist") });
  await expect(page.getByText("Document read.")).toBeVisible();
  await expect(page.getByText("AI suggestions")).toBeVisible();
  await expect(page.getByLabel("Project name")).not.toHaveValue("");
});

test("Copilot answers workspace questions and shows a confirmation before writing", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("main").getByRole("button", { name: "Ask Enter AI", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Enter AI Copilot" })).toBeVisible();
  await page.getByPlaceholder("Ask about work or propose a task").fill("What should I work on today?");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText("Today, focus on", { exact: false })).toBeVisible();
  await page.getByPlaceholder("Ask about work or propose a task").fill("Create task prepare Copilot review for AI Workspace");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("button", { name: "Confirm and run" })).toBeVisible();
});
