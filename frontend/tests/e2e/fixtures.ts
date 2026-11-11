import { test as base } from "@playwright/test";

// New sessions open the CEO welcome panel. Treat closing that one-time panel
// like a user dismissing a welcome, so unrelated flows can interact with the
// workspace. Later inspector openings in the same test are left untouched.
export const test = base.extend({
  page: async ({ page }, use) => {
    await page.addLocatorHandler(
      page.getByRole("button", { name: "Close agent inspector" }),
      // This is test setup for the one-time welcome panel, not a test of the
      // close button. Dispatch the same click event directly so mobile viewport
      // hit testing does not make the setup handler block the interaction under test.
      async (close) => { await close.evaluate((button) => (button as HTMLButtonElement).click()); },
      { times: 1 },
    );
    await use(page);
  },
});
