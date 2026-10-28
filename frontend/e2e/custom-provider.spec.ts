import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: the custom (OpenAI-compatible) provider — a keyless local endpoint.
 *
 * Mirrors providers.spec's philosophy. Auth and repository creation hit the real
 * stack; the calls that would otherwise need a live local server (verifying the
 * endpoint, listing its models) are intercepted at the browser boundary, and because
 * nothing is really stored, the provider-status and llm-config reads are stubbed to
 * reflect the verified state. It proves, in a real browser, that:
 *   1. the Providers tab renders a custom row with a Base URL field, an *optional*
 *      key, and the "embeddings still use Gemini" note,
 *   2. saving with only a base URL (no key) flips it to Verified and sends
 *      { api_key: null, base_url } — a key is never required, and
 *   3. a repository configured against the custom provider takes a *free-typed*
 *      model id (not a fixed <select>), and the saved config is just
 *      { provider, model }.
 */

const ADMIN = { username: "admin", password: "adminpass123" };
const BASE_URL = "http://localhost:11434/v1";

async function signIn(page: Page) {
  // The app defaults to Ukrainian; pin English so the string assertions below hold.
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

test("configure a keyless custom endpoint, then pick a repo's free-typed model", async ({
  page,
}) => {
  await signIn(page);

  // Mutable "is custom configured?" — flipped true once its base URL is saved, so the
  // repo config later sees it as usable. Everything provider/key/model is stubbed.
  let customConfigured = false;
  const providerPuts: Array<{ url: string; body: Record<string, unknown> }> = [];
  const configPuts: Array<{ provider: string; model: string; api_key?: string }> = [];

  const providerRows = () =>
    ["anthropic", "openai", "gemini", "openrouter", "custom"].map((p) => ({
      provider: p,
      configured: p === "custom" && customConfigured,
      verified: p === "custom" && customConfigured,
      api_key_masked: null,
      base_url: p === "custom" && customConfigured ? BASE_URL : null,
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
    customConfigured = true;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        provider: "custom",
        configured: true,
        verified: true,
        api_key_masked: null,
        base_url: BASE_URL,
      }),
    });
  });
  await page.route(/\/repositories\/[^/]+\/llm-models$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: ["llama3.1:8b", "qwen2.5:14b"] }),
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

  // --- Providers tab: the custom row shows a Base URL field, an optional key, and
  // the Gemini-embeddings note. Save with only a base URL — it flips to Verified. ---
  await page.getByRole("link", { name: "Providers" }).click();
  await expect(page.getByRole("heading", { name: "Providers" })).toBeVisible();
  const customRow = page.locator("li.provider-item", {
    hasText: "Custom (local / self-hosted)",
  });
  await expect(customRow.getByLabel("Base URL")).toBeVisible();
  await expect(customRow.getByLabel("API key (optional)")).toBeVisible();
  await expect(customRow.getByText(/document embedding still uses gemini/i)).toBeVisible();

  await customRow.getByLabel("Base URL").fill(BASE_URL);
  await customRow.getByRole("button", { name: "Save key" }).click();
  await expect(customRow.locator("span.badge", { hasText: "Verified" })).toBeVisible();

  // The save carried the base URL and NO key.
  expect(providerPuts).toHaveLength(1);
  expect(providerPuts[0].url).toContain("/admin/providers/custom");
  expect(providerPuts[0].body).toEqual({ api_key: null, base_url: BASE_URL });

  // --- Repositories tab: create a repo, pick the custom provider, free-type a model. ---
  const repoName = `Custom E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  const row = page.locator("li.repo-item", { hasText: repoName });
  await expect(row).toBeVisible();

  await row.getByRole("button", { name: "Configure" }).click();
  await row.getByLabel("Provider").selectOption("custom");

  // For a custom endpoint the model is a *free-text* input (local model names are
  // arbitrary), not a fixed <select> — type an id the "catalogue" never listed.
  const modelInput = row.getByLabel("Model");
  await expect(modelInput).toBeVisible();
  await modelInput.fill("my-local-model");
  await row.getByRole("button", { name: "Save configuration" }).click();
  await expect(row.getByText(/saved/i)).toBeVisible();

  // The saved config carries only { provider, model } — no key.
  expect(configPuts).toHaveLength(1);
  expect(configPuts[0]).toEqual({ provider: "custom", model: "my-local-model" });
  expect(configPuts[0].api_key).toBeUndefined();
});
