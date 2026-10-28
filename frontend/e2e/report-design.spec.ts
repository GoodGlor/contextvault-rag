import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: choosing a report's design template.
 *
 * This is the first e2e that exercises **re-render at all** — the gap standing
 * open since step 1, which existed because `can_rerender` was never declared in
 * the SPA and so no UI consumed it.
 *
 * What only this test can see: the three calls happen in the right order
 * against the real router, and the preview iframe is fed a blob fetched with the
 * session's bearer token. An <iframe src> pointed straight at the download URL
 * would arrive unauthenticated, and no unit test of the panel can notice that.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

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
  plan: null;
  plan_rounds: number;
  can_rerender: boolean;
  template: string | null;
}

interface TemplateChoice {
  key: string;
  name: string;
  paper: string;
  ink: string;
  accent: string;
}

interface ReportBlock {
  kind: string;
  section?: number;
  variant?: string;
  chart_type?: string;
  hidden?: boolean;
}

interface Harness {
  report: Report;
  // Recorded in the order the route handlers observe them, so the test can
  // assert the store-then-rebuild ordering against the real router rather than
  // against a mock's call log.
  calls: string[];
  downloadAuthed: boolean | null;
  /** Whether the canvas document was fetched with the session's bearer token —
   *  the document is a customer's data, unlike the font faces it references. */
  previewAuthed: boolean | null;
  // The block list as the server would hold it, so a test can assert what the
  // editor actually saved rather than that it saved something.
  blocks: ReportBlock[] | null;
  savedBlocks: ReportBlock[][];
  /** Every style group the panel saved, newest last (5f). */
  savedStyle: Record<string, unknown>[];
}

function report(overrides: Partial<Report> & { id: string }): Report {
  return {
    repository_id: "repo",
    prompt: "Quarterly revenue",
    status: "done",
    error: null,
    created_at: new Date("2026-08-04T10:00:00Z").toISOString(),
    has_pdf: true,
    has_pptx: false,
    schedule_id: null,
    plan: null,
    plan_rounds: 1,
    can_rerender: true,
    template: "kvarta",
    ...overrides,
  };
}

// Mirrors TEMPLATES in src/contextvault/services/report_style.py — default
// (Кварта) first, exactly as the endpoint promises.
const TEMPLATES: TemplateChoice[] = [
  { key: "kvarta", name: "Кварта", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
  { key: "prezydia", name: "Президія", paper: "#14161d", ink: "#f2f5fa", accent: "#4f97db" },
  { key: "mahistral", name: "Магістраль", paper: "#ffffff", ink: "#14181d", accent: "#9e3d16" },
  { key: "chasopys", name: "Часопис", paper: "#f7f7f5", ink: "#16181a", accent: "#0a5f56" },
  { key: "minimum", name: "Мінімум", paper: "#ffffff", ink: "#0b0b0c", accent: "#0d4677" },
];

// What an unedited one-section report derives, mirroring `default_blocks`.
// Mirrors `TEXT_KINDS` in services/report_blocks.py, which is where the rule is
// enforced. The stub needs it so the sheet it serves is editable in the same
// places the real document is.
const EDITABLE_KINDS = new Set(["cover", "heading", "prose", "sources", "callout"]);

const DEFAULT_BLOCKS: ReportBlock[] = [
  { kind: "cover" },
  { kind: "heading", section: 0 },
  { kind: "viz", section: 0, variant: "chart" },
  { kind: "stats", section: 0 },
  { kind: "viz", section: 0, variant: "table" },
];

async function signIn(page: Page) {
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

async function stubDesign(page: Page, initial: Report): Promise<Harness> {
  const harness: Harness = {
    report: initial,
    calls: [],
    downloadAuthed: null,
    previewAuthed: null,
    blocks: null,
    savedBlocks: [],
    savedStyle: [],
  };

  await page.route(/\/report-templates$/, (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(TEMPLATES),
    }),
  );

  await page.route(/\/repositories\/[^/]+\/reports\/[^/]+$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report),
    });
  });

  await page.route(/\/repositories\/[^/]+\/reports(\?.*)?$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([harness.report]),
    });
  });

  // The canvas's document. Built from `harness.blocks` so the sheet and the tree
  // describe the same report — a stub with fixed anchors would let a test pass
  // while the two disagreed, which is the one thing these specs exist to catch.
  await page.route(
    /\/repositories\/[^/]+\/reports\/[^/]+\/design\/preview$/,
    async (route: Route) => {
      harness.calls.push("preview");
      const auth = await route.request().headerValue("authorization");
      harness.previewAuthed = auth !== null && auth.startsWith("Bearer ");
      const blocks = harness.blocks ?? DEFAULT_BLOCKS;
      const body = blocks
        .map((block, index) => {
          // The real document marks the editable *run* of text and only for the
          // kinds `TEXT_KINDS` permits. A stub that marked everything would make
          // "a computed block offers nothing to type into" unfailable.
          const words = block.text ?? `${block.kind}${block.variant ? ` ${block.variant}` : ""}`;
          const run = EDITABLE_KINDS.has(block.kind)
            ? `<span data-block-text="${index}" contenteditable="true">${words}</span>`
            : words;
          return (
            `<p data-block-index="${index}" style="padding:14px;margin:0 0 10px">` + `${run}</p>`
          );
        })
        .join("");
      await route.fulfill({
        status: 200,
        contentType: "text/html; charset=utf-8",
        body:
          // `zoom: var(--cv-zoom, 1)` mirrors `_preview_css` exactly (#168). The
          // canvas only sets the property; if the stub's sheet did not read it,
          // every scale assertion below would pass against an unscaled document.
          "<style>.cv-sheet{width:210mm;box-sizing:border-box;margin:0 auto;" +
          "padding:18mm;background:#fff;color:#111;zoom:var(--cv-zoom,1)}" +
          "[data-block-index].cv-selected{outline:2px solid #1a4a75}</style>" +
          `<div class="cv-sheet"><header class="cv-head">Annual report</header>${body}` +
          '<footer class="cv-foot">Page —</footer></div>',
      });
    },
  );

  // Kept off the network so a missing face cannot colour a timing assertion, and
  // so the console stays clean enough to read.
  await page.route(/\/api\/report-fonts\//, (route: Route) =>
    route.fulfill({ status: 200, contentType: "font/ttf", body: Buffer.from("") }),
  );

  await page.route(/\/repositories\/[^/]+\/reports\/[^/]+\/design$/, async (route: Route) => {
    // One path, two verbs: GET is the editor's read model, PUT the partial save.
    if (route.request().method() === "GET") {
      harness.calls.push("design:get");
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          template: harness.report.template,
          blocks_version: 1,
          blocks: harness.blocks ?? DEFAULT_BLOCKS,
          stored: harness.blocks !== null,
          // Mirrors what the endpoint really sends. The panel tolerates their
          // absence, but a stub that omits them stops resembling the backend.
          typography: {},
          page: {},
          brand: {},
          sections: [
            {
              index: 0,
              title: "Revenue by year",
              kind: "data",
              // No pie: this section is a time series, and the gate is the
              // backend's to apply.
              allowed_chart_types: ["bar", "line"],
              draws_chart: true,
              prose: null,
              sources: null,
            },
          ],
          title: "Annual report",
        }),
      });
      return;
    }
    const body = route.request().postDataJSON() as {
      template?: string;
      blocks?: ReportBlock[] | null;
      typography?: Record<string, unknown> | null;
      page?: Record<string, unknown> | null;
      brand?: Record<string, unknown> | null;
    };
    harness.calls.push("design");
    if ("template" in body && body.template !== undefined) {
      harness.report = { ...harness.report, template: body.template };
    }
    if ("blocks" in body) {
      harness.blocks = body.blocks ?? null;
      if (body.blocks != null) harness.savedBlocks.push(body.blocks);
    }
    for (const group of ["typography", "page", "brand"] as const) {
      if (group in body) harness.savedStyle.push({ [group]: body[group] });
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report),
    });
  });

  await page.route(/\/repositories\/[^/]+\/reports\/[^/]+\/rerender$/, async (route: Route) => {
    harness.calls.push("rerender");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report),
    });
  });

  await page.route(/\/reports\/[^/]+\/download\?format=[a-z]+$/, async (route: Route) => {
    // The token proves the preview is a fetched blob rather than an <iframe
    // src> pointed straight at the download URL, which would arrive
    // unauthenticated.
    const auth = await route.request().headerValue("authorization");
    harness.downloadAuthed = auth !== null && auth.startsWith("Bearer ");
    await route.fulfill({
      status: 200,
      contentType: "application/pdf",
      body: Buffer.from("%PDF-1.7 stub"),
    });
  });

  return harness;
}

