import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: a report's life after its plan is approved — generating, arriving,
 * being downloaded, and being deleted.
 *
 * `report-planning.spec.ts` owns the review loop; this one owns everything after
 * it. Sign-in and repository creation hit the real stack, the report endpoints
 * are intercepted, and the state machine is advanced from the test so each
 * assertion is about the page rather than about a timer.
 *
 * Two things here are covered nowhere else. A finished report offers a PPTX
 * button **only when the deck really exists** — reports generated before PPTX
 * existed have none, and offering the button would hand the reader a 404 under a
 * .pptx name. And a download is a blob fetched with the session's bearer token,
 * not a plain link: an <a href> would arrive unauthenticated, which no unit test
 * of the page can notice.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

// Just enough of a plan to make the review card render. Mirrors ReportPlan in
// src/api/reports.ts; a server-returned plan never has a blank source.
/** A database section must name the database it reads (#203). An empty list is a
 *  blank the reader cannot approve, so a fixture standing in for a *server* plan
 *  has to carry one. The id need not resolve: the editor prints a name only when
 *  `report-databases` knows it, and prints nothing rather than a raw id. */
const DB_ID = "11111111-1111-1111-1111-111111111111";

interface PlanSection {
  title: string;
  source: "database" | "documents";
  // A server-returned section ALWAYS carries these, because the response model
  // defaults them (#203 `databases`, #204 `columns`). The stub must too: the plan
  // editor reads `databases.length`, so a fixture without them throws and the
  // editor never mounts — which is exactly how five of these specs failed the
  // first time the suite was run after those cards. Production was never
  // affected; only this stub was stale.
  databases: string[];
  columns: string[];
  sub_question: string;
}
interface Plan {
  title: string;
  language: string;
  sections: PlanSection[];
}

interface Report {
  id: string;
  repository_id: string;
  prompt: string;
  status: string;
  error: string | null;
  created_at: string;
  has_pdf: boolean;
  has_pptx: boolean;
  schedule_id: string | null;
  plan: Plan | null;
  plan_rounds: number;
  can_rerender: boolean;
  /** The template snapshotted on the row, which the real API always sends.
   *  Omitting it from this harness left every stubbed report at `undefined`,
   *  and `undefined !== null` is exactly what keeps the panel's `noChoice`
   *  sentence out of the row — so a layout guard written against this shape
   *  covered the one report in which the panel says nothing at all. */
  template: string | null;
}

interface Harness {
  reports: Report[];
  downloads: string[];
  deletes: string[];
}

function report(overrides: Partial<Report> & { id: string }): Report {
  return {
    repository_id: "repo",
    prompt: "Quarterly revenue",
    status: "done",
    error: null,
    created_at: new Date("2026-08-04T10:00:00Z").toISOString(),
    has_pdf: true,
    has_pptx: true,
    schedule_id: null,
    plan: null,
    plan_rounds: 1,
    can_rerender: true,
    template: "kvarta",
    ...overrides,
  };
}

async function signIn(page: Page) {
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

async function stubReports(page: Page, initial: Report[]): Promise<Harness> {
  const harness: Harness = { reports: initial, downloads: [], deletes: [] };

  await page.route(/\/repositories\/[^/]+\/schedules(\?.*)?$/, (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );

  await page.route(/\/reports\/[^/]+\/download\?format=[a-z]+$/, async (route: Route) => {
    const url = route.request().url();
    // The token proves the page fetched this as an authenticated request rather
    // than navigating to a bare href, which would download an HTML 401.
    const auth = await route.request().headerValue("authorization");
    harness.downloads.push(`${url.slice(url.indexOf("/repositories"))}|auth=${auth !== null}`);
    await route.fulfill({
      status: 200,
      contentType: "application/pdf",
      body: Buffer.from("%PDF-1.7 stub"),
    });
  });

  await page.route(/\/repositories\/[^/]+\/reports\/[^/?]+$/, async (route: Route) => {
    if (route.request().method() !== "DELETE") {
      await route.fallback();
      return;
    }
    const url = route.request().url();
    const id = url.slice(url.lastIndexOf("/") + 1);
    harness.deletes.push(id);
    harness.reports = harness.reports.filter((r) => r.id !== id);
    await route.fulfill({ status: 204, body: "" });
  });

  await page.route(/\/repositories\/[^/]+\/reports(\?.*)?$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.reports),
    });
  });

  return harness;
}

