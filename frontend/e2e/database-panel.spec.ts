import { test, expect, type Locator, type Page } from "@playwright/test";

/**
 * End-to-end: the connected-database panel's controls do not overlap (#211).
 *
 * **This spec exists because no other kind of test can see the defect.** jsdom
 * performs no layout, so every `getBoundingClientRect` in a vitest run is zero —
 * a unit test can assert the buttons are *grouped*, which
 * `DatabasePanel.test.tsx` does, but never that they are *apart*. The bug the
 * owner reported was purely geometric: a textarea and four buttons rendered as
 * bare siblings with no rules at all, and ran into each other.
 *
 * It drives the real stack, and connects the app to **its own Postgres** — the
 * one `dev.sh` already brought up. That is not a shortcut: the panel only shows
 * the description block once a connection exists, and a connection is only
 * created after the backend genuinely reaches the database.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

// The database `dev.sh` runs against. Connecting the app to it is enough to
// produce a real, introspectable connection without inventing a second server.
const LOCAL_DB = {
  name: `E2E panel ${Date.now()}`,
  host: "localhost",
  port: "5432",
  database: "contextvault",
  username: "contextvault",
  password: "contextvault",
};

async function signIn(page: Page) {
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

/**
 * The shortest distance between two elements' rectangles, in px. 0 when they
 * touch or intersect.
 *
 * **Non-intersection is the wrong test, and measuring proved it.** With the
 * defect in place these controls did not mathematically overlap — they abutted
 * exactly: `Describe`'s right edge and `Save description`'s left edge were both
 * at x=405.25, and each button's bottom was the next one's top. A strict
 * intersection check passed on the broken layout, which is the whole reason
 * this helper measures separation instead.
 */
async function separation(a: Locator, b: Locator): Promise<number> {
  const [ra, rb] = [await a.boundingBox(), await b.boundingBox()];
  // A missing element has no box. Treating that as "infinitely far apart" would
  // let this pass on a panel that rendered nothing, so it is a failure here.
  if (ra === null || rb === null) throw new Error("an element under test has no box");
  const dx = Math.max(0, ra.x - (rb.x + rb.width), rb.x - (ra.x + ra.width));
  const dy = Math.max(0, ra.y - (rb.y + rb.height), rb.y - (ra.y + ra.height));
  // Diagonal neighbours are clear on both axes; the distance that matters is the
  // larger one, since either alone is enough to keep them apart.
  return Math.max(dx, dy);
}

/**
 * Derived from measurement, not chosen. With the block unstyled the gaps between
 * these controls measured 0px, 0px, 0px and 3px; with the fix they measure 8px,
 * 9.6px, 13.6px and 13.6px. Six lies above every broken value and below every
 * fixed one, so it fails the defect and passes the fix with margin either side.
 */
const MIN_GAP = 6;

test.describe("the connected database panel", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page);
    await page.goto("/admin/data?tab=database");
    await expect(page.getByRole("heading", { name: "Data" })).toBeVisible();

    // A repository with no connection shows the setup form directly; one with
    // connections shows the list and an "Add database" button first.
    const add = page.getByRole("button", { name: "Add database" });
    if (await add.isVisible().catch(() => false)) await add.click();

    // Scoped to the setup form: the page also carries the repository switcher,
    // whose accessible name collides with "Name" and "Port" at the page level.
    const form = page.locator("form.database-setup");
    await form.getByLabel("Name", { exact: true }).fill(LOCAL_DB.name);
    await form.getByLabel("Host").fill(LOCAL_DB.host);
    await form.getByLabel("Port").fill(LOCAL_DB.port);
    await form.getByLabel("Database", { exact: true }).fill(LOCAL_DB.database);
    await form.getByLabel("Username").fill(LOCAL_DB.username);
    await form.getByLabel("Password").fill(LOCAL_DB.password);
    await form.getByRole("button", { name: "Connect" }).click();

    // The connection's own panel — the block this card is about.
    await expect(page.getByRole("heading", { name: LOCAL_DB.name })).toBeVisible();
  });

  test("the description field and every button around it are clear of each other", async ({
    page,
  }) => {
    const textarea = page.getByLabel("Description");
    const describe = page.getByRole("button", { name: "Describe" });
    const save = page.getByRole("button", { name: "Save description" });
    const introspect = page.getByRole("button", { name: "Introspect" });
    const remove = page.getByRole("button", { name: "Delete connection" });

    for (const element of [textarea, describe, save, introspect, remove]) {
      await expect(element).toBeVisible();
    }

    const named: [string, Locator][] = [
      ["description", textarea],
      ["Describe", describe],
      ["Save description", save],
      ["Introspect", introspect],
      ["Delete connection", remove],
    ];
    for (let i = 0; i < named.length; i += 1) {
      for (let j = i + 1; j < named.length; j += 1) {
        const [nameA, a] = named[i];
        const [nameB, b] = named[j];
        const gap = await separation(a, b);
        expect(gap, `${nameA} sits ${gap}px from ${nameB}`).toBeGreaterThanOrEqual(MIN_GAP);
      }
    }
  });

  test("the controls stay apart on a narrow viewport", async ({ page }) => {
    // The row wraps rather than colliding. 420px is narrower than the two
    // description buttons laid side by side.
    await page.setViewportSize({ width: 420, height: 900 });
    const describe = page.getByRole("button", { name: "Describe" });
    const save = page.getByRole("button", { name: "Save description" });
    await expect(describe).toBeVisible();
    const gap = await separation(describe, save);
    expect(gap, `Describe sits ${gap}px from Save description`).toBeGreaterThanOrEqual(MIN_GAP);
  });

  test.afterEach(async ({ page }) => {
    // Leave the repository as it was found: this spec creates a real connection.
    const remove = page.getByRole("button", { name: "Delete connection" });
    page.once("dialog", (dialog) => dialog.accept());
    if (await remove.isVisible().catch(() => false)) await remove.click();
  });
});
