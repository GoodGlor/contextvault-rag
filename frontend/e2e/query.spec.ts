import { test, expect } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

/**
 * End-to-end: the query page as a chat with server-persisted memory.
 *
 * Everything runs against the *real* stack (auth, repository creation, the grant,
 * and the granted-repository listing all hit Postgres) — except the one piece that
 * would otherwise require a live, non-deterministic LLM: the `/query` call the
 * browser makes is intercepted at the browser boundary and fulfilled with a canned
 * grounded answer. Because that interception stops the request from ever reaching
 * the backend, the backend never gets a chance to append the turn to this user's
 * saved conversation either — so `/repositories/{id}/conversation` (GET) is
 * intercepted too, backed by an in-memory list this file appends to as each mocked
 * `/query` resolves. That stands in for the real per-user persistence and lets the
 * test prove, in a real browser, that (a) the exchange renders as chat bubbles, (b)
 * the client no longer sends a `history` field — the backend resolves follow-up
 * context from its own saved state — and (c) a full page reload restores the
 * conversation instead of wiping it. Backend memory threading (the "Conversation so
 * far" preamble, retrieval contextualisation, actual persistence) is covered by the
 * pytest suite.
 */

const ADMIN = { username: "admin", password: "adminpass123" };

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

function answer(text: string, sourceTitle: string) {
  return {
    answer: `${text} [1].`,
    not_in_vault: false,
    citations: [{ number: 1, chunk_id: "c1", source_id: "src-1", char_start: 0, char_end: 20 }],
    sources: [
      {
        id: "src-1",
        title: sourceTitle,
        original_filename: "policy.pdf",
        kind: "document",
        verified: false,
        author: null,
      },
    ],
  };
}

test("query page is a chat, and the server-side conversation survives a reload", async ({
  page,
}) => {
  await signIn(page);

  // --- Real backend setup: a repository the admin is granted on, so it shows up
  // in the query picker. ---
  const repoName = `Chat E2E ${Date.now()}`;
  await page.getByRole("link", { name: "Repositories" }).click();
  await page.getByLabel("Repository name").fill(repoName);
  await page.getByRole("button", { name: "Create repository" }).click();
  await expect(page.locator("li.repo-item", { hasText: repoName })).toBeVisible();

  // Grant the admin access to the new repo (Users page → Repository access).
  await page.getByRole("link", { name: "Users" }).click();
  const access = page.getByRole("region", { name: "Repository access" });
  await access.getByLabel("Repository").selectOption({ label: repoName });
  await access.getByLabel("Grant to").selectOption({ label: "admin" });
  await access.getByRole("button", { name: "Grant access" }).click();
  // The grant now lists the admin.
  await expect(access.getByRole("list")).toContainText("admin");

  // --- Intercept the LLM-backed /query call with a deterministic answer, and
  // record the request bodies so we can assert no `history` field is sent anymore.
  // Also intercept the saved-conversation GET so the reload assertion below has a
  // server-side store to restore from (see file header for why). ---
  const bodies: Array<Record<string, unknown>> = [];
  const answers = [
    answer("The retention period is 30 days", "Retention Policy"),
    answer("Part-timers receive pro-rated leave", "Retention Policy"),
  ];
  const savedTurns: Array<{
    question: string;
    answer: string;
    not_in_vault: boolean;
    citations: unknown[];
    sources: unknown[];
  }> = [];

  await page.route(/\/repositories\/[^/]+\/query$/, async (route: Route) => {
    const body = route.request().postDataJSON() as { question: string };
    bodies.push(body);
    const payload = answers[Math.min(bodies.length - 1, answers.length - 1)];
    savedTurns.push({
      question: body.question,
      answer: payload.answer,
      not_in_vault: payload.not_in_vault,
      citations: payload.citations,
      sources: payload.sources,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });
  await page.route(/\/repositories\/[^/]+\/conversation$/, async (route: Route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ turns: savedTurns }),
    });
  });

  // Back to the chat. The repository was just granted mid-session, but the sidebar
  // repo switcher loads its list once at app mount — so a full reload is needed for
  // the new grant to surface in it. Route intercepts set up above survive the reload.
  await page.getByRole("link", { name: "Ask" }).click();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
  await page.getByLabel("Repository").selectOption({ label: repoName });

  // --- First turn: ask, and see the exchange render as chat bubbles. ---
  const composer = page.getByLabel("Question");
  await composer.fill("How long is retention?");
  await page.getByRole("button", { name: "Send" }).click();

  // The question renders as a user bubble, the answer as an assistant bubble with
  // its cited source.
  await expect(
    page.locator(".msg-row.user .bubble", { hasText: "How long is retention?" }),
  ).toBeVisible();
  await expect(
    page.locator(".msg-row.assistant .bubble", { hasText: "The retention period is 30 days" }),
  ).toBeVisible();
  await expect(page.getByText("Retention Policy")).toBeVisible();

  // --- Follow-up turn: a question that only makes sense in light of the first one.
  // The client sends nothing but the new question; the grounded reply proves the
  // server, not the browser, is the one carrying the thread forward. ---
  await composer.fill("and for part-timers?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(
    page.locator(".msg-row.assistant .bubble", { hasText: "Part-timers receive pro-rated leave" }),
  ).toBeVisible();

  // Both prior bubbles are still on screen — this is a conversation, not a reset.
  await expect(
    page.locator(".msg-row.user .bubble", { hasText: "How long is retention?" }),
  ).toBeVisible();

  // Neither request carried a `history` field — the backend resolves it itself.
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toEqual({ question: "How long is retention?" });
  expect(bodies[1]).toEqual({ question: "and for part-timers?" });

  // --- Reload the page: the conversation is server-authoritative now, so it must
  // be restored — not wiped — once the repo is (re)selected. ---
  await page.reload();
  await expect(page.getByRole("heading", { name: "Ask a repository" })).toBeVisible();
  await page.getByLabel("Repository").selectOption({ label: repoName });

  await expect(
    page.locator(".msg-row.user .bubble", { hasText: "How long is retention?" }),
  ).toBeVisible();
  await expect(
    page.locator(".msg-row.assistant .bubble", { hasText: "The retention period is 30 days" }),
  ).toBeVisible();
  await expect(
    page.locator(".msg-row.user .bubble", { hasText: "and for part-timers?" }),
  ).toBeVisible();
  await expect(
    page.locator(".msg-row.assistant .bubble", { hasText: "Part-timers receive pro-rated leave" }),
  ).toBeVisible();

  // No third /query call was made by the reload/restore itself — restoring the
  // thread is a read (GET /conversation), not a re-ask.
  expect(bodies).toHaveLength(2);
});
