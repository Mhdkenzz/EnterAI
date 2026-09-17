import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("workspace has no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["critical", "serious"].includes(violation.impact || ""))).toEqual([]);
});

test("agent hierarchy view and inspector have no serious accessibility violations", async ({ page, isMobile }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  if (isMobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Agents", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent hierarchy" })).toBeVisible();
  await page.getByLabel("VPs under CEO").fill("1");
  await page.getByRole("button", { name: "Save hierarchy" }).click();
  await expect(page.getByText("Chief Executive Officer", { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole("button", { name: "Save hierarchy" })).toBeEnabled();
  const treeResults = await new AxeBuilder({ page }).analyze();
  expect(treeResults.violations.filter((violation) => ["critical", "serious"].includes(violation.impact || ""))).toEqual([]);
  await page.getByRole("button", { name: /Chief Executive Officer/ }).first().click();
  await expect(page.getByRole("heading", { name: "Chief Executive Officer" })).toBeVisible();
  const inspectorResults = await new AxeBuilder({ page }).analyze();
  expect(inspectorResults.violations.filter((violation) => ["critical", "serious"].includes(violation.impact || ""))).toEqual([]);
});
