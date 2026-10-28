import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

/**
 * End-to-end: an admin adds sources to a fresh repository, against the *real* stack
 * (backend + Postgres + frontend, brought up by `./dev.sh`). Covers the web-link flow
 * and — since images are now OCR'd by the repo's configured vision model — that an
 * image upload to a repo with no model configured is blocked with a clear message
 * (the real 409 gate; a live OCR run isn't deterministic, so it stays in pytest).
 * Run with the stack up: `npm run test:e2e`.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

// A 1x1 transparent PNG — a valid image. The repo has no configured model, so the
// upload must be blocked before any OCR is attempted.
const BLANK_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

async function signIn(page: Page): Promise<void> {
  // The app defaults to Ukrainian; pin the browser to English so the English-string
  // assertions below hold. (The language switch itself is covered by unit tests.)
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  // Unauthenticated visitors are bounced to the login screen.
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

async function createRepository(page: Page, name: string): Promise<void> {
  await page.getByRole("link", { name: "Repositories" }).click();
  await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
  await page.getByLabel("Repository name").fill(name);
  await page.getByLabel("Description").fill("created by the sources e2e test");
  await page.getByRole("button", { name: "Create repository" }).click();
  await expect(page.locator("li.repo-item", { hasText: name })).toBeVisible();
  // The sidebar repo switcher loads its list once at app mount; reload so the just-
  // created repo appears there (the Data page reads the repo from that switcher).
  await page.reload();
}

test("admin adds an image (OCR) source and a web-link source", async ({ page }) => {
  await signIn(page);

  // A uniquely-named repository so reruns never collide.
  const repoName = `E2E Sources ${Date.now()}`;
  await createRepository(page, repoName);

  // Go to the Data page (which absorbed the former Sources page — its Documents &
  // web tab is the default) and select the repository we just made in the switcher.
  await page.getByRole("link", { name: "Data" }).click();
  await expect(page.getByRole("heading", { name: "Data" })).toBeVisible();
  await page.getByLabel("Repository").selectOption({ label: repoName });

  // The OCR helper note and the web-link form are present on the page.
  await expect(page.getByText(/only text visible in the image is captured/i)).toBeVisible();
  await expect(page.getByLabel("Web link")).toBeVisible();

  // --- Web-link source: adding a URL creates a row tagged `web` that links out. ---
  const url = "https://example.com/article";
  await page.getByLabel("Web link").fill(url);
  await page.getByRole("button", { name: "Add link" }).click();

  const webRow = page.locator("li.source-item", { hasText: url });
  await expect(webRow).toBeVisible();
  await expect(webRow.locator("span.badge.kind-web")).toBeVisible();
  await expect(webRow.getByRole("link", { name: url })).toHaveAttribute("href", url);

  // --- Image source on an unconfigured repo: images are read by the repo's own
  // vision model, so with no model configured the upload is blocked with a clear,
  // actionable message (the real 409 gate) and no source row is created. ---
  // `exact` so this doesn't also match the "Documents & web" tabpanel's label.
  await page.getByLabel("Documents", { exact: true }).setInputFiles({
    name: "blank.png",
    mimeType: "image/png",
    buffer: BLANK_PNG,
  });
  await page.getByRole("button", { name: "Upload" }).click();

  await expect(
    page.getByText(/configure a model for this repository before uploading images/i),
  ).toBeVisible();
  await expect(page.locator("li.source-item", { hasText: "blank.png" })).toHaveCount(0);
});
