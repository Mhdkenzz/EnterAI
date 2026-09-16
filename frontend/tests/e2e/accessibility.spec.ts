import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("workspace has no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((violation) => ["critical", "serious"].includes(violation.impact || ""))).toEqual([]);
});