async function openReports(page: Page): Promise<void> {
  const repoName = `Report life E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  await expect(page.locator("li.repo-item", { hasText: repoName })).toBeVisible();
  await page.getByRole("link", { name: "Reports" }).click();
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
}

test("a generating report becomes downloadable while the page stays open", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page, [
    report({
      id: "aaaa1111-1111-4111-8111-111111111111",
      status: "pending",
      has_pdf: false,
      has_pptx: false,
    }),
  ]);
  await openReports(page);

  const row = page.locator("li.report-item");
  await expect(row.locator("span.badge")).toHaveText("Pending");
  await expect(row.getByRole("status")).toHaveText("Generating…");
  // Nothing to download while it is still running — offering a button here hands
  // the reader a 404.
  await expect(row.getByRole("button", { name: "Download PDF" })).toBeHidden();

  harness.reports = [{ ...harness.reports[0], status: "processing" }];
  await expect(row.locator("span.badge")).toHaveText("Processing");

  harness.reports = [{ ...harness.reports[0], status: "done", has_pdf: true, has_pptx: true }];
  await expect(row.locator("span.badge")).toHaveText("Done");
  await expect(row.getByRole("status")).toBeHidden();

  // --- both formats, each fetched with the session's token ---
  await row.getByRole("button", { name: "Download PDF" }).click();
  await row.getByRole("button", { name: "Download PPTX" }).click();
  await expect(() => expect(harness.downloads).toHaveLength(2)).toPass();
  expect(harness.downloads[0]).toContain("/download?format=pdf");
  expect(harness.downloads[0]).toContain("auth=true");
  expect(harness.downloads[1]).toContain("/download?format=pptx");
  expect(harness.downloads[1]).toContain("auth=true");
});

test("a report with no deck offers no PPTX button", async ({ page }) => {
  await signIn(page);
  await stubReports(page, [
    report({ id: "bbbb2222-2222-4222-8222-222222222222", has_pptx: false }),
  ]);
  await openReports(page);

  const row = page.locator("li.report-item");
  await expect(row.locator("span.badge")).toHaveText("Done");
  await expect(row.getByRole("button", { name: "Download PDF" })).toBeVisible();
  await expect(row.getByRole("button", { name: "Download PPTX" })).toBeHidden();
});

test("a failed report shows its reason and offers nothing to download", async ({ page }) => {
  await signIn(page);
  await stubReports(page, [
    report({
      id: "cccc3333-3333-4333-8333-333333333333",
      status: "failed",
      error: 'relation "quotes" does not exist',
      has_pdf: false,
      has_pptx: false,
    }),
  ]);
  await openReports(page);

  const row = page.locator("li.report-item");
  await expect(row.locator("span.badge")).toHaveText("Failed");
  await expect(row.locator("span.report-error")).toHaveText('relation "quotes" does not exist');
  await expect(row.getByRole("button", { name: "Download PDF" })).toBeHidden();
  await expect(row.getByRole("status")).toBeHidden();
});

test("deleting a report asks first, and a cancelled dialog keeps the row", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page, [
    report({ id: "dddd4444-4444-4444-8444-444444444444", prompt: "Keep me" }),
    report({ id: "eeee5555-5555-4555-8555-555555555555", prompt: "Delete me" }),
  ]);
  await openReports(page);

  await expect(page.locator("li.report-item")).toHaveCount(2);

  // Dismissing the confirmation must delete nothing — the row is gone forever
  // and the artifact with it.
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toBe("Delete this report? This cannot be undone.");
    void dialog.dismiss();
  });
  await page
    .locator("li.report-item", { hasText: "Delete me" })
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(page.locator("li.report-item")).toHaveCount(2);
  expect(harness.deletes).toEqual([]);

  page.once("dialog", (dialog) => void dialog.accept());
  await page
    .locator("li.report-item", { hasText: "Delete me" })
    .getByRole("button", { name: "Delete" })
    .click();

  await expect(page.locator("li.report-item")).toHaveCount(1);
  await expect(page.locator("li.report-item")).toContainText("Keep me");
  expect(harness.deletes).toEqual(["eeee5555-5555-4555-8555-555555555555"]);
});

test("a failing report list explains itself instead of showing an empty history", async ({
  page,
}) => {
  await signIn(page);
  await stubReports(page, []);
  await page.route(/\/repositories\/[^/]+\/reports(\?.*)?$/, (route: Route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Database is unavailable." }),
    }),
  );
  await openReports(page);

  // "No reports yet" under a failed load reads as "you have none", which is a
  // different and wrong statement.
  await expect(page.locator("p.error")).toHaveText("Database is unavailable.");
  await expect(page.getByText("No reports yet. Request one above.")).toBeHidden();
});

/** The bounding boxes of the named descendants of a row, in the order asked for.
 *
 *  Measured in the browser: a class in the stylesheet makes a "not in html"
 *  check unfailable, and jsdom performs no layout at all. */
async function rectsIn(page: Page, selectors: string[]): Promise<DOMRect[]> {
  const row = page.locator("li.report-item").first();
  const rects = await row.evaluate(
    (li, sels) => sels.map((sel) => li.querySelector(sel)?.getBoundingClientRect() ?? null),
    selectors,
  );
  rects.forEach((rect, i) =>
    expect(rect, `no element matched ${selectors[i]} in the row`).not.toBeNull(),
  );
  return rects as DOMRect[];
}

/** Assert the given boxes are one line of controls rather than a stack of rows.
 *
 *  This does not require pixel-identical tops: under `align-items: center`, a
 *  correctly-centred 18px link and a 36px button on the *same* row naturally
 *  have different tops (both share a centre instead). What one row actually
 *  means is that the items' own bounding boxes are tightly stacked — their
 *  combined span is about as tall as the tallest one of them, not several
 *  line-heights of separate rows. */
function expectOneRow(rects: DOMRect[]): void {
  const top = Math.min(...rects.map((r) => r.top));
  const bottom = Math.max(...rects.map((r) => r.bottom));
  const tallest = Math.max(...rects.map((r) => r.height));
  // Four items stacked onto separate lines measured 127.19px span before the
  // fix; sharing one row keeps the span within a single line's height (the
  // tallest item, 36px, plus slack for sub-pixel layout and line spacing).
  expect(
    bottom - top,
    `expected one row (span ~= tallest item's height), got span ${bottom - top}px over rects ${JSON.stringify(rects)}`,
  ).toBeLessThan(tallest + 16);
}

