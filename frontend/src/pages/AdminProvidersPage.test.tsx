import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminProvidersPage } from "./AdminProvidersPage";
import type { ProviderStatus } from "../api/providers";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ROWS: ProviderStatus[] = [
  {
    provider: "anthropic",
    configured: false,
    verified: false,
    api_key_masked: null,
    base_url: null,
  },
  {
    provider: "openai",
    configured: true,
    verified: true,
    api_key_masked: "sk-…•••4f2a",
    base_url: null,
  },
  { provider: "gemini", configured: false, verified: false, api_key_masked: null, base_url: null },
  {
    provider: "openrouter",
    configured: false,
    verified: false,
    api_key_masked: null,
    base_url: null,
  },
];

const CUSTOM_ROW: ProviderStatus = {
  provider: "custom",
  configured: false,
  verified: false,
  api_key_masked: null,
  base_url: null,
};

describe("AdminProvidersPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function mock(opts: { rows?: ProviderStatus[]; saveStatus?: number }) {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/admin/providers") && method === "GET") {
        return Promise.resolve(json(opts.rows ?? ROWS));
      }
      const m = /\/admin\/providers\/([^/]+)$/.exec(url);
      if (m && method === "PUT") {
        if (opts.saveStatus && opts.saveStatus >= 400) {
          return Promise.resolve(
            json({ detail: "Could not list models: invalid key" }, opts.saveStatus),
          );
        }
        return Promise.resolve(
          json({ provider: m[1], configured: true, verified: true, api_key_masked: "sk-…•••9z9z" }),
        );
      }
      if (m && method === "DELETE") return Promise.resolve(new Response(null, { status: 204 }));
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("lists every provider with its key status", async () => {
    mock({});
    render(<AdminProvidersPage />);
    expect(await screen.findByText("OpenAI")).toBeInTheDocument();
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("Google (Gemini)")).toBeInTheDocument();
    expect(screen.getByText("OpenRouter")).toBeInTheDocument();
    // OpenAI has a verified key; the others are not set.
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.getAllByText("Not set")).toHaveLength(3);
    expect(screen.getByText(/sk-…•••4f2a/)).toBeInTheDocument();
  });

  it("verifies and saves a key, flipping the provider to Verified", async () => {
    mock({});
    render(<AdminProvidersPage />);
    const anthropic = (await screen.findByText("Anthropic")).closest("li") as HTMLElement;

    await userEvent.type(within(anthropic).getByLabelText("API key"), "sk-ant-123");
    await userEvent.click(within(anthropic).getByRole("button", { name: "Save key" }));

    const put = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PUT");
    expect(String(put?.[0])).toContain("/admin/providers/anthropic");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ api_key: "sk-ant-123", base_url: null });
    expect(await within(anthropic).findByText("Verified")).toBeInTheDocument();
  });

  it("surfaces the provider's error when a key fails verification", async () => {
    mock({ saveStatus: 400 });
    render(<AdminProvidersPage />);
    const gemini = (await screen.findByText("Google (Gemini)")).closest("li") as HTMLElement;

    await userEvent.type(within(gemini).getByLabelText("API key"), "bad");
    await userEvent.click(within(gemini).getByRole("button", { name: "Save key" }));

    expect(await within(gemini).findByText(/could not list models/i)).toBeInTheDocument();
  });

  it("removes a stored key", async () => {
    mock({});
    render(<AdminProvidersPage />);
    const openai = (await screen.findByText("OpenAI")).closest("li") as HTMLElement;

    await userEvent.click(within(openai).getByRole("button", { name: "Remove key" }));

    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "DELETE");
    expect(String(del?.[0])).toContain("/admin/providers/openai");
    expect(await within(openai).findByText("Not set")).toBeInTheDocument();
  });

  it("renders a Base URL field and embeddings note for the custom provider, and saves with an optional key", async () => {
    mock({ rows: [...ROWS, CUSTOM_ROW] });
    render(<AdminProvidersPage />);
    const custom = (await screen.findByText("Custom (local / self-hosted)")).closest(
      "li",
    ) as HTMLElement;

    expect(within(custom).getByLabelText("Base URL")).toBeInTheDocument();
    expect(
      within(custom).getByText(
        "Chat, reports, and image reading run on your server. Document embedding still uses Gemini in this version.",
      ),
    ).toBeInTheDocument();
    expect(within(custom).getByLabelText("API key (optional)")).toBeInTheDocument();

    await userEvent.type(within(custom).getByLabelText("Base URL"), "http://localhost:11434/v1");
    await userEvent.click(within(custom).getByRole("button", { name: "Save key" }));

    const put = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PUT");
    expect(String(put?.[0])).toContain("/admin/providers/custom");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      api_key: null,
      base_url: "http://localhost:11434/v1",
    });
  });
});
