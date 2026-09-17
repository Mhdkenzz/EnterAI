import { expect, test } from "@playwright/test";

test("admin configures the agent hierarchy, inspects an agent, and chats with it", async ({ page, isMobile }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your workspace" })).toBeVisible();

  if (isMobile) await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Agents", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent hierarchy" })).toBeVisible();

  await page.getByLabel("VPs under CEO").fill("1");
  await page.getByLabel("Directors per VP").fill("1");
  await page.getByLabel("Senior managers per Director").fill("0");
  await page.getByLabel("Workers per Senior Manager").fill("0");
  await page.getByRole("button", { name: "Save hierarchy" }).click();

  await expect(page.getByText("Chief Executive Officer", { exact: true }).first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText("Vice President", { exact: true })).toBeVisible();
  await expect(page.getByText("Director", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Chief Executive Officer/ }).first().click();
  await expect(page.getByRole("heading", { name: "Chief Executive Officer" })).toBeVisible();
  await expect(page.getByText("Direct reports (1)")).toBeVisible();
  await expect(page.getByText("Idle -- no active task.")).toBeVisible();

  const question = `What are you working on, ${test.info().project.name} ${Date.now()}?`;
  await page.getByPlaceholder(/Message Chief Executive Officer/).fill(question);
  await page.getByRole("button", { name: /Send message to Chief Executive Officer/ }).click();
  await expect(page.getByText(question)).toBeVisible();

  await page.getByRole("button", { name: "Close agent inspector" }).click();
  await expect(page.getByRole("heading", { name: "Chief Executive Officer" })).toHaveCount(0);
});

test("agent hierarchy is reachable and usable on mobile", async ({ page, isMobile }) => {
  test.skip(!isMobile, "desktop nav already covered above");
  await page.goto("/");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Open menu", exact: true }).click();
  await page.getByRole("complementary").getByRole("button", { name: "Agents", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Agent hierarchy" })).toBeVisible();
  await expect(page.getByRole("complementary")).toBeHidden();
});
