import { expect, test } from "@playwright/test";
test("core workspace flows stay interactive", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();
  await page.locator("button[aria-label^='Complete']").first().click();
  await expect(page.locator("button[aria-label^='Reopen']").first()).toBeVisible();
  await page.getByRole("button", { name: "New project" }).click();
  await page.getByLabel("Project name").fill("Playwright project");
  await page.getByLabel("Code").fill("PW");
  await page.getByRole("button", { name: "Create project" }).click();
  await expect(page.getByRole("heading", { name: "Playwright project" })).toBeVisible();
  await page.getByRole("button", { name: "New task" }).click();
  await page.getByLabel("Title").fill("Playwright task");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByText("Playwright task")).toBeVisible();
});
test("mobile navigation opens", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Open menu" }).click();
  await expect(page.getByRole("button", { name: "Teams" })).toBeVisible();
});