test("a finished report's actions share one row", async ({ page }) => {
  // The defect: `.report-design` claimed `flex: 1 1 100%` even collapsed, so the
  // download buttons, the design triggers and delete each took a line of their own.
  await signIn(page);
  await stubReports(page, [report({ id: "ffff6666-6666-4666-8666-666666666666" })]);
  await openReports(page);

  expectOneRow(
    await rectsIn(page, [
      ".report-downloads button",
      ".report-design button",
      "a.report-edit-design",
      "button.report-delete",
    ]),
  );
});

/** The visual reach of the submit button's own shadow, in px.
 *
 *  `button[type="submit"]` carries `box-shadow: 0 6px 18px -8px`, so it paints
 *  6px lower, blurred by 18px, shrunk by 8px — about 16px of ink below its own
 *  bottom edge. That number is what makes "touching" and "overlapping" the same
 *  thing here: with the boxes flush, the button's shadow lands *on* the card
 *  below and the button reads as sitting on top of it, which is exactly the
 *  complaint. A gap that does not clear the shadow does not fix the report. */
const SHADOW_REACH = 16;

for (const width of [1280, 768]) {
  test(`the plan button does not sit on the first report row at ${width}px`, async ({ page }) => {
    // The defect: `.report-request` had no rule in `index.css` — it never has,
    // in the file's whole history — and `.report-list` sets `margin: 0`, so the
    // form's bottom edge and the list's top edge were the *same* coordinate.
    // Measured at both widths before the fix: submit bottom 259.20 against row
    // top 259.20 at 1280, and 295.20 against 295.20 at 768. Zero, not "tight".
    //
    // Both widths, because 768 is where the row wraps to two lines; the gap has
    // to survive that, and a fix that only worked while the row was one line
    // would pass a single-width check.
    //
    // NOT a regression from #180, though the card assumed one: the same probe
    // run against v43 (`f7d9c95`) reports the identical flush coordinates, and
    // `display: contents` on `.report-design` only ever affected the row's own
    // children. This is original, and older than the card that suspected it.
    await signIn(page);
    await stubReports(page, [report({ id: "ffff5555-5555-4555-8555-555555555555" })]);
    await openReports(page);

    const row = page.locator("li.report-item").first();
    await expect(row).toBeVisible();

    // Resized *after* navigating, not before: at 768 the sidebar's "Repositories"
    // link sits outside the viewport and `openReports` cannot click it. The
    // subject here is the reports page's own layout at that width, not whether
    // the nav is reachable there — which is a different card's problem.
    await page.setViewportSize({ width, height: 900 });
    await expect(row).toBeVisible();

    // Before measuring, not after: a `display: none` button reports an all-zero
    // rect, which would put its "bottom" at 0 and hand this test a gap of a few
    // hundred px. The check that the button is on screen is what stops the
    // assertion from passing loudest when the button is missing entirely.
    await expect(page.locator("form.report-request button[type=submit]")).toBeVisible();

    const measured = await page.evaluate(() => {
      const button = document.querySelector("form.report-request button[type=submit]");
      const item = document.querySelector("li.report-item");
      if (button === null || item === null) return null;
      return {
        // Returned and asserted because a measurement taken before the resize
        // has settled would describe the *previous* width — and since the wider
        // layout also passes, that would quietly turn this into two runs of the
        // same 1280px case rather than the narrow one it claims to be.
        innerWidth: window.innerWidth,
        gap: item.getBoundingClientRect().top - button.getBoundingClientRect().bottom,
      };
    });

    expect(measured, "neither the plan button nor a report row was in the page").not.toBeNull();
    expect(measured!.innerWidth, "the viewport had not resized when the gap was read").toBe(width);
    expect(
      measured!.gap,
      `the button's shadow reaches ~${SHADOW_REACH}px below it, so a ${measured!.gap}px gap still paints the button onto the row at ${width}px`,
    ).toBeGreaterThanOrEqual(SHADOW_REACH);
  });
}

