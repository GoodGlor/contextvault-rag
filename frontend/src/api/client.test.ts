import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, apiFetch, configureApi } from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiFetch", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    configureApi({ getToken: () => null, onUnauthorized: null });
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("prefixes the path with /api and returns parsed JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    const result = await apiFetch<{ ok: boolean }>("/health");
    expect(result).toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith("/api/health", expect.anything());
  });

  it("attaches the bearer token from the configured provider", async () => {
    configureApi({ getToken: () => "tok-abc" });
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/repositories");
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer tok-abc");
  });

  it("throws an ApiError carrying the backend detail on non-2xx", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Invalid username or password" }, 401));
    await expect(apiFetch("/auth/login")).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
      detail: "Invalid username or password",
    });
  });

  it("fires onUnauthorized exactly once on a 401", async () => {
    const onUnauthorized = vi.fn();
    configureApi({ onUnauthorized });
    fetchMock.mockResolvedValue(jsonResponse({ detail: "nope" }, 401));
    await expect(apiFetch("/x")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("flattens FastAPI validation error lists into one message", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: [{ msg: "field required" }, { msg: "too short" }] }, 422),
    );
    await expect(apiFetch("/x")).rejects.toMatchObject({
      detail: "field required; too short",
    });
  });
});

describe("api.getBlob", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    configureApi({ getToken: () => null, onUnauthorized: null });
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("prefixes the path with /api, attaches the bearer token, and resolves a Blob", async () => {
    configureApi({ getToken: () => "tok-abc" });
    const body = new Blob(["%PDF-1.4"], { type: "application/pdf" });
    fetchMock.mockResolvedValue(
      new Response(body, { status: 200, headers: { "Content-Type": "application/pdf" } }),
    );

    const blob = await api.getBlob("/repositories/r-1/reports/rep-1/download");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/repositories/r-1/reports/rep-1/download",
      expect.anything(),
    );
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer tok-abc");
    // Assert on `.type`/`.size` rather than `toBeInstanceOf(Blob)`: under CI's fetch
    // impl the response body's Blob comes from a different realm than the test's
    // global Blob, so an identity check is flaky (passes locally, fails in CI). The
    // shape properties are present on every Blob impl and are what callers rely on.
    expect(blob.type).toBe("application/pdf");
    expect(blob.size).toBeGreaterThan(0);
  });

  it("throws an ApiError carrying the backend detail on non-2xx, without calling .blob() on JSON", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Report PDF not available" }, 404));
    await expect(api.getBlob("/repositories/r-1/reports/rep-1/download")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Report PDF not available",
    });
  });

  it("fires onUnauthorized on a 401", async () => {
    const onUnauthorized = vi.fn();
    configureApi({ onUnauthorized });
    fetchMock.mockResolvedValue(jsonResponse({ detail: "nope" }, 401));
    await expect(api.getBlob("/x")).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
