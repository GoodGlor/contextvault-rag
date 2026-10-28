import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getConversation, clearConversation } from "./conversations";

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("conversations api", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("GETs the saved conversation", async () => {
    fetchMock.mockResolvedValue(
      json({
        turns: [{ question: "q", answer: "a", not_in_vault: false, citations: [], sources: [] }],
      }),
    );
    const res = await getConversation("r-1");
    expect(res.turns[0].question).toBe("q");
    expect(String(fetchMock.mock.calls[0][0])).toContain("/repositories/r-1/conversation");
  });

  it("DELETEs the saved conversation", async () => {
    fetchMock.mockResolvedValue(json(null, 204));
    await clearConversation("r-1");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
  });
});
