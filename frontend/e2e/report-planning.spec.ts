import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: the plan review loop — the one place a report waits for a human.
 *
 * Same philosophy as `custom-provider.spec.ts`: sign-in and repository creation
 * hit the real stack; everything that would need a live LLM and a connected
 * reporting database is intercepted at the browser boundary. What that leaves is
 * exactly what no other test covers — the *browser's* behaviour across a plan's
 * life: that a status the server changed is picked up by polling without a
 * reload, that an edit made after the last round survives to the approve call,
 * and that a blank sub-question is sent as a blank rather than being repaired on
 * the way out.
 *
 * The stub is a small state machine the test drives on purpose. Advancing it
 * from the test, rather than after N polls, is what makes each assertion about
 * the UI rather than about a timer.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

type Source = "database" | "documents";

/** A database section must name the database it reads (#203). An empty list is a
 *  blank the reader cannot approve, so a fixture standing in for a *server* plan
 *  has to carry one. The id need not resolve: the editor prints a name only when
 *  `report-databases` knows it, and prints nothing rather than a raw id. */
const DB_ID = "11111111-1111-1111-1111-111111111111";

interface PlanSection {
  title: string;
  // "" is a reader-added section's source, not yet chosen by the planner
  // — the page can send it, so the local type must describe it too.
  source: Source | "";
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
}

const ROUND_ONE: Plan = {
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
    {
      title: "Policy context",
      source: "documents",
      databases: [],
      columns: [],
      sub_question: "what do the documents say?",
    },
  ],
};

/** The report the stub is currently serving, plus what the page sent it. */
interface Harness {
  report: Report;
  replans: Plan[];
  approvals: Plan[];
  creates: string[];
}

async function signIn(page: Page) {
  // The app defaults to Ukrainian; pin English so the string assertions hold.
  await page.addInitScript(() => window.localStorage.setItem("contextvault.locale", "en"));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Username").fill(ADMIN.username);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
}

/** Intercept the report endpoints; return the harness the test drives. */
async function stubReports(page: Page): Promise<Harness> {
  const harness: Harness = {
    report: null as unknown as Report,
    replans: [],
    approvals: [],
    creates: [],
  };

  // Schedules are a separate feature and would otherwise 404 into an error banner
  // that hides the thing under test.
  await page.route(/\/repositories\/[^/]+\/schedules(\?.*)?$/, (route: Route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );

  await page.route(/\/repositories\/[^/]+\/reports(\?.*)?$/, async (route: Route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON() as { prompt: string };
      harness.creates.push(body.prompt);
      harness.report = {
        id: "11111111-1111-4111-8111-111111111111",
        repository_id: "repo",
        prompt: body.prompt,
        status: "planning",
        error: null,
        created_at: new Date("2026-08-04T10:00:00Z").toISOString(),
        has_pdf: false,
        has_pptx: false,
        schedule_id: null,
        plan: null,
        plan_rounds: 0,
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(harness.report),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report === null ? [] : [harness.report]),
    });
  });

  await page.route(/\/reports\/[^/]+\/replan$/, async (route: Route) => {
    harness.replans.push(route.request().postDataJSON() as Plan);
    // /replan answers immediately with the row back at `planning` and the *old*
    // plan still attached — the new round lands on a later poll. Modelled exactly,
    // because the page's provenance badges depend on that ordering.
    harness.report = { ...harness.report, status: "planning" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report),
    });
  });

  await page.route(/\/reports\/[^/]+\/approve$/, async (route: Route) => {
    harness.approvals.push(route.request().postDataJSON() as Plan);
    harness.report = { ...harness.report, status: "pending" };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(harness.report),
    });
  });

  return harness;
}

