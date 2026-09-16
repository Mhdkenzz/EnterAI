import { expect, test } from "@playwright/test";
test("core workspace flows stay interactive", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  const projectName = `Playwright ${test.info().project.name} ${Date.now()}`;
  await page.getByRole("button", { name: "New project" }).click();
  await page.getByLabel("Project name").fill(projectName);
  await page.getByLabel("Code").fill("PW");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible();
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Title").fill("Playwright task");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByText("Playwright task")).toBeVisible();
  const complete = page.locator("button[aria-label='Complete Playwright task']");
  await expect(complete).toBeVisible();
  await complete.click();
  await expect(page.locator("button[aria-label='Reopen Playwright task']")).toBeVisible({ timeout: 15000 });
});
test("mobile navigation opens", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  await page.locator('header button[aria-label="Open menu"]').click({ force: true });
  await expect(page.getByRole("button", { name: "Teams" })).toBeVisible();
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
