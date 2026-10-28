import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { configureApi } from "./client";
import {
  BLOCKS_VERSION,
  getReportDesign,
  getReportPreviewHtml,
  isAwaitingApproval,
  isGenerating,
  listReportTemplates,
  setReportDesign,
  rerenderReport,
} from "./reports";
import type { ReportStatus } from "./reports";

describe("report status predicates", () => {
  it("keeps polling while the backend still owes us something", () => {
    // `planning` is the new one: the LLM is composing the plan, so the row is
    // not terminal and the list must keep polling it.
    (["planning", "pending", "processing"] as ReportStatus[]).forEach((s) =>
      expect(isGenerating(s)).toBe(true),
    );
  });

  it("stops polling once the report is waiting on the user or finished", () => {
    (["awaiting_approval", "done", "failed"] as ReportStatus[]).forEach((s) =>
      expect(isGenerating(s)).toBe(false),
    );
  });

  it("recognises the one status that needs the user to act", () => {
    expect(isAwaitingApproval("awaiting_approval")).toBe(true);
    (["planning", "pending", "processing", "done", "failed"] as ReportStatus[]).forEach((s) =>
      expect(isAwaitingApproval(s)).toBe(false),
    );
  });
});

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("report design api", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("lists the templates from the un-scoped endpoint", async () => {
    fetchMock.mockResolvedValue(
      json([
        { key: "kvarta", name: "Кварта", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
      ]),
    );
    const res = await listReportTemplates();
    expect(res[0].key).toBe("kvarta");
    // Not repository-scoped: the templates are a property of the build, not of a
    // repository, and scoping the path would imply otherwise.
    expect(String(fetchMock.mock.calls[0][0])).toContain("/report-templates");
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("/repositories/");
  });

  it("PUTs the chosen template onto the report row", async () => {
    fetchMock.mockResolvedValue(json({ id: "rep-1", template: "chasopys" }));
    await setReportDesign("r-1", "rep-1", { template: "chasopys" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/repositories/r-1/reports/rep-1/design");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ template: "chasopys" });
  });

  it("sends only the keys the patch carries, so a template change leaves blocks alone", async () => {
    // The endpoint is a *partial* update. Sending `blocks: undefined` would be
    // serialised away by JSON.stringify and work by luck; sending `blocks: null`
    // would clear the reader's whole block list on every template click.
    fetchMock.mockResolvedValue(json({ id: "rep-1" }));
    await setReportDesign("r-1", "rep-1", { template: "chasopys" });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({ template: "chasopys" });
  });

  it("sends blocks with the version they were built against, never an assumed one", async () => {
    fetchMock.mockResolvedValue(json({ id: "rep-1" }));
    const blocks = [{ kind: "cover" }];
    await setReportDesign("r-1", "rep-1", { blocks });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      blocks,
      blocks_version: BLOCKS_VERSION,
    });
  });

  it("clears the block list with an explicit null rather than by omitting it", async () => {
    // Omitting is "leave it alone" and null is "back to the derived default";
    // the reader who has edited a report has no other way to undo that wholesale.
    fetchMock.mockResolvedValue(json({ id: "rep-1" }));
    await setReportDesign("r-1", "rep-1", { blocks: null });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      blocks: null,
      blocks_version: BLOCKS_VERSION,
    });
  });

  it("GETs the design a report renders from", async () => {
    fetchMock.mockResolvedValue(
      json({ template: null, blocks_version: 1, blocks: [], stored: false, sections: [] }),
    );
    await getReportDesign("r-1", "rep-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/repositories/r-1/reports/rep-1/design");
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("POSTs the rebuild with no body", async () => {
    fetchMock.mockResolvedValue(json({ id: "rep-1", template: "chasopys" }));
    await rerenderReport("r-1", "rep-1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/repositories/r-1/reports/rep-1/rerender");
    expect(init.method).toBe("POST");
    // A rerender takes no arguments on purpose: it rebuilds from what is already
    // stored. A body here would suggest it accepts a design, which it does not.
    expect(init.body).toBeUndefined();
  });
});

describe("the design canvas's document", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    configureApi({ getToken: () => "tok-123" });
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
    configureApi({ getToken: () => null });
  });

  function html(body: string, status = 200): Response {
    return new Response(body, { status, headers: { "Content-Type": "text/html" } });
  }

  it("returns the document as a string rather than parsing it as JSON", async () => {
    fetchMock.mockResolvedValue(html('<!doctype html><div class="cv-sheet">…</div>'));

    const doc = await getReportPreviewHtml("r-1", "rep-1");

    expect(doc).toContain("cv-sheet");
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "/api/repositories/r-1/reports/rep-1/design/preview",
    );
  });

  it("carries the session's bearer token", async () => {
    // The *document* is a customer's data, unlike the font faces it references.
    fetchMock.mockResolvedValue(html("<html></html>"));

    await getReportPreviewHtml("r-1", "rep-1");

    const headers = new Headers(fetchMock.mock.calls[0][1].headers);
    expect(headers.get("Authorization")).toBe("Bearer tok-123");
  });

  it("turns a refusal into an ApiError carrying the backend's words", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: "This report has no stored section data." }), {
        status: 409,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(getReportPreviewHtml("r-1", "rep-1")).rejects.toThrow(
      "This report has no stored section data.",
    );
  });
});