/** The visual reach of the plan card's own bottom-most ink, in px.
 *
 *  `.plan-editor` — the raised card filling `.plan-card` — carries
 *  `box-shadow: var(--shadow)` — `0 8px 24px -12px` — so it paints 8px lower,
 *  blurred by 24px, shrunk by 12px: about 20px of ink below its own bottom
 *  edge, across the full width. Same arithmetic as SHADOW_REACH above. */
const PLAN_SHADOW_REACH = 20;

test("the plan card does not sit on the first report row", async ({ page }) => {
  // The defect: `.plan-card` has no rule in `index.css` — the selector has
  // never appeared in the file — so when a plan is open, the card's bottom
  // edge and the report list's top edge are the same coordinate, and the
  // editor card's shadow paints onto the first row. Same family as the
  // `.report-request` case above: a block that owns no bottom margin, found
  // while verifying #187 and deliberately left to its own card (#198).
  await signIn(page);
  await stubReports(page, [
    report({
      id: "ffff8888-8888-4888-8888-888888888888",
      status: "awaiting_approval",
      plan: {
        title: "Quarterly market review",
        language: "en",
        sections: [
          {
            title: "Revenue by city",
            source: "database",
            databases: [DB_ID],
            columns: [],
            sub_question: "how did revenue move?",
          },
        ],
      },
      has_pdf: false,
      has_pptx: false,
      can_rerender: false,
      template: null,
    }),
    report({ id: "ffff9999-9999-4999-8999-999999999999" }),
  ]);
  await openReports(page);

  // Visible before measured: a missing card or row reports an all-zero rect,
  // which would hand this test a gap of a few hundred px exactly when the
  // thing under test is gone.
  const card = page.locator("div.plan-card");
  const row = page.locator("li.report-item").first();
  await expect(card).toBeVisible();
  await expect(row).toBeVisible();

  const measured = await page.evaluate(() => {
    const card = document.querySelector("div.plan-card");
    const item = document.querySelector("li.report-item");
    if (card === null || item === null) return null;
    return { gap: item.getBoundingClientRect().top - card.getBoundingClientRect().bottom };
  });

  expect(measured, "neither the plan card nor a report row was in the page").not.toBeNull();
  expect(
    measured!.gap,
    `the editor card's shadow reaches ~${PLAN_SHADOW_REACH}px below it, so a ${measured!.gap}px gap still paints it onto the first row`,
  ).toBeGreaterThanOrEqual(PLAN_SHADOW_REACH);
});

/** What the heading itself grants the content *below* it: `h2` declares
 *  `margin: 0 0 0.9rem` — 14.4px down, nothing up. The section above a heading
 *  must grant it at least as much, or the heading reads as the last row's
 *  caption rather than the next section's title. */
const HEADING_GAP = 14;