async function openReports(page: Page): Promise<void> {
  const repoName = `Report design E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  await expect(page.locator("li.repo-item", { hasText: repoName })).toBeVisible();
  await page.getByRole("link", { name: "Reports" }).click();
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
}

/** Navigate from the reports list into the design workbench.
 *
 *  Step 6a moved the editor out of the row and onto `/reports/:id/design`, so a
 *  spec that wants the tree navigates rather than expanding a panel. */
async function openWorkbench(page: Page): Promise<void> {
  await page.locator("li.report-item").getByRole("link", { name: "Edit design" }).click();
  await expect(page).toHaveURL(/\/reports\/[^/]+\/design$/);
  await expect(page.getByRole("list", { name: "Report blocks" })).toBeVisible();
}

test("choosing a template stores it before rebuilding, and shows the rebuilt preview", async ({
  page,
}) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffff6666-6666-4666-8666-666666666666" }));
  await openReports(page);

  const row = page.locator("li.report-item");
  await expect(row.locator("span.badge")).toHaveText("Done");

  await row.getByRole("button", { name: "Design template" }).click();
  const gallery = page.getByRole("dialog", { name: "Report templates" });
  await expect(gallery).toBeVisible();

  await gallery.getByRole("button", { name: /Часопис/ }).click();

  // Store, then rebuild — reversed, a failed store could leave the artifact
  // wearing a template the row never recorded.
  await expect(() => expect(harness.calls).toContain("rerender")).toPass();
  expect(harness.calls.indexOf("design")).toBeGreaterThanOrEqual(0);
  expect(harness.calls.indexOf("design")).toBeLessThan(harness.calls.indexOf("rerender"));

  await expect(gallery).toBeHidden();
  const preview = row.getByTitle("Report preview");
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute("src", /^blob:/);
  expect(harness.downloadAuthed).toBe(true);
});

test("the preview gets the whole row, not the half the delete button leaves it", async ({
  page,
}) => {
  // `.report-item` is a wrapping flex row. Every other child either declares its
  // own basis or is small enough not to matter; the design panel declared no
  // layout rule at all, so it landed on a line beside `.report-delete` and got
  // roughly half the width — the report rendered into a 501px column inside a
  // 1014px row, with the delete button stranded vertically centred against it.
  //
  // Only a real browser can see this. jsdom performs no layout, so every
  // bounding box it reports is zero and the unit suite is blind to it by
  // construction — which is why this shipped.
  await signIn(page);
  await stubDesign(page, report({ id: "ffff7777-7777-4777-8777-777777777777" }));
  await openReports(page);

  const row = page.locator("li.report-item");
  await row.getByRole("button", { name: "Design template" }).click();
  await page
    .getByRole("dialog", { name: "Report templates" })
    .getByRole("button", {
      name: /Часопис/,
    })
    .click();

  const preview = row.getByTitle("Report preview");
  await expect(preview).toBeVisible();

  const rowBox = (await row.boundingBox())!;
  const previewBox = (await preview.boundingBox())!;
  expect(previewBox.width).toBeGreaterThan(rowBox.width * 0.9);
});

test("reordering a block saves the moved list and rebuilds from it", async ({ page }) => {
  // The acceptance criterion jsdom cannot reach. HTML5 drag-and-drop needs a
  // layout engine and a real pointer: jsdom implements neither, so the unit
  // suite dispatches synthetic `dragstart`/`drop` events and can only prove the
  // handlers are wired — never that a reader dragging a row moves it.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffff8888-8888-4888-8888-888888888888" }));
  await openReports(page);

  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await expect(tree).toBeVisible();
  const rows = tree.getByRole("listitem");
  await expect(rows).toHaveCount(5);

  // The stats line, dragged above the chart.
  await rows.nth(3).dragTo(rows.nth(2));

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved.map((b) => `${b.kind}:${b.variant ?? ""}`)).toEqual([
    "cover:",
    "heading:",
    "stats:",
    "viz:chart",
    "viz:table",
  ]);

  // Saved, then drawn, then shown — the same ordering the template path keeps,
  // and the reason `PUT …/design` deliberately does not rebuild on its own. As
  // of 6b the drawing is the canvas's document, not a print: editing on the
  // sheet costs no PDF at all.
  // The editor draws a sheet on open, so the *first* preview precedes any edit.
  // The claim is about this edit: its save lands before the redraw it causes.
  await expect(() => expect(harness.calls.lastIndexOf("preview")).toBeGreaterThan(0)).toPass();
  expect(harness.calls.indexOf("design")).toBeLessThan(harness.calls.lastIndexOf("preview"));
  expect(harness.calls).not.toContain("rerender");

  await expect(page.getByTestId("canvas-host")).toBeVisible();
});

test("the editor offers no pie for a time series", async ({ page }) => {
  // Parent design §3, seen end to end: the chips come from the backend's
  // `allowed_chart_types`, so what the reader can click is what the renderer can
  // actually draw.
  await signIn(page);
  await stubDesign(page, report({ id: "ffff9999-9999-4999-8999-999999999999" }));
  await openReports(page);

  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(2).getByRole("button").first().click();

  const chips = page.getByRole("group", { name: "Chart type" });
  await expect(chips.getByRole("button", { name: "Bar" })).toBeVisible();
  await expect(chips.getByRole("button", { name: "Line" })).toBeVisible();
  await expect(chips.getByRole("button", { name: "Pie" })).toHaveCount(0);
});

test("editing a heading saves it as an override and rebuilds from it", async ({ page }) => {
  // Step 5e's acceptance criterion, end to end. The unit suite can prove the
  // field publishes a block list; only a browser proves that typing into it
  // travels through the debounce, reaches `PUT …/design` as an override on that
  // block, and produces a rebuilt artifact.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffffaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }));
  await openReports(page);

  await openWorkbench(page);

  // 6c moved the typing onto the sheet. `data-block-text` is inside the shadow
  // root, and Playwright's selector engine pierces open shadow roots — which is
  // convenient here and was a trap in 6b, where it made a "not in the light DOM"
  // assertion unfailable.
  const run = page.locator('[data-block-text="1"]');
  await expect(run).toHaveText("heading");
  await run.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("Revenue, restated");

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved[1]).toEqual({ kind: "heading", section: 0, text: "Revenue, restated" });
  // The override rides on one block; the rest of the list is untouched.
  expect(saved[0]).toEqual({ kind: "cover" });

  await expect(() => expect(harness.calls.lastIndexOf("preview")).toBeGreaterThan(0)).toPass();
  expect(harness.calls.indexOf("design")).toBeLessThan(harness.calls.lastIndexOf("preview"));
  await expect(page.getByTestId("canvas-host")).toBeVisible();
});

test("a block that prints computed values offers no field to edit", async ({ page }) => {
  // The parent design's non-goal, where the reader can see it. The backend
  // refuses `text` on these kinds with a 422; this is the same rule stated in
  // the UI, so the refusal is never the reader's first sight of it.
  await signIn(page);
  await stubDesign(page, report({ id: "ffffbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }));
  await openReports(page);

  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(3).getByRole("button").first().click(); // stats

  await expect(page.getByText(/not editable/i)).toBeVisible();
  // And on the sheet, which is where a reader would try. The block is there and
  // clickable; what it does not have is a run to put a caret in.
  await expect(page.locator('[data-block-index="3"]')).toBeVisible();
  await expect(page.locator('[data-block-index="3"] [contenteditable]')).toHaveCount(0);
});

// --- 5f: brand, typography and images ---------------------------------------

test("the style inspector is reachable and saves a whole group", async ({ page }) => {
  // jsdom has no layout and the unit tests mock the API, so this is the only
  // place that proves a reader can actually reach these controls in a browser
  // and that a click leaves the SPA as a request.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffff9999-9999-4999-8999-999999999999" }));
  await openReports(page);

  await openWorkbench(page);

  await expect(page.getByTestId("pairing-chip")).toHaveCount(4);
  await expect(page.getByTestId("accent-swatch")).toHaveCount(6);

  await page.getByTestId("density-chip").nth(2).click();

  // The hook debounces, so the save is not immediate — and when it lands it
  // carries the whole typography group, which is what the backend stores.
  await expect(() => expect(harness.savedStyle.length).toBeGreaterThan(0)).toPass();
  expect(harness.savedStyle[harness.savedStyle.length - 1]).toEqual({
    typography: { density: "airy" },
  });
});

test("choosing the template default clears a token rather than sending a value", async ({
  page,
}) => {
  // "As template" must mean *absent*. The backend applies nothing when nothing
  // is set, and that is what keeps every previously stored report rendering
  // byte-identically — so a chip that sent a concrete value would silently
  // restyle documents the reader never touched.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffffaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" }));
  await openReports(page);

  await openWorkbench(page);

  await page.getByTestId("pairing-chip").nth(1).click(); // grotesque
  await expect(() => expect(harness.savedStyle.length).toBeGreaterThan(0)).toPass();

  await page.getByTestId("pairing-chip").nth(0).click(); // as template
  await expect(() =>
    expect(harness.savedStyle[harness.savedStyle.length - 1]).toEqual({ typography: {} }),
  ).toPass();
});

test("the cover layout and signature toggle save as one brand group", async ({ page }) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffffbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" }));
  await openReports(page);

  await openWorkbench(page);

  // A closed <details> genuinely hides its body in a browser — jsdom does not —
  // so the group has to be opened before its controls can be reached.
  await page.getByTestId("group-brand").getByText("Brand kit").click();
  await page.getByTestId("cover-chip").nth(1).click(); // band
  await expect(() => expect(harness.savedStyle.length).toBeGreaterThan(0)).toPass();

  await page.getByTestId("approval-toggle").check();

  // Both live in `brand`, so the second save carries the first's choice too —
  // a group travels whole, and losing the cover here would be the bug.
  await expect(() =>
    expect(harness.savedStyle[harness.savedStyle.length - 1]).toEqual({
      brand: { cover: "band", approval: true },
    }),
  ).toPass();
});

test("the logo picker offers only the raster formats the deck can place", async ({ page }) => {
  // Not cosmetic: python-pptx supports no SVG, so a vector logo would render in
  // the PDF and leave a silent gap in the deck. The upload is refused
  // server-side too; this is the half a reader meets first.
  await signIn(page);
  await stubDesign(page, report({ id: "ffffcccc-cccc-4ccc-8ccc-cccccccccccc" }));
  await openReports(page);

  await openWorkbench(page);

  await expect(page.getByTestId("logo-file")).toHaveAttribute(
    "accept",
    "image/png,image/jpeg,image/webp",
  );
  await expect(page.getByTestId("logo-clear")).toHaveCount(0);
});

test("a toc and an image can be added to the block list", async ({ page }) => {
  // The two block kinds 5f adds. Proves the palette offers them and that the
  // saved list carries them — the renderers are covered by unit tests, but
  // nothing else shows a reader can put one on the sheet.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffffdddd-dddd-4ddd-8ddd-ddddddddddddd" }));
  await openReports(page);

  await openWorkbench(page);
  await expect(page.getByRole("list", { name: "Report blocks" })).toBeVisible();

  // The palette opens on demand as of #167 — nine buttons no longer stand
  // permanently above an empty panel for something done once per report.
  await page.getByTestId("block-add-toggle").click();
  const palette = page.getByRole("group", { name: /add/i });
  await palette.getByRole("button", { name: "+ Contents", exact: true }).click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved.some((block) => block.kind === "toc")).toBe(true);
});

// --- step 6a: the workbench ---------------------------------------------------

test("the report row navigates to the workbench", async ({ page }) => {
  await signIn(page);
  await stubDesign(page, report({ id: "aaaa1111-1111-4111-8111-111111111111" }));
  await openReports(page);

  await page.locator("li.report-item").getByRole("link", { name: "Edit design" }).click();

  await expect(page).toHaveURL(/\/reports\/[^/]+\/design$/);
  await expect(page.getByRole("list", { name: "Report blocks" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Rebuild" })).toBeVisible();
});

test("selecting in the tree fills the Блок group in the other column", async ({ page }) => {
  // The whole reason the block list was lifted into a hook: two columns, one
  // selection. jsdom can prove the components agree; only a browser can prove
  // they are on screen at the same time.
  await signIn(page);
  await stubDesign(page, report({ id: "aaaa2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);

  await page.getByTestId("block-node-1").click();

  await expect(page.getByTestId("block-node-1")).toHaveAttribute("aria-current", "true");
  // The witness was the inspector's textbox until 6c retired it. Its replacement
  // is stronger rather than weaker: "Move up" is enabled only away from the top
  // of the list, so it says *which* block the inspector is showing, and the ring
  // on the sheet makes it all three columns agreeing rather than two.
  await expect(page.getByTestId("block-up")).toBeVisible();
  await expect(page.getByTestId("block-up")).toBeEnabled();
  await expect(page.locator('[data-block-index="1"].cv-selected')).toBeVisible();
});

test("an inspector group stays closed across a reload", async ({ page }) => {
  await signIn(page);
  await stubDesign(page, report({ id: "aaaa3333-3333-4333-8333-333333333333" }));
  await openReports(page);
  await openWorkbench(page);

  const group = page.getByTestId("group-typo");
  await expect(group).toHaveAttribute("open", "");
  await group.getByText("Typography").click();
  await expect(group).not.toHaveAttribute("open", "");

  // `toggle` is dispatched asynchronously, so the DOM closes a task before the
  // state is written. Reloading on the attribute alone races that write — and
  // does so only under load, which is why it passed run after run in isolation.
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("contextvault.inspector.typo")))
    .toBe("0");

  await page.reload();
  await expect(page.getByTestId("group-typo")).not.toHaveAttribute("open", "");
});

test("the three columns sit side by side and nothing scrolls sideways", async ({ page }) => {
  // jsdom performs no layout, so this is the first place these are real boxes.
  await signIn(page);
  await stubDesign(page, report({ id: "aaaa4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);

  const tree = (await page.getByTestId("block-node-0").boundingBox())!;
  const inspector = (await page.getByTestId("group-typo").boundingBox())!;
  expect(tree.x).toBeLessThan(inspector.x);

  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflows).toBe(false);
});

test("a design response with no style groups leaves the block tree standing", async ({ page }) => {
  // The exact 5f defect: a cached SPA meeting an older response threw inside the
  // inspector and React unmounted the entire editor, block tree included. Four
  // specs failed then with the list simply absent, and 1332 unit tests did not
  // see it because their fixtures were complete.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "aaaa5555-5555-4555-8555-555555555555" }));
  await page.route(/\/repositories\/[^/]+\/reports\/[^/]+\/design$/, async (route: Route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        template: harness.report.template,
        blocks_version: 1,
        blocks: DEFAULT_BLOCKS,
        stored: false,
        sections: [],
        title: "Revenue",
      }),
    });
  });
  await openReports(page);
  await openWorkbench(page);

  await expect(page.getByRole("list", { name: "Report blocks" })).toBeVisible();
});

test("a report id from another repository is a stated dead end", async ({ page }) => {
  await signIn(page);
  await page.route(/\/repositories\/[^/]+\/reports\/[^/]+$/, (route: Route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: '{"detail":"not found"}' }),
  );

  await page.goto("/reports/00000000-0000-4000-8000-000000000000/design");

  await expect(page.getByText(/not in the selected repository/i)).toBeVisible();
  await expect(page.getByRole("list", { name: "Report blocks" })).toHaveCount(0);
});

// --- the canvas (6b) ---------------------------------------------------------

test("the sheet arrives with the session's token and lands inside a shadow root", async ({
  page,
}) => {
  // Two things only a browser shows. The document is a customer's data, so it
  // must be fetched rather than pointed at; and it must land behind a style
  // boundary, because `stylesheet()` styles bare `p`, `table` and `body` and the
  // application's own pages would otherwise be restyled by opening the editor.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "bbbb1111-1111-4111-8111-111111111111" }));
  await openReports(page);
  await openWorkbench(page);

  await expect(() => expect(harness.calls).toContain("preview")).toPass();
  expect(harness.previewAuthed).toBe(true);

  const inShadow = await page.evaluate(
    () =>
      document
        .querySelector("[data-testid='canvas-host']")
        ?.shadowRoot?.querySelectorAll("[data-block-index]").length ?? 0,
  );
  expect(inShadow).toBe(5);
  // Nothing of the document is in the light DOM, where it could style the app.
  // `document.querySelectorAll`, not a Playwright locator: Playwright's selector
  // engine deliberately pierces open shadow roots, so a locator would find all
  // five and this assertion would be unfailable.
  const inLightDom = await page.evaluate(
    () => document.querySelectorAll("[data-block-index]").length,
  );
  expect(inLightDom).toBe(0);
});

test("hovering a block on the sheet names it", async ({ page }) => {
  await signIn(page);
  await stubDesign(page, report({ id: "bbbb2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);

  await expect(page.getByTestId("canvas-host")).toBeVisible();
  const block = page.locator("[data-testid='canvas-host']").locator("[data-block-index='1']");
  // Park the cursor off the sheet first. `hover()` moves the mouse, and a mouse
  // that is *already* on the target moves nowhere — so no `mouseover` fires and
  // the assertion reads whatever block happened to be under the resting cursor
  // when the canvas first drew. That is a browser fact, not a defect: no event
  // is dispatched when layout shifts under a stationary pointer.
  await page.mouse.move(4, 400);
  await block.hover();

  await expect(page.getByTestId("canvas-tag")).toContainText("Heading");
});

test("the tree and the sheet never disagree about what is selected", async ({ page }) => {
  // The reason block state was lifted into `useBlockList` in 6a, now observable:
  // two columns, one selection, moved from either end.
  await signIn(page);
  await stubDesign(page, report({ id: "bbbb3333-3333-4333-8333-333333333333" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const sheet = page.locator("[data-testid='canvas-host']");
  const tree = page.getByRole("list", { name: "Report blocks" });

  // Click on the sheet — the tree follows.
  await sheet.locator("[data-block-index='2']").click();
  await expect(tree.getByRole("listitem").nth(2).locator("button.wb-node")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(sheet.locator("[data-block-index='2']")).toHaveClass(/cv-selected/);

  // Click in the tree — the sheet follows.
  await tree.getByRole("listitem").nth(4).getByRole("button").first().click();
  await expect(sheet.locator("[data-block-index='4']")).toHaveClass(/cv-selected/);
  await expect(sheet.locator(".cv-selected")).toHaveCount(1);
});

test("selecting a block brings it to the middle of the canvas", async ({ page }) => {
  // `scrollIntoView({ block: "nearest" })` moved nothing when the block was
  // already partly on screen and clipped it to the edge otherwise, so choosing a
  // block often looked like it had done nothing (#194). Only a real browser can
  // see this: jsdom performs no layout and every rect it reports is zero.
  //
  // **The document has to be long enough to scroll**, and the default fixture is
  // not: five short paragraphs fit the column whole, so nothing scrolls, every
  // block is near the centre already and the assertion passes against `nearest`
  // too. Written that way first, it did — which is the only reason this comment
  // exists.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "cccc1111-1111-4111-8111-111111111111" }));
  harness.blocks = Array.from({ length: 40 }, () => ({ kind: "heading", section: 0 }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const tree = page.getByRole("list", { name: "Report blocks" });

  /** How far the block's centre sits from the centre of what the reader sees.
   *
   *  The **window** is the scroller, and finding that out was most of this test.
   *  The canvas host is not height-constrained — measured `scrollHeight ===
   *  clientHeight === 2014` — so it grows to fit the sheet and never scrolls
   *  inside; `.wb-canvas` declares no `overflow` at all. Measuring against
   *  either one compares a box with itself and passes however the sheet is
   *  positioned. */
  const offsetFromCentre = async (index: number): Promise<number> =>
    page.evaluate((i) => {
      const host = document.querySelector("[data-testid='canvas-host']");
      const block = host?.shadowRoot?.querySelector(`[data-block-index="${i}"]`);
      if (!block) return Number.NaN;
      const b = block.getBoundingClientRect();
      return Math.abs(b.top + b.height / 2 - window.innerHeight / 2);
    }, index);

  // There must be real room to scroll, or this test proves nothing at all.
  const room = await page.evaluate(
    () => document.documentElement.scrollHeight - document.documentElement.clientHeight,
  );
  const viewport = page.viewportSize()!.height;
  expect(room, "the fixture must overflow the window or nothing can move").toBeGreaterThan(
    viewport / 2,
  );

  for (const index of [35, 2]) {
    await tree.getByRole("listitem").nth(index).getByRole("button").first().click();
    const away = await offsetFromCentre(index);
    expect(
      away,
      `expected block ${index} near the middle of the window, it sat ${away}px away in a ${viewport}px viewport`,
    ).toBeLessThan(viewport / 4);
  }
});

test("choosing a block opens the group the reader collapsed, without adopting it", async ({
  page,
}) => {
  // The inspector half of #194, and the half that needs a real browser twice
  // over. A closed `<details>` genuinely hides its body here and not in jsdom;
  // and `.open = true` fires `toggle` here and, as far as this suite could
  // detect, not in jsdom at all — so the unit suite cannot see the persistence
  // question this test exists for.
  await signIn(page);
  await stubDesign(page, report({ id: "cccc2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const group = page.getByTestId("group-block");
  await expect(group).toHaveAttribute("open", "");
  // By element, not by title: this suite runs in English, so the Ukrainian
  // label never matches.
  await group.locator("summary").click();
  await expect(group).not.toHaveAttribute("open", "");
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("contextvault.inspector.block")))
    .toBe("0");

  await page
    .getByRole("list", { name: "Report blocks" })
    .getByRole("listitem")
    .nth(2)
    .getByRole("button")
    .first()
    .click();
  await expect(group).toHaveAttribute("open", "");

  // **The reload is what makes this able to fail.** Reading storage straight
  // after the reveal races the write: `toggle` is queued, so the assertion runs
  // while the old value is still there and passes however the handler behaves —
  // written that way first, it passed with the suppression deleted. A reload
  // waits for the write and asks the question the reader would: is the group I
  // closed still closed tomorrow? A reveal is not a preference.
  await page.reload();
  await expect(page.getByTestId("group-block")).not.toHaveAttribute("open", "");
});

test("the floating toolbar deletes the selected block", async ({ page }) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "bbbb4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  await page.locator("[data-testid='canvas-host']").locator("[data-block-index='3']").click();
  await page.getByTestId("canvas-toolbar").getByTitle("Delete this block").click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved.map((b) => b.kind)).toEqual(["cover", "heading", "viz", "viz"]);
});

test("the switch swaps the sheet for the real PDF and back", async ({ page }) => {
  // 5c's objection answered in the interface: the canvas is where you select and
  // edit, the PDF stays the artifact of record and is one click away.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "bbbb5555-5555-4555-8555-555555555555" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();
  expect(harness.calls).not.toContain("rerender");

  await page.getByRole("button", { name: "PDF", exact: true }).click();

  const pdf = page.getByTitle("Report preview");
  await expect(pdf).toBeVisible();
  await expect(pdf).toHaveAttribute("src", /^blob:/);
  // Only now is anything printed — a reader who stays on the sheet never pays.
  expect(harness.calls).toContain("rerender");
  expect(harness.downloadAuthed).toBe(true);

  await page.getByRole("button", { name: "Sheet", exact: true }).click();
  await expect(page.getByTestId("canvas-host")).toBeVisible();
  await expect(pdf).toBeHidden();
});

test("the workbench PDF preview keeps the canvas column's own height", async ({ page }) => {
  // `.wb-canvas .report-preview` sets `height: calc(100vh - 130px)` for this
  // exact iframe, but `.wb-canvas` is a flex *column* — its main axis is
  // height — so an unscoped `.report-preview { flex: 1 1 100% }` (added for the
  // unrelated list-row panel in reports.spec.ts) would override that height via
  // flex-basis and shrink the preview. Pinning the viewport makes the expected
  // height a fixed, checkable number instead of "smaller than it should be".
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "bbbb5556-5556-4556-8556-556655665566" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  await page.getByRole("button", { name: "PDF", exact: true }).click();
  const pdf = page.getByTitle("Report preview");
  await expect(pdf).toBeVisible();
  expect(harness.calls).toContain("rerender");

  const height = await pdf.evaluate((el) => el.getBoundingClientRect().height);
  // calc(100vh - 130px) at a 900px viewport is exactly 770px.
  expect(height, `expected calc(100vh - 130px) = 770px at a 900px viewport, got ${height}px`).toBe(
    770,
  );
});

test("the three columns still sit side by side with a sheet in the middle", async ({ page }) => {
  await signIn(page);
  await stubDesign(page, report({ id: "bbbb6666-6666-4666-8666-666666666666" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const tree = await page.locator("aside.wb-panel").first().boundingBox();
  const canvas = await page.locator("section.wb-canvas").boundingBox();
  const inspector = await page.locator("aside.wb-panel-right").boundingBox();
  expect(tree!.x + tree!.width).toBeLessThanOrEqual(canvas!.x + 1);
  expect(canvas!.x + canvas!.width).toBeLessThanOrEqual(inspector!.x + 1);

  // The sheet is A4-wide and may scroll inside its own column; the page itself
  // must never scroll sideways.
  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflows).toBe(false);
});

// --- editing on the sheet (6c) ----------------------------------------------

test("words typed on the sheet come back in the rebuilt document", async ({ page }) => {
  // The round trip, which only a browser can show: a caret in a shadow root, a
  // debounced PUT, and the *server's* answer replacing the document the reader
  // typed into. The unit suite proves each half and can prove neither joint.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "cccc1111-1111-4111-8111-111111111111" }));
  await openReports(page);
  await openWorkbench(page);

  const run = page.locator('[data-block-text="1"]');
  await run.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("Переписаний заголовок");

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  // Blurring is what releases the held redraw — the reader has to look away
  // before the server's document replaces the one under their caret.
  await page.locator(".cv-sheet").click({ position: { x: 2, y: 2 } });

  await expect(page.locator('[data-block-text="1"]')).toHaveText("Переписаний заголовок");
});

test("a burst of typing costs one rebuild, not one per character", async ({ page }) => {
  // The acceptance criterion 5c's debounce and coalescing already satisfy, kept
  // honest across the move: typing on the sheet must go through the same hook.
  // A per-keystroke rebuild is 300 ms of chart redraw each, so the failure mode
  // is an editor that falls further behind the longer someone works.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "cccc2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(() => expect(harness.calls).toContain("preview")).toPass();
  const before = harness.calls.filter((call) => call === "preview").length;

  const run = page.locator('[data-block-text="1"]');
  await run.click();
  await page.keyboard.type("двадцять три символи", { delay: 20 });

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const rebuilds = harness.calls.filter((call) => call === "preview").length - before;
  // At most two: one that may have started mid-burst and the coalesced one that
  // follows it. Twenty would be one per character.
  expect(rebuilds).toBeLessThanOrEqual(2);
});

test("reset gives back the generated words and keeps no copy of them", async ({ page }) => {
  // 5e made a reset a *deletion* of the override rather than a copy of today's
  // words, and 6c had to carry that across the move — a reset that wrote the
  // current text back would freeze it against the next re-render.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "cccc3333-3333-4333-8333-333333333333" }));
  await openReports(page);
  await openWorkbench(page);

  const run = page.locator('[data-block-text="1"]');
  await run.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type("Мій заголовок");
  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();

  await page.getByRole("button", { name: /Restore the generated/i }).click();

  await expect(() => {
    const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
    // The key is *gone*, not emptied: an empty string is a decision to print
    // nothing, which is a different thing from having no opinion.
    expect(saved[1]).toEqual({ kind: "heading", section: 0 });
  }).toPass();
});

// --- marks and actions (6d) --------------------------------------------------

test("a mark chosen in the picker lands on that block and no other", async ({ page }) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "dddd1111-1111-4111-8111-111111111111" }));
  await openReports(page);
  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(1).getByRole("button").first().click(); // heading
  await page.getByRole("group", { name: "Mark" }).getByRole("button", { name: "warn" }).click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved[1]).toEqual({ kind: "heading", section: 0, icon: "warn" });
  // The mark rides on one block; nothing else in the list acquires one.
  expect(saved.filter((block) => block.icon !== undefined)).toHaveLength(1);
});

test("the picker offers no mark for a block whose kind cannot carry one", async ({ page }) => {
  // The write gate refuses an icon on a computed kind with a 422. This is the
  // same rule stated where the reader is, so the refusal is never their first
  // sight of it.
  await signIn(page);
  await stubDesign(page, report({ id: "dddd2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(3).getByRole("button").first().click(); // stats

  await expect(page.getByRole("group", { name: "Mark" })).toHaveCount(0);
});

test("duplicating a block puts an identical copy directly after it", async ({ page }) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "dddd3333-3333-4333-8333-333333333333" }));
  await openReports(page);
  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(1).getByRole("button").first().click(); // heading
  await page.getByTestId("block-duplicate").click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  const saved = harness.savedBlocks[harness.savedBlocks.length - 1];
  expect(saved.map((block) => block.kind)).toEqual([
    "cover",
    "heading",
    "heading",
    "viz",
    "stats",
    "viz",
  ]);
  expect(saved[2]).toEqual(saved[1]);
  await expect(tree.getByRole("listitem")).toHaveCount(6);
});

test("the actions row reorders a block without a drag", async ({ page }) => {
  // 6a built these buttons and the unit suite covers them; nothing had ever
  // driven them in a browser. They are also the only reordering path a reader
  // without a pointer has, which is precisely the case a synthetic drag event
  // cannot speak for.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "dddd4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);

  const tree = page.getByRole("list", { name: "Report blocks" });
  await tree.getByRole("listitem").nth(3).getByRole("button").first().click(); // stats
  await page.getByTestId("block-up").click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  expect(harness.savedBlocks[harness.savedBlocks.length - 1].map((b) => b.kind)).toEqual([
    "cover",
    "heading",
    "stats",
    "viz",
    "viz",
  ]);

  // At the top of the list the button is a no-op, and says so rather than
  // silently doing nothing.
  await tree.getByRole("listitem").nth(0).getByRole("button").first().click();
  await expect(page.getByTestId("block-up")).toBeDisabled();
});

// --- focus mode (#167) -------------------------------------------------------

test("the workbench collapses the app sidebar and gives the room to the sheet", async ({
  page,
}) => {
  // The measurement the card is built on: four columns at 1440px leave 564px
  // for a 794px sheet. Only a browser can say whether the layout actually
  // produced the width — jsdom performs no layout at all.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "eeee1111-1111-4111-8111-111111111111" }));
  await openReports(page);

  const sidebar = page.locator(".app-shell > .sidebar");
  expect(await sidebar.evaluate((el) => el.getBoundingClientRect().width)).toBeGreaterThan(200);

  await openWorkbench(page);

  expect(await sidebar.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThan(70);
  // The navigation is narrowed, not removed — an editor with one way out is a
  // trap — and every icon keeps a name a screen reader can read.
  await expect(page.locator(".sidebar").getByRole("link", { name: "Reports" })).toBeVisible();

  // 836px measured, against the 794px an A4 page needs at 96dpi. The card's
  // whole claim, asserted where it can actually be checked.
  const canvas = page.locator(".wb-canvas");
  expect(await canvas.evaluate((el) => el.clientWidth)).toBeGreaterThanOrEqual(794);
  // And the page never scrolls sideways to achieve it.
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});

test("leaving the workbench gives the sidebar back", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "eeee2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);

  await page.getByRole("link", { name: "Back to reports" }).click();

  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
  const width = await page
    .locator(".app-shell > .sidebar")
    .evaluate((el) => el.getBoundingClientRect().width);
  expect(width).toBeGreaterThan(200);
});

test("adding a block takes one click and the palette does not stand there", async ({ page }) => {
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "eeee4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);

  await expect(page.getByRole("group", { name: "Add a block" })).toHaveCount(0);

  await page.getByTestId("block-add-toggle").click();
  await page.getByRole("button", { name: "+ Callout" }).click();

  await expect(() => expect(harness.savedBlocks.length).toBeGreaterThan(0)).toPass();
  expect(harness.savedBlocks[harness.savedBlocks.length - 1].map((b) => b.kind)).toContain(
    "callout",
  );
  // Closed behind itself, or it is the permanent grid again one click later.
  await expect(page.getByRole("group", { name: "Add a block" })).toHaveCount(0);
});

/** Scale the sheet by clicking until the readout says so.
 *
 *  The steps are 50/75/100/125/150, so this walks rather than jumps — which is
 *  also what a reader does, and it exercises the disabled ends on the way. */
async function setScale(page: Page, percent: number): Promise<void> {
  const readout = page.getByTestId("canvas-zoom");
  for (let guard = 0; guard < 8; guard += 1) {
    const shown = Number(((await readout.textContent()) ?? "").replace("%", ""));
    if (shown === percent) return;
    await page.getByRole("button", { name: shown < percent ? "Zoom in" : "Zoom out" }).click();
  }
  throw new Error(`could not reach ${percent}%`);
}

/** Wait until the zoom readout stops changing, rather than for a fixed budget.
 *
 *  A `waitForTimeout` here would be a wall-clock bet: it passes today because a
 *  stray re-fit lands ~250 ms after the switch, and it would stop catching one
 *  that landed later. Consecutive identical samples settle on the *behaviour*
 *  instead — and cost nothing when the value was already stable. */
async function settledZoom(page: Page, samples = 4, gap = 100): Promise<string> {
  const readout = page.getByTestId("canvas-zoom");
  let last = (await readout.textContent()) ?? "";
  let stable = 0;
  for (let guard = 0; guard < 40 && stable < samples; guard += 1) {
    await page.waitForTimeout(gap);
    const now = (await readout.textContent()) ?? "";
    stable = now === last ? stable + 1 : 0;
    last = now;
  }
  return last;
}

test("the sheet is drawn at the scale that was asked for", async ({ page }) => {
  // Only a browser can answer this: jsdom performs no layout, so the unit suite
  // can see the custom property being written and nothing at all about size.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "ffff1111-1111-4111-8111-111111111111" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  // The editor now opens fitted rather than at 100 % (#180), so "life size" has
  // to be asked for explicitly rather than read off the page as it opens.
  await setScale(page, 100);
  const sheet = page.locator("[data-testid='canvas-host']").locator(".cv-sheet");
  const lifeSize = await sheet.evaluate((el) => el.getBoundingClientRect().width);
  expect(lifeSize).toBeGreaterThan(780);

  await setScale(page, 125);
  expect(await sheet.evaluate((el) => el.getBoundingClientRect().width)).toBeCloseTo(
    lifeSize * 1.25,
    0,
  );

  await setScale(page, 50);
  expect(await sheet.evaluate((el) => el.getBoundingClientRect().width)).toBeCloseTo(
    lifeSize * 0.5,
    0,
  );
});

test("hover, selection and the floating toolbar are right at every offered scale", async ({
  page,
}) => {
  // **This is the card.** #167 built a zoom and cut it because pointer events and
  // the toolbar's anchor were wrong at every scale but 100% — and both defects
  // are invisible to a single-scale test, which is why this one loops.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "ffff2222-2222-4222-8222-222222222222" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const sheet = page.locator("[data-testid='canvas-host']");

  for (const percent of [50, 75, 125, 150]) {
    await setScale(page, percent);

    // Hover. Park the cursor first — a mouse already on the target moves
    // nowhere and dispatches nothing.
    await page.mouse.move(4, 400);
    await sheet.locator("[data-block-index='1']").hover();
    await expect(page.getByTestId("canvas-tag"), `hover at ${percent}%`).toContainText("Heading");

    // Click to select — the defect `transform` was cut for.
    await sheet.locator("[data-block-index='2']").click();
    await expect(sheet.locator("[data-block-index='2']"), `selection at ${percent}%`).toHaveClass(
      /cv-selected/,
    );
    await expect(sheet.locator(".cv-selected")).toHaveCount(1);

    // And the toolbar lands on the block it is anchored to — the defect CSS
    // `zoom` was cut for. It is anchored from the block's own rect plus the 8px
    // the stylesheet adds, so the two must agree at every scale.
    const block = await sheet.locator("[data-block-index='2']").evaluate((el) => {
      const b = el.getBoundingClientRect();
      return { top: b.top, right: b.right };
    });
    const bar = await page.getByTestId("canvas-toolbar").evaluate((el) => {
      const b = el.getBoundingClientRect();
      return { top: b.top, left: b.left };
    });
    expect(Math.abs(bar.top - block.top), `toolbar top at ${percent}%`).toBeLessThanOrEqual(2);
    expect(
      Math.abs(bar.left - (block.right + 8)),
      `toolbar left at ${percent}%`,
    ).toBeLessThanOrEqual(2);
  }
});

test("scaling the sheet costs no request", async ({ page }) => {
  // A rebuild is ~300ms of chart redraw and a round trip. Zoom is a custom
  // property on a document that is already mounted, and must stay that cheap.
  await signIn(page);
  const harness = await stubDesign(page, report({ id: "ffff3333-3333-4333-8333-333333333333" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  await expect(() => expect(harness.calls).toContain("preview")).toPass();
  const before = [...harness.calls];

  await setScale(page, 150);
  await setScale(page, 50);
  await page.getByRole("button", { name: "Fit to width" }).click();

  expect(harness.calls).toEqual(before);
});

test("an oversized sheet can be scrolled to both of its edges", async ({ page }) => {
  // The host is a block container precisely so this works: a shadow root's
  // content are children of the host, so under `display: flex` the sheet was a
  // flex item and *shrank* to fit instead of overflowing — 507px where 794px was
  // asked for. Block layout overflows it to the right, which is the direction
  // `overflow-x` can scroll.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "ffff4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  await setScale(page, 150);

  const host = page.getByTestId("canvas-host");
  expect(await host.evaluate((el) => el.scrollWidth - el.clientWidth)).toBeGreaterThan(0);
  // The sheet's left edge is reachable: at scroll 0 it is not off to the left.
  const left = await host.evaluate((el) => {
    el.scrollLeft = 0;
    const sheet = el.shadowRoot?.querySelector(".cv-sheet") as HTMLElement;
    return sheet.getBoundingClientRect().left - el.getBoundingClientRect().left;
  });
  expect(left).toBeGreaterThanOrEqual(-1);
});

test("the sheet's scale survives a trip through the PDF view", async ({ page }) => {
  // #172. The centre column renders the canvas behind a ternary on the view, so
  // switching to `pdf` unmounted it — and the scale, which lived inside it, went
  // with it. Coming back re-initialised at 100 %, silently undoing the reader's
  // choice on a switch they mean as a detour.
  //
  // The unit suite reproduces the readout half of this without a browser. What
  // needs one is everything below it: that the *sheet* is still drawn large, that
  // fit-to-width — which depends on a measured width — survives too, and that the
  // toolbar's anchor still tracks at a scale that outlived a remount.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "ffff4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  // The editor now opens fitted rather than at 100 % (#180), so "life size" has
  // to be asked for explicitly rather than read off the page as it opens.
  await setScale(page, 100);
  const sheet = page.locator("[data-testid='canvas-host']").locator(".cv-sheet");
  const lifeSize = await sheet.evaluate((el) => el.getBoundingClientRect().width);

  await setScale(page, 125);
  const scaled = await sheet.evaluate((el) => el.getBoundingClientRect().width);
  expect(scaled).toBeCloseTo(lifeSize * 1.25, 0);

  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await expect(page.getByTitle("Report preview")).toBeVisible();
  await page.getByRole("button", { name: "Sheet", exact: true }).click();
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  // The readout, and the sheet it claims to describe. Asserting only the readout
  // would pass on a number restored into a document that is drawn life-size.
  await expect(page.getByTestId("canvas-zoom")).toHaveText("125%");
  expect(await sheet.evaluate((el) => el.getBoundingClientRect().width)).toBeCloseTo(scaled, 0);

  // The toolbar is anchored from a rect taken when the selection last changed.
  // A remount is exactly the kind of thing that can leave it stale.
  await sheet.locator("[data-block-index='2']").click();
  const block = await sheet.locator("[data-block-index='2']").evaluate((el) => {
    const b = el.getBoundingClientRect();
    return { top: b.top, right: b.right };
  });
  const bar = await page.getByTestId("canvas-toolbar").evaluate((el) => {
    const b = el.getBoundingClientRect();
    return { top: b.top, left: b.left };
  });
  expect(Math.abs(bar.top - block.top), "toolbar top after the round trip").toBeLessThanOrEqual(2);
  expect(
    Math.abs(bar.left - (block.right + 8)),
    "toolbar left after the round trip",
  ).toBeLessThanOrEqual(2);
});

test("the smallest scale survives a trip through the PDF view", async ({ page }) => {
  // This case used to click *Fit to width* and assert the same number afterwards.
  // Since #180 the editor already opens fitted, so that click became a no-op and
  // the assertion compared the fit against the fit — which a re-fit-on-every-mount
  // regression reproduces exactly, and the test could no longer fail. Asking for
  // 50 % gives it a value the fit would never land on at this viewport (a 804px
  // canvas fits at ~101 %), so a stray re-fit shows up as a changed readout.
  //
  // It complements the case below rather than repeating it: that one leaves the
  // reader one step away from the fit, this one parks them on `MIN`, the end the
  // clamp in `fit()` would snap an unmeasured host to.
  await page.setViewportSize({ width: 1440, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "ffff5555-5555-4555-8555-555555555556" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  await setScale(page, 50);

  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await expect(page.getByTitle("Report preview")).toBeVisible();
  await page.getByRole("button", { name: "Sheet", exact: true }).click();
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  // Settled rather than instantaneous: the remount paints once with the chosen
  // value still in place, and only a beat later would a re-fit overwrite it.
  expect(await settledZoom(page)).toBe("50%");
});

test("the editor opens fitted, and a chosen zoom survives a view switch", async ({ page }) => {
  // Opening at 100 % made every reader's first action the same click. The fit is
  // a real measurement, so this cannot be a unit test — jsdom reports every
  // width as zero.
  //
  // The viewport is pinned because the fit is a function of it: the canvas column
  // is roughly the viewport less 636px of chrome, so the same page opens at 81 %
  // here and 101 % at 1440 — and at ~1430 it rounds to exactly 100 %, which would
  // fail a "not 100 %" assertion for no reason connected to this behaviour.
  await page.setViewportSize({ width: 1280, height: 900 });
  await signIn(page);
  await stubDesign(page, report({ id: "cccc4444-4444-4444-8444-444444444444" }));
  await openReports(page);
  await openWorkbench(page);
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  const readout = page.getByTestId("canvas-zoom");
  // "Not 100 %" is satisfied by 50 %, which is `MIN` — the value `fit()` clamps
  // *up* to when it measures an unmeasured host, and the exact trap this guard
  // exists for. What fitted means to a reader is that the sheet fills the room
  // it was given, so assert that instead: the drawn sheet is the host's own
  // width, give or take the tenth-of-a-percent `tidy()` rounds the scale to.
  const room = await page.getByTestId("canvas-host").evaluate((el) => el.clientWidth);
  const drawn = await page
    .locator("[data-testid='canvas-host']")
    .locator(".cv-sheet")
    .evaluate((el) => el.getBoundingClientRect().width);
  expect(
    Math.abs(drawn - room),
    `expected the sheet to fill the canvas: drawn ${drawn}px in a ${room}px canvas`,
  ).toBeLessThanOrEqual(1);
  const fitted = (await readout.textContent()) ?? "";

  // #172's guarantee, re-asserted from the other side: the reader's own zoom is
  // not re-fitted away by the switch that unmounts the canvas.
  await page.getByRole("button", { name: "Zoom out" }).click();
  const chosen = (await readout.textContent()) ?? "";
  expect(chosen).not.toBe(fitted);

  await page.getByRole("button", { name: "PDF", exact: true }).click();
  await expect(page.getByTitle("Report preview")).toBeVisible();
  await page.getByRole("button", { name: "Sheet", exact: true }).click();
  await expect(page.getByTestId("canvas-host")).toBeVisible();

  // The remount renders one paint with the chosen zoom still in place, and only
  // a beat later would a broken flag's re-fit effect overwrite it. Asserting the
  // instant the switch lands could catch that first, correct paint and pass
  // regardless of what happens next — `settledZoom` waits for the readout to
  // stop moving instead of for a fixed budget, so it keeps discriminating no
  // matter how long a future regression's re-fit takes to land.
  expect(await settledZoom(page)).toBe(chosen);
});
