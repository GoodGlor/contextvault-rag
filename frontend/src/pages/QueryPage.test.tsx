import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { QueryPage } from "./QueryPage";
import type { QueryResult } from "../api/query";
import type { SavedConversation } from "../api/conversations";
import { renderWithRepo, repoValue } from "../test/renderWithRepo";
import { RepositoryContext } from "../repository/RepositoryContext";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const CITED: QueryResult = {
  answer: "Retention is 30 days [1] and reviewed quarterly [2].",
  not_in_vault: false,
  citations: [
    { number: 1, chunk_id: "c1", source_id: "s-1", char_start: 0, char_end: 40 },
    { number: 2, chunk_id: "c2", source_id: "s-2", char_start: 41, char_end: 80 },
  ],
  sources: [
    {
      id: "s-1",
      title: "Retention Policy",
      original_filename: "policy.pdf",
      kind: "document",
      verified: false,
      author: null,
    },
    {
      id: "s-2",
      title: "Quarterly review note",
      original_filename: null,
      kind: "admin_note",
      verified: true,
      author: "ada",
    },
  ],
};

describe("QueryPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  // The repo comes from the seeded RepositoryContext now, not a page-level fetch;
  // this mock only ever needs to answer /conversation and /query.
  function mock(result?: QueryResult, conversation: SavedConversation = { turns: [] }) {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("/query")) return Promise.resolve(json(result ?? CITED));
      if (url.includes("/conversation")) {
        if (init?.method === "DELETE") return Promise.resolve(new Response(null, { status: 204 }));
        return Promise.resolve(json(conversation));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
  }

  it("asks a question and renders the cited answer, verified badge, and sources", async () => {
    mock();
    renderWithRepo(<QueryPage />);
    await screen.findByLabelText("Question");

    await userEvent.type(screen.getByLabelText("Question"), "How long is retention?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await screen.findByText(/Retention is 30 days/);
    expect(screen.getByText("Retention Policy")).toBeInTheDocument();
    expect(screen.getByText("Quarterly review note")).toBeInTheDocument();
    // The admin-note source is flagged Verified and attributed.
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.getByText(/by ada/)).toBeInTheDocument();
    // Posted to the current (context) repository.
    const posted = fetchMock.mock.calls.find((c) => String(c[0]).includes("/query"));
    expect(String(posted?.[0])).toContain("/repositories/r-1/query");
  });

  it("sends only the question — the backend resolves conversation history server-side", async () => {
    mock();
    renderWithRepo(<QueryPage />);
    await screen.findByLabelText("Question");

    // First turn.
    await userEvent.type(screen.getByLabelText("Question"), "What is the PTO policy?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/Retention is 30 days/);

    // Follow-up turn.
    await userEvent.type(screen.getByLabelText("Question"), "and for part-timers?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const queryCalls = fetchMock.mock.calls.filter((c) => String(c[0]).includes("/query"));
    expect(queryCalls).toHaveLength(2);
    expect(JSON.parse(String(queryCalls[0][1]?.body))).toEqual({
      question: "What is the PTO policy?",
    });
    expect(JSON.parse(String(queryCalls[1][1]?.body))).toEqual({
      question: "and for part-timers?",
    });
  });

  it("starts a fresh conversation when the current repository changes", async () => {
    mock();
    const { rerender } = renderWithRepo(<QueryPage />);
    await screen.findByLabelText("Question");

    await userEvent.type(screen.getByLabelText("Question"), "How long is retention?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/Retention is 30 days/);

    // The repo switcher now lives in the sidebar; simulate it changing the shared
    // context. That reloads this repository's own saved conversation (empty here),
    // replacing the prior repo's transcript...
    rerender(
      <MemoryRouter>
        <RepositoryContext.Provider value={repoValue({ currentRepoId: "r-2" })}>
          <QueryPage />
        </RepositoryContext.Provider>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.queryByText(/Retention is 30 days/)).not.toBeInTheDocument();
    });

    // ...so the next question carries no history in the request body.
    await userEvent.type(screen.getByLabelText("Question"), "fresh start?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    const lastQuery = fetchMock.mock.calls.filter((c) => String(c[0]).includes("/query")).at(-1);
    expect(String(lastQuery?.[0])).toContain("/repositories/r-2/query");
    expect(JSON.parse(String(lastQuery?.[1]?.body))).toEqual({
      question: "fresh start?",
    });
  });

  it("restores the saved conversation on load", async () => {
    mock(undefined, {
      turns: [
        {
          question: "Saved question?",
          answer: "This is the saved answer from a prior session.",
          not_in_vault: false,
          citations: [],
          sources: [],
        },
      ],
    });
    renderWithRepo(<QueryPage />);
    expect(await screen.findByText("Saved question?")).toBeInTheDocument();
    expect(screen.getByText(/saved answer/i)).toBeInTheDocument();
  });

  it("clears the conversation when Clear is clicked", async () => {
    mock(undefined, {
      turns: [
        {
          question: "Saved question?",
          answer: "This is the saved answer from a prior session.",
          not_in_vault: false,
          citations: [],
          sources: [],
        },
      ],
    });
    renderWithRepo(<QueryPage />);
    await screen.findByText("Saved question?");
    await userEvent.click(screen.getByRole("button", { name: "Clear conversation" }));
    expect(screen.queryByText("Saved question?")).not.toBeInTheDocument();
    const del = fetchMock.mock.calls.find((c) => c[1]?.method === "DELETE");
    expect(String(del?.[0])).toContain("/repositories/r-1/conversation");
  });

  it("highlights the matching source when a citation is clicked", async () => {
    mock();
    renderWithRepo(<QueryPage />);
    await screen.findByLabelText("Question");
    await userEvent.type(screen.getByLabelText("Question"), "retention?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByText(/Retention is 30 days/);

    expect(screen.getByTestId("source-s-1")).not.toHaveClass("highlighted");
    await userEvent.click(screen.getByRole("button", { name: "Jump to source 1" }));
    expect(screen.getByTestId("source-s-1")).toHaveClass("highlighted");
    expect(screen.getByTestId("source-s-2")).not.toHaveClass("highlighted");
  });

  it("shows an explicit not-in-vault state", async () => {
    mock({
      answer: "I could not find this in the vault.",
      not_in_vault: true,
      citations: [],
      sources: [],
    });
    renderWithRepo(<QueryPage />);
    await screen.findByLabelText("Question");
    await userEvent.type(screen.getByLabelText("Question"), "unknown thing?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/not in this vault/i);
  });

  it("tells a user with no grants they have no repositories", async () => {
    renderWithRepo(<QueryPage />, repoValue({ repos: [], currentRepoId: "" }));
    expect(await screen.findByText(/don’t have access to any repositories/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Question")).not.toBeInTheDocument();
  });
});