test("the report list does not sit on the nightly-schedules heading", async ({ page }) => {
  // The defect: `.report-list` declares `margin: 0`, and the `<h2>` that
  // follows it owns space below only — so the list's last row and the
  // "Nightly schedules" heading were the same coordinate. This is the second of
  // the two flush edges.
  await signIn(page);
  await stubReports(page, [
    report({ id: "ffffaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }),
    report({ id: "ffffbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }),
  ]);
  await openReports(page);

  const lastRow = page.locator("li.report-item").last();
  const heading = page.getByRole("heading", { name: "Nightly schedules" });
  await expect(lastRow).toBeVisible();
  await expect(heading).toBeVisible();

  const measured = await page.evaluate(() => {
    const items = document.querySelectorAll("li.report-item");
    const last = items[items.length - 1];
    const headings = Array.from(document.querySelectorAll("h2"));
    const heading = headings.find((h) => h.textContent === "Nightly schedules");
    if (last === undefined || heading === undefined) return null;
    return { gap: heading.getBoundingClientRect().top - last.getBoundingClientRect().bottom };
  });

  expect(
    measured,
    "the last report row or the schedules heading was not in the page",
  ).not.toBeNull();
  expect(
    measured!.gap,
    `the heading grants ${HEADING_GAP}px to the content below it; the ${measured!.gap}px gap above it makes it read as the last row's caption`,
  ).toBeGreaterThanOrEqual(HEADING_GAP);
});

test("a report the panel has a sentence about keeps its actions on one row", async ({ page }) => {
  // The shape the first case cannot reach. A report that cannot be rebuilt makes
  // the panel render a full-width note, and a note that sorts *between* the
  // triggers and Delete pushes Delete onto a second line — measured at a 166px
  // row against 84.8px for the case above. The note is a sentence and keeps its
  // own line; `order` puts that line below the controls instead of inside them.
  await signIn(page);
  await stubReports(page, [
    report({ id: "ffff7777-7777-4777-8777-777777777777", can_rerender: false }),
  ]);
  await openReports(page);

  const note = page.locator("li.report-item p.report-note");
  await expect(note).toHaveText("This report cannot be rebuilt from storage.");

  // No `a.report-edit-design`: structural editing is a rebuild, so a report that
  // cannot be rebuilt is not offered it.
  const [download, design, del, sentence] = await rectsIn(page, [
    ".report-downloads button",
    ".report-design button",
    "button.report-delete",
    "p.report-note",
  ]);
  expectOneRow([download, design, del]);
  // Below the controls, not above and not between them.
  expect(
    sentence.top,
    `expected the note beneath the action row, note top ${sentence.top}px vs delete bottom ${del.bottom}px`,
  ).toBeGreaterThanOrEqual(del.bottom);
});

test("a failed template apply reports itself on its own line under the actions", async ({
  page,
}) => {
  // The panel's error is a *child of the row* now that `.report-design` is
  // `display: contents`, and the real shape is `li.report-item >
  // div.report-design > p.error`: `display: contents` removes the box, not the
  // node, so a `.report-item > .error` rule matches nothing at all and the
  // sentence lays out as an ordinary item wedged between the triggers and
  // Delete. Nothing measured this line until now.
  await signIn(page);
  await stubReports(page, [report({ id: "ffff8888-8888-4888-8888-888888888888" })]);
  await page.route(/\/report-templates$/, (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { key: "kvarta", name: "Кварта", paper: "#fff", ink: "#000", accent: "#1a4a75" },
      ]),
    }),
  );
  await page.route(/\/reports\/[^/]+\/design$/, (route: Route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "no" }),
    }),
  );
  await openReports(page);

  await page.locator("li.report-item").getByRole("button", { name: "Design template" }).click();
  await page.getByRole("button", { name: "Кварта" }).click();
  // The gallery covers the page and renders its own copy of the message; the
  // row's own line only appears once it closes.
  await page.getByRole("button", { name: "Close" }).click();

  const failure = page.locator("li.report-item p.error");
  await expect(failure).toHaveText("Could not apply the template.");

  const [download, design, edit, del, message] = await rectsIn(page, [
    ".report-downloads button",
    ".report-design button",
    "a.report-edit-design",
    "button.report-delete",
    "p.error",
  ]);
  expectOneRow([download, design, edit, del]);
  expect(
    message.top,
    `expected the failure beneath the action row, error top ${message.top}px vs delete bottom ${del.bottom}px`,
  ).toBeGreaterThanOrEqual(del.bottom);
});
