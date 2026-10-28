import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminRepositoriesPage } from "./AdminRepositoriesPage";
import type { AdminRepository, LLMConfig, LLMProvider } from "../api/repositories";
import type { TemplateChoice } from "../api/reports";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const REPOS: AdminRepository[] = [
  { id: "r-1", name: "Handbook", description: "the company handbook", configured: true },
  { id: "r-2", name: "Runbook", description: null, configured: false },
];

const UNCONFIGURED: LLMConfig = { provider: null, model: null, configured: false };
const CONFIGURED: LLMConfig = { provider: "openai", model: "gpt-4o", configured: true };

const ALL_PROVIDERS: LLMProvider[] = ["anthropic", "openai", "gemini", "openrouter", "custom"];

const TEMPLATES: TemplateChoice[] = [
  { key: "kvarta", name: "Кварта", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
  { key: "chasopys", name: "Часопис", paper: "#f7f7f5", ink: "#16181a", accent: "#0a5f56" },
];

/** Provider-status rows, marking `verified` ones as configured+verified. */
function providerRows(verified: LLMProvider[]) {
  return ALL_PROVIDERS.map((p) => ({
    provider: p,
    configured: verified.includes(p),
    verified: verified.includes(p),
    api_key_masked: verified.includes(p) ? "sk-…•••4f2a" : null,
  }));
}

describe("AdminRepositoriesPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  /** Route every call the page makes. `verified` lists providers with a stored key. */
  function mock(opts: {
    repos?: AdminRepository[];
    configs?: Record<string, LLMConfig>;
    created?: AdminRepository;
    models?: string[];
    modelsStatus?: number;
    verified?: LLMProvider[];
  }) {
    const repos = opts.repos ?? REPOS;
    const configs = opts.configs ?? {};
    const verified = opts.verified ?? (["openai"] as LLMProvider[]);
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/admin/providers")) return Promise.resolve(json(providerRows(verified)));
      if (url.endsWith("/report-templates")) return Promise.resolve(json(TEMPLATES));
      if (/\/repositories\/([^/]+)\/report-template$/.test(url)) {
        return Promise.resolve(json({ template: null }));
      }
      if (/\/repositories\/([^/]+)\/llm-models$/.test(url) && method === "POST") {
        if (opts.modelsStatus && opts.modelsStatus >= 400) {
          return Promise.resolve(
            json({ detail: "Could not list models: invalid key" }, opts.modelsStatus),
          );
        }
        return Promise.resolve(json({ models: opts.models ?? [] }));
      }
      const m = /\/repositories\/([^/]+)\/llm-config$/.exec(url);
      if (m) {
        if (method === "PUT") {
          const body = JSON.parse(String(init?.body)) as { provider: string; model: string };
          return Promise.resolve(
            json({ provider: body.provider, model: body.model, configured: true }),
          );
        }
        return Promise.resolve(json(configs[m[1]] ?? UNCONFIGURED));
      }
      if (url.endsWith("/admin/repositories")) return Promise.resolve(json(repos));
      if (url.endsWith("/repositories") && method === "POST") {
        return Promise.resolve(json(opts.created ?? REPOS[0], 201));
      }
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("lists every repository with its configured state", async () => {
    mock({});
    render(<AdminRepositoriesPage />);
    expect(await screen.findByText("Handbook")).toBeInTheDocument();
    expect(screen.getByText("Runbook")).toBeInTheDocument();
    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });

  it("creates a repository and shows it in the list", async () => {
    const created: AdminRepository = {
      id: "r-3",
      name: "Wiki",
      description: "internal wiki",
      configured: false,
    };
    mock({ repos: [], created });
    render(<AdminRepositoriesPage />);
    await screen.findByRole("button", { name: "Create repository" });

    await userEvent.type(screen.getByLabelText("Repository name"), "Wiki");
    await userEvent.type(screen.getByLabelText("Description"), "internal wiki");
    await userEvent.click(screen.getByRole("button", { name: "Create repository" }));

    const posted = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/repositories"),
    );
    expect(JSON.parse(String(posted?.[1]?.body))).toMatchObject({
      name: "Wiki",
      description: "internal wiki",
    });
    expect(await screen.findByText("Wiki")).toBeInTheDocument();
  });

  it("configures an unconfigured repo: pick a verified provider, models auto-load, save", async () => {
    mock({
      configs: { "r-2": UNCONFIGURED },
      models: ["claude-opus-4-8", "claude-sonnet-5"],
      verified: ["anthropic"],
    });
    render(<AdminRepositoriesPage />);
    const runbook = (await screen.findByText("Runbook")).closest("li") as HTMLElement;
    await userEvent.click(within(runbook).getByRole("button", { name: "Configure" }));

    // No verified provider is preselected as OpenAI here — anthropic is the only one
    // with a key, so it's the default. Models auto-load for it (no key entry here).
    const modelSelect = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    await userEvent.selectOptions(modelSelect, "claude-opus-4-8");
    expect(modelSelect.value).toBe("claude-opus-4-8");
    // The repo config never asks for a key — keys live in the Providers tab.
    expect(screen.queryByLabelText("API key")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    const put = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PUT");
    expect(String(put?.[0])).toContain("/repositories/r-2/llm-config");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      provider: "anthropic",
      model: "claude-opus-4-8",
    });
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("auto-loads the model list for a configured repo and changes the model", async () => {
    mock({
      configs: { "r-1": CONFIGURED },
      models: ["gpt-4o", "gpt-4o-mini"],
      verified: ["openai"],
    });
    render(<AdminRepositoriesPage />);
    const handbook = (await screen.findByText("Handbook")).closest("li") as HTMLElement;
    await userEvent.click(within(handbook).getByRole("button", { name: "Configure" }));

    // The Model dropdown holds the current model preselected, and the alternatives.
    const modelSelect = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    expect(modelSelect.value).toBe("gpt-4o");
    expect(within(modelSelect).getByRole("option", { name: "gpt-4o-mini" })).toBeInTheDocument();

    // The list-models call sends only the provider — the global key is used server-side.
    const post = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).includes("/llm-models"),
    );
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ provider: "openai" });

    await userEvent.selectOptions(modelSelect, "gpt-4o-mini");
    await userEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    const put = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      provider: "openai",
      model: "gpt-4o-mini",
    });
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("disables a provider that has no key", async () => {
    mock({ configs: { "r-2": UNCONFIGURED }, models: ["gpt-4o"], verified: ["openai"] });
    render(<AdminRepositoriesPage />);
    const runbook = (await screen.findByText("Runbook")).closest("li") as HTMLElement;
    await userEvent.click(within(runbook).getByRole("button", { name: "Configure" }));

    const providerSelect = (await screen.findByLabelText("Provider")) as HTMLSelectElement;
    // OpenAI is usable; a keyless provider (Gemini) is disabled.
    const gemini = within(providerSelect).getByRole("option", {
      name: /Gemini/,
    }) as HTMLOptionElement;
    expect(gemini.disabled).toBe(true);
  });

  it("tells the admin to set up a provider when none is verified", async () => {
    mock({ configs: { "r-2": UNCONFIGURED }, verified: [] });
    render(<AdminRepositoriesPage />);
    const runbook = (await screen.findByText("Runbook")).closest("li") as HTMLElement;
    await userEvent.click(within(runbook).getByRole("button", { name: "Configure" }));

    expect(await screen.findByText(/Providers tab/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
  });

  it("uses a free-text model input with datalist suggestions for the custom provider", async () => {
    mock({ configs: { "r-2": UNCONFIGURED }, models: [], verified: ["custom"] });
    render(<AdminRepositoriesPage />);
    const runbook = (await screen.findByText("Runbook")).closest("li") as HTMLElement;
    await userEvent.click(within(runbook).getByRole("button", { name: "Configure" }));

    // Custom is the only verified provider, so it's preselected — no dropdown needed.
    // With no models returned (no /v1/models on this server), the field must still be
    // usable: a free-text input, not a dead empty <select>.
    const modelInput = (await screen.findByLabelText("Model")) as HTMLInputElement;
    expect(modelInput.tagName).toBe("INPUT");

    await userEvent.type(modelInput, "llama3.1:8b");
    await userEvent.click(screen.getByRole("button", { name: "Save configuration" }));

    const put = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({
      provider: "custom",
      model: "llama3.1:8b",
    });
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("surfaces an error when loading models fails", async () => {
    mock({ configs: { "r-2": UNCONFIGURED }, modelsStatus: 400, verified: ["openai"] });
    render(<AdminRepositoriesPage />);
    const runbook = (await screen.findByText("Runbook")).closest("li") as HTMLElement;
    await userEvent.click(within(runbook).getByRole("button", { name: "Configure" }));

    // Auto-load fires for the verified provider and fails.
    expect(await screen.findByText(/could not list models/i)).toBeInTheDocument();
  });
});
