import { expect, test } from "@playwright/test";

test("a new session can dismiss the CEO welcome and use the workspace", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  const close = page.getByRole("button", { name: "Close agent inspector" });
  await expect(close).toBeVisible();
  await close.click();
  await expect(close).toHaveCount(0);
});
