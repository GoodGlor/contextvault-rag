import { test, expect } from "@playwright/test";

/**
 * End-to-end: an admin signs in and manages a repository against the *real* stack
 * (backend + Postgres + frontend, brought up by `./dev.sh`). Exercises the full
 * chain — JWT auth, the admin nav, repository creation, and the live list — through
 * a real browser.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

async function signIn(page: import("@playwright/test").Page) {
  // The app defaults to Ukrainian; pin the browser to English so the English-string
  // assertions below hold. (The language switch itself is covered by unit tests.)
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  // Unauthenticated visitors are bounced to the login screen.
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Lands on the query home.
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

test("admin signs in, navigates the admin nav, and creates a repository", async ({ page }) => {
  await signIn(page);

  // Navigate via the (now spaced) admin nav.
  await page.getByRole("link", { name: "Repositories" }).click();
  await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();

  // Create a uniquely-named repository so reruns never collide.
  const name = `E2E Vault ${Date.now()}`;
  await page.getByLabel("Repository name").fill(name);
  await page.getByLabel("Description").fill("created by the e2e test");
  await page.getByRole("button", { name: "Create repository" }).click();

  // It appears in the live list, flagged as not-yet-configured.
  const row = page.locator("li.repo-item", { hasText: name });
  await expect(row).toBeVisible();
  await expect(row.getByText("Not configured")).toBeVisible();

  // Global provider keys: opening a repo's config must NEVER ask for an API key —
  // keys live in the Providers tab, so the config only ever offers a provider + model.
  // (The "no provider verified yet" empty state is covered by the component tests,
  // which can pin the global provider state; e2e shares one stack, so it asserts only
  // the state-independent invariant here.)
  await row.getByRole("button", { name: "Configure" }).click();
  await expect(row.getByLabel("API key")).toHaveCount(0);
  // Close the panel again so it doesn't interfere with later assertions.
  await row.getByRole("button", { name: "Configure" }).click();

  // The Providers tab lists every provider with its key status (all unset on a fresh
  // stack). Rendering it end-to-end proves the new nav entry + page + API are wired.
  await page.getByRole("link", { name: "Providers" }).click();
  await expect(page.getByRole("heading", { name: "Providers" })).toBeVisible();
  await expect(page.getByText("OpenAI")).toBeVisible();
  await expect(page.getByText("Anthropic")).toBeVisible();

  // Users page: create an invite and copy its link. Grant clipboard so the copy
  // succeeds, then assert the button confirms with "Copied".
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.getByRole("link", { name: "Users" }).click();
  await expect(page.getByRole("heading", { name: "Invite a user" })).toBeVisible();
  const invite = page.getByRole("region", { name: "Invite a user" });
  await invite.getByLabel("Username").fill(`e2e-invitee-${Date.now()}`);
  await invite.getByRole("button", { name: "Send invite" }).click();
  await expect(page.getByRole("button", { name: "Copy" })).toBeVisible();
  await page.getByRole("button", { name: "Copy" }).click();
  await expect(page.getByRole("button", { name: "Copied" })).toBeVisible();
  // The clipboard holds the full accept-invite URL.
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  expect(clip).toContain("/accept-invite?token=");

  // The other admin surfaces are reachable and render.
  await page.getByRole("link", { name: "Insights" }).click();
  await expect(page.getByRole("heading", { name: "Insights" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
});
