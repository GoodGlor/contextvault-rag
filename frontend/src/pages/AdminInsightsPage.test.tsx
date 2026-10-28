import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminInsightsPage } from "./AdminInsightsPage";
import { renderWithRepo, repoValue } from "../test/renderWithRepo";
import type { KnowledgeGap, GapRejection } from "../api/knowledgeGaps";
import type { AnalyticsOverview } from "../api/analytics";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const GAPS: KnowledgeGap[] = [
  {
    question: "How do I reset 2FA?",
    ask_count: 5,
    user_count: 3,
    last_asked_at: "2026-07-19T10:00:00Z",
  },
  {
    question: "What is the PTO policy?",
    ask_count: 2,
    user_count: 2,
    last_asked_at: "2026-07-18T10:00:00Z",
  },
];

const ANALYTICS: AnalyticsOverview = {
  total_queries: 100,
  answered: 80,
  not_in_vault: 20,
  not_in_vault_rate: 0.2,
  per_repository: [
    { repository_id: "r-1", repository_name: "Handbook", query_count: 100, not_in_vault_count: 20 },
  ],
  top_questions: [{ question: "How do I reset 2FA?", ask_count: 5 }],
  active_users: [{ user_id: "u-1", username: "member", query_count: 12 }],
  by_day: [{ day: "2026-07-19", total: 50, not_in_vault: 10 }],
};

describe("AdminInsightsPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function mock(opts: { gaps?: KnowledgeGap[]; rejected?: GapRejection[] } = {}) {
    const gaps = opts.gaps ?? GAPS;
    const rejected = opts.rejected ?? [];
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/knowledge-gaps/rejected")) return Promise.resolve(json(rejected));
      if (url.includes("/knowledge-gaps/reject") && method === "POST") {
        const body = JSON.parse(String(init?.body)) as { question: string; reason: string };
        return Promise.resolve(
          json(
            {
              question: body.question,
              reason: body.reason,
              rejected_by: "admin",
              rejected_at: "2026-07-20T10:00:00Z",
            },
            201,
          ),
        );
      }
      if (url.includes("/knowledge-gaps")) return Promise.resolve(json(gaps));
      if (url.includes("/analytics")) return Promise.resolve(json(ANALYTICS));
      if (/\/repositories\/[^/]+\/admin-notes$/.test(url) && method === "POST") {
        const body = JSON.parse(String(init?.body)) as { title: string; content: string };
        return Promise.resolve(
          json(
            {
              id: "s-note",
              repository_id: "r-1",
              kind: "admin_note",
              title: body.title,
              original_filename: null,
              status: "pending",
              ingest_error: null,
              created_at: "2026-07-20T10:00:00Z",
            },
            201,
          ),
        );
      }
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("lists the selected repository's ranked knowledge gaps", async () => {
    mock();
    renderWithRepo(<AdminInsightsPage />);
    const gaps = await screen.findByRole("region", { name: "Knowledge gaps" });
    expect(await within(gaps).findByText("How do I reset 2FA?")).toBeInTheDocument();
    expect(within(gaps).getByText("What is the PTO policy?")).toBeInTheDocument();
    // The demand signal (ask count) is shown.
    expect(within(gaps).getByText(/5/)).toBeInTheDocument();
  });

  it("answers a gap by writing an Admin Note prefilled with the question", async () => {
    mock();
    renderWithRepo(<AdminInsightsPage />);
    const gaps = await screen.findByRole("region", { name: "Knowledge gaps" });
    const row = (await within(gaps).findByText("How do I reset 2FA?")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Answer this gap" }));

    // The note title is prefilled with the gap's question.
    const title = within(row).getByLabelText("Note title") as HTMLInputElement;
    expect(title.value).toBe("How do I reset 2FA?");

    await userEvent.type(within(row).getByLabelText("Answer"), "Go to Settings → Security.");
    await userEvent.click(within(row).getByRole("button", { name: "Save Admin Note" }));

    const posted = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).includes("/admin-notes"),
    );
    expect(String(posted?.[0])).toContain("/repositories/r-1/admin-notes");
    expect(JSON.parse(String(posted?.[1]?.body))).toEqual({
      title: "How do I reset 2FA?",
      content: "Go to Settings → Security.",
    });
    expect(await within(gaps).findByText(/saved/i)).toBeInTheDocument();
  });

  it("shows the analytics overview", async () => {
    mock();
    renderWithRepo(<AdminInsightsPage />);
    const analytics = await screen.findByRole("region", { name: "Analytics" });
    // Totals + gap rate.
    expect(within(analytics).getByText("Total queries")).toBeInTheDocument();
    expect(within(analytics).getByText("20%")).toBeInTheDocument();
    // Per-repository volume, a top question, and an active user.
    expect(within(analytics).getByText("Handbook")).toBeInTheDocument();
    expect(within(analytics).getByText("How do I reset 2FA?")).toBeInTheDocument();
    expect(within(analytics).getByText("member")).toBeInTheDocument();
  });

  it("rejects a gap with a required reason and removes it from the list", async () => {
    mock({
      gaps: [
        {
          question: "What is the VPN?",
          ask_count: 2,
          user_count: 1,
          last_asked_at: "2026-07-19T10:00:00Z",
        },
      ],
    });
    renderWithRepo(<AdminInsightsPage />);
    await screen.findByText("What is the VPN?");
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    // confirm blocked while reason empty:
    expect(screen.getByRole("button", { name: "Confirm rejection" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason for rejecting"), "Out of scope");
    await userEvent.click(screen.getByRole("button", { name: "Confirm rejection" }));
    expect(screen.queryByText("What is the VPN?")).not.toBeInTheDocument();
    // "/knowledge-gaps/reject" is also a substring of the GET "/knowledge-gaps/rejected"
    // call made on mount, so also filter on method to find the POST specifically.
    const call = fetchMock.mock.calls.find(
      (c) => String(c[0]).includes("/knowledge-gaps/reject") && (c[1]?.method ?? "GET") === "POST",
    );
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      question: "What is the VPN?",
      reason: "Out of scope",
    });
  });

  it("surfaces a repository-list load failure from the shared context", async () => {
    mock();
    renderWithRepo(
      <AdminInsightsPage />,
      repoValue({ error: "Could not load your repositories." }),
    );
    expect(await screen.findByText("Could not load your repositories.")).toBeInTheDocument();
  });
});
