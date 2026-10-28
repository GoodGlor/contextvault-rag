import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SourcesPanel, SOURCE_POLL_MS } from "./SourcesPanel";
import { renderWithRepo, repoValue } from "../../test/renderWithRepo";
import type { Source } from "../../api/sources";

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function source(overrides: Partial<Source> = {}): Source {
  return {
    id: "s-1",
    repository_id: "r-1",
    kind: "document",
    title: "policy.pdf",
    original_filename: "policy.pdf",
    source_url: null,
    status: "done",
    ingest_error: null,
    chunks_total: null,
    chunks_done: null,
    created_at: "2026-07-20T10:00:00Z",
    ...overrides,
  };
}

describe("SourcesPanel", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  /** `sources` may be a static array or a function of the (0-based) list-call index. */
  function mock(opts: {
    sources?: Source[] | ((call: number) => Source[]);
    uploaded?: Source;
    webAdded?: Source;
  }) {
    let listCall = 0;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (/\/repositories\/[^/]+\/web-sources$/.test(url) && method === "POST") {
        return Promise.resolve(
          json(opts.webAdded ?? source({ kind: "web", status: "pending" }), 201),
        );
      }
      if (/\/repositories\/[^/]+\/sources$/.test(url)) {
        if (method === "POST") {
          if (opts.uploaded) return Promise.resolve(json(opts.uploaded, 201));
          // Derive a unique source per upload from the posted file, so multi-file
          // uploads yield distinct rows (distinct ids/titles) rather than collide.
          const posted = (init?.body as FormData).get("file") as File;
          return Promise.resolve(
            json(source({ id: `s-${posted.name}`, title: posted.name, status: "pending" }), 201),
          );
        }
        const src = opts.sources ?? [];
        let list: Source[];
        try {
          list = typeof src === "function" ? src(listCall++) : src;
        } catch (err) {
          // A `sources` callback may throw to simulate a failed poll.
          return Promise.reject(err instanceof Error ? err : new Error(String(err)));
        }
        return Promise.resolve(json(list));
      }
      if (/\/sources\/[^/]+$/.test(url) && method === "DELETE") {
        return Promise.resolve(json(null, 204));
      }
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("lists the selected repository's sources with their status", async () => {
    mock({ sources: [source({ id: "s-1", title: "policy.pdf", status: "done" })] });
    renderWithRepo(<SourcesPanel />);
    expect(await screen.findByText("policy.pdf")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it("uploads a document and shows it in the list", async () => {
    mock({
      sources: [],
      uploaded: source({ id: "s-9", title: "handbook.pdf", status: "pending" }),
    });
    renderWithRepo(<SourcesPanel />);
    await screen.findByRole("button", { name: "Upload" });

    const file = new File(["hello"], "handbook.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Documents"), file);
    await userEvent.click(screen.getByRole("button", { name: "Upload" }));

    const posted = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/sources"),
    );
    expect(posted?.[1]?.body).toBeInstanceOf(FormData);
    expect((posted?.[1]?.body as FormData).get("file")).toBeInstanceOf(File);
    // The newly uploaded source appears (pending).
    expect(await screen.findByText("handbook.pdf")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
  });

  it("uploads several files at once, one request each, and lists them all", async () => {
    mock({ sources: [] });
    renderWithRepo(<SourcesPanel />);
    await screen.findByRole("button", { name: "Upload" });

    const files = [
      new File(["a"], "one.pdf", { type: "application/pdf" }),
      new File(["b"], "two.txt", { type: "text/plain" }),
      new File(["c"], "three.png", { type: "image/png" }),
    ];
    await userEvent.upload(screen.getByLabelText("Documents"), files);
    // The button reflects the selection count.
    await userEvent.click(screen.getByRole("button", { name: "Upload 3 files" }));

    // One POST per file.
    const posts = fetchMock.mock.calls.filter(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/sources"),
    );
    expect(posts).toHaveLength(3);
    const names = posts.map((c) => ((c[1]?.body as FormData).get("file") as File).name).sort();
    expect(names).toEqual(["one.pdf", "three.png", "two.txt"]);
    // All three appear in the list.
    expect(await screen.findByText("one.pdf")).toBeInTheDocument();
    expect(screen.getByText("two.txt")).toBeInTheDocument();
    expect(screen.getByText("three.png")).toBeInTheDocument();
  });

  it("deletes a source", async () => {
    mock({ sources: [source({ id: "s-1", title: "policy.pdf" })] });
    renderWithRepo(<SourcesPanel />);
    const row = (await screen.findByText("policy.pdf")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Delete" }));

    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "DELETE");
    expect(String(del?.[0])).toContain("/sources/s-1");
    // Removed from the list.
    expect(screen.queryByText("policy.pdf")).not.toBeInTheDocument();
  });

  it("polls a still-ingesting source until it reaches done", async () => {
    vi.useFakeTimers();
    try {
      // First listing: processing. Every listing after: done.
      mock({
        sources: (call) => [
          source({ id: "s-1", title: "big.pdf", status: call === 0 ? "processing" : "done" }),
        ],
      });
      renderWithRepo(<SourcesPanel />);
      // Flush the mount (list sources).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("Processing")).toBeInTheDocument();

      // The poll fires and the status advances to done.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SOURCE_POLL_MS);
      });
      expect(screen.getByText("Completed")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps polling after a failed poll, so a late failure still surfaces", async () => {
    // Regression: the poll used to reschedule only when the source list changed
    // identity, so one failed request killed it and the final status appeared only
    // on a manual page refresh.
    vi.useFakeTimers();
    try {
      mock({
        sources: (call) => {
          if (call === 0) return [source({ id: "s-1", title: "big.pdf", status: "processing" })];
          if (call === 1) throw new Error("network blip");
          return [
            source({
              id: "s-1",
              title: "big.pdf",
              status: "failed",
              ingest_error: "EmbeddingError: 429 RESOURCE_EXHAUSTED",
            }),
          ];
        },
      });
      renderWithRepo(<SourcesPanel />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("Processing")).toBeInTheDocument();

      // First poll rejects — the row must not get stuck on "Processing".
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SOURCE_POLL_MS);
      });
      // Second poll succeeds and the failure finally shows without a refresh.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SOURCE_POLL_MS);
      });
      expect(screen.getByText("Failed")).toBeInTheDocument();
      expect(screen.getByText(/RESOURCE_EXHAUSTED/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows ingestion progress as a percentage while embedding", async () => {
    mock({
      sources: [
        source({
          id: "s-1",
          title: "big.pdf",
          status: "processing",
          chunks_total: 4042,
          chunks_done: 1900,
        }),
      ],
    });
    renderWithRepo(<SourcesPanel />);
    expect(await screen.findByText("big.pdf")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("value", "47");
    expect(bar).toHaveAttribute("max", "100");
    expect(screen.getByText("47%")).toBeInTheDocument();
    // Percentage only — the raw chunk counts are an implementation detail.
    expect(screen.queryByText(/1900/)).not.toBeInTheDocument();
    expect(screen.queryByText(/4042/)).not.toBeInTheDocument();
  });

  it("omits the progress bar once a source is done", async () => {
    mock({
      sources: [
        source({ id: "s-1", title: "big.pdf", status: "done", chunks_total: 12, chunks_done: 12 }),
      ],
    });
    renderWithRepo(<SourcesPanel />);
    expect(await screen.findByText("big.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("omits the progress bar when the source predates progress tracking", async () => {
    mock({
      sources: [
        source({
          id: "s-1",
          title: "old.pdf",
          status: "processing",
          chunks_total: null,
          chunks_done: null,
        }),
      ],
    });
    renderWithRepo(<SourcesPanel />);
    expect(await screen.findByText("old.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(screen.getByText("Processing")).toBeInTheDocument();
  });

  it("shows the ingestion error for a failed source", async () => {
    mock({
      sources: [source({ id: "s-1", title: "broken.pdf", status: "failed", ingest_error: "boom" })],
    });
    renderWithRepo(<SourcesPanel />);
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  it("renders the OCR helper note", async () => {
    mock({ sources: [] });
    renderWithRepo(<SourcesPanel />);
    expect(
      await screen.findByText(/only text visible in the image is captured/i),
    ).toBeInTheDocument();
  });

  it("accepts HEIC/HEIF images in the file picker", async () => {
    mock({ sources: [] });
    renderWithRepo(<SourcesPanel />);
    const input = (await screen.findByLabelText(/document/i)) as HTMLInputElement;
    expect(input.accept).toContain(".heic");
    expect(input.accept).toContain(".heif");
  });

  it("submits a web link and appends the created source", async () => {
    mock({
      sources: [],
      webAdded: source({
        id: "w-1",
        kind: "web",
        title: "https://x.test",
        original_filename: null,
        source_url: "https://x.test",
        status: "pending",
      }),
    });
    renderWithRepo(<SourcesPanel />);
    await screen.findByRole("button", { name: "Add link" });

    await userEvent.type(screen.getByLabelText("Web link"), "https://x.test");
    await userEvent.click(screen.getByRole("button", { name: "Add link" }));

    const posted = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/web-sources"),
    );
    expect(String(posted?.[0])).toContain("/repositories/r-1/web-sources");
    expect(JSON.parse(String(posted?.[1]?.body))).toEqual({ url: "https://x.test" });

    // The newly added web source appears, linked to its URL.
    const link = await screen.findByRole("link", { name: "https://x.test" });
    expect(link).toHaveAttribute("href", "https://x.test");
  });

  it("shows the no-repos message when there is no current repository", async () => {
    renderWithRepo(<SourcesPanel />, repoValue({ repos: [], currentRepoId: "" }));
    expect(
      await screen.findByText("No repositories yet. Create one under Repositories first."),
    ).toBeInTheDocument();
  });
});
