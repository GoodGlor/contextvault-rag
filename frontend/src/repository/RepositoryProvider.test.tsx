import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { RepositoryProvider } from "./RepositoryProvider";
import { useCurrentRepository } from "./RepositoryContext";

// Auth is faked per-test so we can flip role → which list endpoint is used.
const roleRef = { current: "member" as "member" | "admin" };
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({ session: { role: roleRef.current, username: "u", userId: "1" } }),
}));

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Routes fetch by URL so admin tests can give different bodies to the
 *  granted-list and all-list endpoints. */
function routedFetchMock(routes: Record<string, unknown>) {
  return vi.fn((url: string) => {
    const body = routes[url];
    return Promise.resolve(json(body ?? []));
  });
}

function Probe() {
  const { repos, currentRepoId, setCurrentRepoId, loading } = useCurrentRepository();
  if (loading) return <p>loading</p>;
  return (
    <div>
      <span data-testid="current">{currentRepoId}</span>
      <span data-testid="count">{repos.length}</span>
      <button onClick={() => setCurrentRepoId("r-2")}>pick2</button>
    </div>
  );
}

/** Test-only helper: fires a client-side navigation so a single render can
 *  observe the provider react to a route/scope change. */
function GoTo({ to }: { to: string }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(to)}>
      goto {to}
    </button>
  );
}

describe("RepositoryProvider", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    roleRef.current = "member";
    localStorage.clear();
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("defaults to the first repo and calls the granted-list endpoint for members", async () => {
    fetchMock.mockResolvedValue(
      json([
        { id: "r-1", name: "A" },
        { id: "r-2", name: "B" },
      ]),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("r-1"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/repositories");
  });

  it("uses the admin all-repos endpoint when the session is admin", async () => {
    roleRef.current = "admin";
    fetchMock.mockImplementation(
      routedFetchMock({
        "/api/repositories": [{ id: "r-1", name: "A" }],
        "/api/admin/repositories": [
          { id: "r-1", name: "A", description: null, configured: true },
          { id: "r-2", name: "B", description: null, configured: false },
        ],
      }),
    );
    render(
      <MemoryRouter initialEntries={["/admin/data"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/repositories");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/admin/repositories");
  });

  it("restores a still-valid stored repo, and persists a new selection", async () => {
    localStorage.setItem("contextvault.currentRepo", "r-2");
    fetchMock.mockResolvedValue(
      json([
        { id: "r-1", name: "A" },
        { id: "r-2", name: "B" },
      ]),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("r-2"));
    await act(() => userEvent.click(screen.getByText("pick2")));
    expect(localStorage.getItem("contextvault.currentRepo")).toBe("r-2");
  });

  it("falls back to the first repo when the stored id is gone", async () => {
    localStorage.setItem("contextvault.currentRepo", "stale");
    fetchMock.mockResolvedValue(json([{ id: "r-1", name: "A" }]));
    render(
      <MemoryRouter initialEntries={["/"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("r-1"));
  });

  it("scopes an admin on a workspace path (/) to the GRANTED list, not the all list", async () => {
    roleRef.current = "admin";
    fetchMock.mockImplementation(
      routedFetchMock({
        "/api/repositories": [{ id: "r-1", name: "A" }],
        "/api/admin/repositories": [
          { id: "r-1", name: "A", description: null, configured: true },
          { id: "r-2", name: "B", description: null, configured: false },
        ],
      }),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(screen.getByTestId("current")).toHaveTextContent("r-1");
  });

  it("scopes an admin on a management path (/admin/data) to the ALL list", async () => {
    roleRef.current = "admin";
    fetchMock.mockImplementation(
      routedFetchMock({
        "/api/repositories": [{ id: "r-1", name: "A" }],
        "/api/admin/repositories": [
          { id: "r-1", name: "A", description: null, configured: true },
          { id: "r-2", name: "B", description: null, configured: false },
        ],
      }),
    );
    render(
      <MemoryRouter initialEntries={["/admin/data"]}>
        <RepositoryProvider>
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
  });

  it("reconciles the selection when leaving a management page for a workspace page drops an ungranted repo", async () => {
    roleRef.current = "admin";
    fetchMock.mockImplementation(
      routedFetchMock({
        "/api/repositories": [{ id: "r-1", name: "A" }],
        "/api/admin/repositories": [
          { id: "r-1", name: "A", description: null, configured: true },
          { id: "r-2", name: "B", description: null, configured: false },
        ],
      }),
    );
    render(
      <MemoryRouter initialEntries={["/admin/data"]}>
        <RepositoryProvider>
          <GoTo to="/" />
          <Probe />
        </RepositoryProvider>
      </MemoryRouter>,
    );
    // On the management page, the all-list is visible; pick the ungranted repo.
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
    await act(() => userEvent.click(screen.getByText("pick2")));
    await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("r-2"));

    // Navigate to a workspace page: the list narrows to granted-only (r-1), and
    // the now-invisible r-2 selection must reconcile back to r-1.
    await act(() => userEvent.click(screen.getByText("goto /")));
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("r-1"));
  });
});
