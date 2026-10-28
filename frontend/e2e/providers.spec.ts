import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: global provider keys + picking a repo's model (redesign).
 *
 * The flow is: set one API key per provider in the Providers tab (verified on save),
 * then configure a repository by choosing a model from a provider that has a key — no
 * key entry on the repo. Auth and repo creation hit the real stack; the calls that
 * would otherwise need a live provider (verifying a key, listing models) are
 * intercepted at the browser boundary and, because the key is never really stored,
 * the provider-status and llm-config calls are stubbed to reflect the verified state.
 * It proves, in a real browser, that:
 *   1. saving a provider key flips it to "Verified",
 *   2. the repo config asks for NO key — only a provider + model, and
 *   3. the saved config carries just { provider, model }.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

async function signIn(page: Page) {
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

test("set a provider key, then pick a repo's model without entering a key", async ({ page }) => {
  await signIn(page);

  // Mutable "is OpenAI verified?" — flipped true once its key is saved, so the repo
  // config later sees it as usable. Everything provider/key/model is stubbed here.
  let openaiVerified = false;
  const providerPuts: Array<{ url: string; body: unknown }> = [];
  const configPuts: Array<{ provider: string; model: string; api_key?: string }> = [];

  const providerRows = () =>
    ["anthropic", "openai", "gemini", "openrouter"].map((p) => ({
      provider: p,
      configured: p === "openai" && openaiVerified,
      verified: p === "openai" && openaiVerified,
      api_key_masked: p === "openai" && openaiVerified ? "sk-…•••1234" : null,
    }));

  await page.route(/\/admin\/providers$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(providerRows()),
    });
  });
  await page.route(/\/admin\/providers\/[^/]+$/, async (route: Route) => {
    providerPuts.push({ url: route.request().url(), body: route.request().postDataJSON() });
    openaiVerified = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        provider: "openai",
        configured: true,
        verified: true,
        api_key_masked: "sk-…•••1234",
      }),
    });
  });
  await page.route(/\/repositories\/[^/]+\/llm-models$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: ["gpt-4o", "gpt-4o-mini"] }),
    });
  });
  await page.route(/\/repositories\/[^/]+\/llm-config$/, async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      configPuts.push(body);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ provider: body.provider, model: body.model, configured: true }),
      });
      return;
    }
    // GET: reflect the current model choice (unconfigured until the PUT happens).
    const last = configPuts[configPuts.length - 1];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        last
          ? { provider: last.provider, model: last.model, configured: true }
          : { provider: null, model: null, configured: false },
      ),
    });
  });

  // --- Providers tab: enter and save the OpenAI key; it flips to Verified. ---
  await page.getByRole("link", { name: "Providers" }).click();
  await expect(page.getByRole("heading", { name: "Providers" })).toBeVisible();
  const openaiRow = page.locator("li.provider-item", { hasText: "OpenAI" });
  await openaiRow.getByLabel("API key").fill("sk-test-key-123");
  await openaiRow.getByRole("button", { name: "Save key" }).click();
  await expect(openaiRow.locator("span.badge", { hasText: "Verified" })).toBeVisible();

  // The save carried the key, to the OpenAI provider. Since the custom-provider work,
  // the payload always includes base_url (null for a cloud provider like OpenAI).
  expect(providerPuts).toHaveLength(1);
  expect(providerPuts[0].url).toContain("/admin/providers/openai");
  expect(providerPuts[0].body).toEqual({ api_key: "sk-test-key-123", base_url: null });

  // --- Repositories tab: create a repo and pick a model from OpenAI. ---
  const repoName = `Config E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  const row = page.locator("li.repo-item", { hasText: repoName });
  await expect(row).toBeVisible();

  await row.getByRole("button", { name: "Configure" }).click();
  // The repo config asks for NO key — just provider + model.
  await expect(row.getByLabel("API key")).toHaveCount(0);
  await row.getByLabel("Provider").selectOption("openai");

  // The model is a single <select>, auto-loaded from the provider's (global) key.
  const modelSelect = row.getByLabel("Model");
  await expect(modelSelect).toBeVisible();
  await modelSelect.selectOption("gpt-4o-mini");
  await row.getByRole("button", { name: "Save configuration" }).click();
  await expect(row.getByText(/saved/i)).toBeVisible();

  // The saved config carries only { provider, model } — no key.
  expect(configPuts).toHaveLength(1);
  expect(configPuts[0]).toEqual({ provider: "openai", model: "gpt-4o-mini" });
  expect(configPuts[0].api_key).toBeUndefined();
});
