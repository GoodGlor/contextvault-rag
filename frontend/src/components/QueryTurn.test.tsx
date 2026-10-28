import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryTurn } from "./QueryTurn";
import type { QueryResult } from "../api/query";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const RESULT: QueryResult = {
  answer: "Retention is 30 days [1].",
  not_in_vault: false,
  citations: [{ number: 1, chunk_id: "c1", source_id: "s-1", char_start: 0, char_end: 20 }],
  sources: [
    {
      id: "s-1",
      title: "policy.pdf",
      original_filename: "policy.pdf",
      kind: "document",
      verified: false,
      author: null,
    },
  ],
};

describe("QueryTurn — view cited passage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("loads and shows a cited source's passage on demand", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (/\/repositories\/r-1\/sources\/s-1$/.test(url)) {
        return Promise.resolve(
          json({
            id: "s-1",
            repository_id: "r-1",
            title: "policy.pdf",
            kind: "document",
            content: "Retention is 30 days.",
          }),
        );
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<QueryTurn question="How long is retention?" result={RESULT} repositoryId="r-1" />);
    const source = screen.getByTestId("source-s-1");

    await userEvent.click(within(source).getByRole("button", { name: "View passage" }));

    // The passage text is fetched from the user-scoped source-content endpoint.
    expect(await within(source).findByText("Retention is 30 days.")).toBeInTheDocument();
    const got = fetchMock.mock.calls.find((c) => String(c[0]).includes("/sources/s-1"));
    expect(String(got?.[0])).toContain("/repositories/r-1/sources/s-1");
  });
});