/** Create a repository through the real stack and land on its Reports page. */
async function openReports(page: Page): Promise<void> {
  const repoName = `Reports E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  await expect(page.locator("li.repo-item", { hasText: repoName })).toBeVisible();

  await page.getByRole("link", { name: "Reports" }).click();
  await expect(page.getByRole("heading", { name: "Reports" })).toBeVisible();
}

test("a plan is composed, edited, re-planned and approved without a reload", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page);
  await openReports(page);

  await expect(page.getByText("No reports yet. Request one above.")).toBeVisible();

  // --- request: the row appears at `planning`, and says so honestly ---
  await page.getByLabel("Prompt").fill("Quarterly revenue with policy context");
  await page.getByRole("button", { name: "Plan report" }).click();

  const row = page.locator("li.report-item");
  await expect(row).toBeVisible();
  await expect(row.locator("span.badge")).toHaveText("Planning");
  // "Generating…" next to a "Planning" badge would claim work that is not running.
  await expect(row.getByRole("status")).toHaveText("The LLM is composing the plan…");
  expect(harness.creates).toEqual(["Quarterly revenue with policy context"]);

  // --- the model answers: polling alone must surface the plan ---
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: ROUND_ONE,
    plan_rounds: 1,
  };

  const editor = page.locator("section.plan-editor");
  await expect(editor).toBeVisible();
  await expect(editor.getByRole("heading", { name: "Report plan" })).toBeVisible();
  await expect(editor.locator("p.plan-round")).toHaveText("Round 1");
  await expect(editor.locator("li.plan-section")).toHaveCount(2);
  await expect(editor.getByLabel("Report title")).toHaveValue("Quarterly market review");

  // Round one is entirely the model's, so nothing is attributed to anyone yet.
  await expect(editor.locator("span.plan-badge")).toHaveCount(0);

  // --- edit: rename a section, add one, and blank a sub-question ---
  const sections = editor.locator("li.plan-section");
  await sections.nth(0).getByLabel("Section title").fill("Revenue by region");
  await editor.getByRole("button", { name: "Add section" }).click();
  await expect(sections).toHaveCount(3);
  await sections.nth(2).getByLabel("Section title").fill("Outlook");

  // A blank sub-question is a *request*, not an oversight: it asks the model to
  // fill that one field. The page must send it blank rather than repairing it.
  await expect(sections.nth(2).getByLabel("Sub-question")).toHaveValue("");

  // While anything is blank, approval is blocked and the page says why.
  await expect(editor.locator("p.plan-hint")).toHaveText(
    "Fill in the title and every sub-question, or press Re-plan.",
  );
  await expect(editor.locator("button.plan-approve")).toBeDisabled();

  // --- re-plan: the edits go up verbatim, the blank stays blank ---
  await editor.getByRole("button", { name: "Re-plan with my edits" }).click();
  expect(harness.replans).toHaveLength(1);
  expect(harness.replans[0].sections.map((s) => s.title)).toEqual([
    "Revenue by region",
    "Policy context",
    "Outlook",
  ]);
  expect(harness.replans[0].sections[2].sub_question).toBe("");
  // The added section has no source yet either: the plan chooses it, the reader
  // never does, so the re-plan payload must carry it blank just like sub_question.
  expect(harness.replans[0].sections[2].source).toBe("");
  expect(harness.replans[0].title).toBe("Quarterly market review");

  // The row went back to `planning`; the editor is busy until the round lands.
  await expect(row.locator("span.badge")).toHaveText("Planning");

  // --- round two: the model filled the blank, kept the reader's words ---
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: {
      ...ROUND_ONE,
      sections: [
        {
          title: "Revenue by region",
          source: "database",
          databases: [DB_ID],
          columns: [],
          sub_question: "how did revenue move?",
        },
        {
          title: "Policy context",
          source: "documents",
          databases: [],
          columns: [],
          sub_question: "what do the documents say?",
        },
        {
          title: "Outlook",
          source: "documents",
          databases: [],
          columns: [],
          sub_question: "what is expected next quarter?",
        },
      ],
    },
    plan_rounds: 2,
  };

  await expect(editor.locator("p.plan-round")).toHaveText("Round 2");
  await expect(sections.nth(2).getByLabel("Sub-question")).toHaveValue(
    "what is expected next quarter?",
  );
  // Now provenance is meaningful: the model filled one field, the reader owns
  // the others.
  await expect(editor.locator("span.plan-badge-filled").first()).toBeVisible();

  // --- an edit made *after* the last round must survive to approve ---
  await sections.nth(0).getByLabel("Section title").fill("Revenue by market");
  await expect(editor.locator("button.plan-approve")).toBeEnabled();
  await editor.getByRole("button", { name: "Next →" }).click();
  await page.getByRole("button", { name: "Generate report" }).click();

  expect(harness.approvals).toHaveLength(1);
  expect(harness.approvals[0].sections[0].title).toBe("Revenue by market");
  expect(harness.approvals[0].sections).toHaveLength(3);

  // Approval starts generation, and the editor gets out of the way.
  await expect(row.locator("span.badge")).toHaveText("Pending");
  await expect(editor).toBeHidden();
  await expect(row.getByRole("status")).toHaveText("Generating…");
});

test("a section can be reordered and removed before approval", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page);
  await openReports(page);

  await page.getByLabel("Prompt").fill("Structure check");
  await page.getByRole("button", { name: "Plan report" }).click();
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: ROUND_ONE,
    plan_rounds: 1,
  };

  const editor = page.locator("section.plan-editor");
  const sections = editor.locator("li.plan-section");
  await expect(sections).toHaveCount(2);

  // Move section one down, so the order the reader sees is the order that is sent.
  // The reader can no longer re-route a section's source (the
  // plan chooses it, the reader only reads it), so this test only covers reorder
  // and remove — re-routing has its own coverage in "the section source is shown
  // but cannot be changed".
  await sections.nth(0).getByRole("button", { name: "Move down" }).click();
  await expect(sections.nth(1).getByLabel("Section title")).toHaveValue("Revenue by city");

  // Remove the other one.
  await sections.nth(0).getByRole("button", { name: "Remove section" }).click();
  await expect(sections).toHaveCount(1);

  await editor.getByRole("button", { name: "Next →" }).click();
  await page.getByRole("button", { name: "Generate report" }).click();
  expect(harness.approvals).toHaveLength(1);
  expect(harness.approvals[0].sections).toEqual([
    {
      title: "Revenue by city",
      source: "database",
      databases: [DB_ID],
      columns: [],
      sub_question: "how did revenue move?",
    },
  ]);
});

test("the section source is shown but cannot be changed", async ({ page }) => {
  // jsdom (the unit-test DOM) performs no layout and has no real accessibility
  // tree, so it cannot tell a visible chip from a hidden one, nor confirm that
  // no interactive source control is reachable by role. Only a real browser can
  // prove the source chip is actually on screen and actually inert.
  await signIn(page);
  const harness = await stubReports(page);
  await openReports(page);

  await page.getByLabel("Prompt").fill("Quarterly revenue with policy context");
  await page.getByRole("button", { name: "Plan report" }).click();
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: ROUND_ONE,
    plan_rounds: 1,
  };

  const editor = page.locator("section.plan-editor");
  await expect(editor).toBeVisible();

  const chip = page.locator(".plan-source-chosen").first();
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText(/БД|Database/);
  await expect(page.locator(".plan-source-option")).toHaveCount(0);
  await expect(page.getByRole("group", { name: /Джерело|Source/ })).toHaveCount(0);
});

test("a re-plan that fails leaves the plan on screen with the reason", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page);
  await openReports(page);

  await page.getByLabel("Prompt").fill("Failing replan");
  await page.getByRole("button", { name: "Plan report" }).click();
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: ROUND_ONE,
    plan_rounds: 1,
  };

  const editor = page.locator("section.plan-editor");
  await expect(editor).toBeVisible();

  // The server rejects the round. The plan must stay editable: hiding it would
  // leave the reader re-pressing a button that has already said why it will not
  // work.
  await page.route(/\/reports\/[^/]+\/replan$/, (route: Route) =>
    route.fulfill({
      status: 429,
      contentType: "application/json",
      body: JSON.stringify({ detail: "The model is rate limited. Try again in a minute." }),
    }),
  );

  await editor.getByRole("button", { name: "Re-plan with my edits" }).click();
  await expect(editor.locator("p.error")).toHaveText(
    "The model is rate limited. Try again in a minute.",
  );
  await expect(editor.locator("li.plan-section")).toHaveCount(2);
  await expect(editor.locator("button.plan-approve")).toBeEnabled();
});

test("the reader designs the report before it is generated", async ({ page }) => {
  await signIn(page);
  const harness = await stubReports(page);

  // Both endpoints record into ONE list, because the claim under test is their
  // order relative to each other. Recording only the design calls would leave
  // "before /approve" asserted nowhere — the approve would not appear at all,
  // and every ordering assertion over the list would hold whatever it did.
  const calls: string[] = [];
  await page.route(/\/reports\/[^/]+\/approve$/, async (route: Route) => {
    calls.push("POST approve");
    await route.fallback();
  });
  await page.route(/\/reports\/[^/]+\/design$/, async (route: Route) => {
    const method = route.request().method();
    calls.push(`${method} design`);
    if (method === "PUT") {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      // The endpoint would answer 422 for a block list on a report with no
      // stored sections. Never sending one keeps that reason on this side.
      expect("blocks" in body).toBe(false);
      expect(body.template).toBe("prezydia");
      await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        template: null,
        blocks_version: 1,
        blocks: [],
        stored: false,
        sections: [],
        title: null,
        typography: {},
        page: {},
        brand: {},
      }),
    });
  });

  await openReports(page);
  await page.getByLabel("Prompt").fill("Quarterly revenue with policy context");
  await page.getByRole("button", { name: "Plan report" }).click();
  harness.report = {
    ...harness.report,
    status: "awaiting_approval",
    plan: ROUND_ONE,
    plan_rounds: 1,
  };
  await expect(page.locator("section.plan-editor")).toBeVisible();

  await page.getByRole("button", { name: "Next →" }).click();
  await expect(page.getByText("Step 2 — Design")).toBeVisible();
  await page.getByRole("button", { name: "Design template" }).click();
  await page.getByRole("button", { name: /Prezydia|Президія/ }).click();
  await expect(page.getByText(/Chosen:/)).toBeVisible();
  await page.getByRole("button", { name: "Generate report" }).click();

  // The order is the point: the design is stored before the report is built, so
  // the first artifact already wears it. `_bind_style` is what then honours it —
  // proved backend-side by
  // tests/test_reports_service.py::test_a_design_already_on_the_row_beats_the_repository_default.
  // This spec cannot prove it: there is no renderer behind these routes.
  await expect.poll(() => harness.approvals.length).toBe(1);
  expect(calls.filter((c) => c === "PUT design")).toHaveLength(1);
  // Both indexes are asserted present first. `indexOf` answers -1 for a call
  // that never happened, and -1 is less than every real index, so a bare
  // `toBeLessThan` would pass most loudly in exactly the case it exists to
  // catch: the design never stored at all.
  expect(calls).toContain("PUT design");
  expect(calls).toContain("POST approve");
  expect(calls.indexOf("PUT design")).toBeLessThan(calls.indexOf("POST approve"));
});
