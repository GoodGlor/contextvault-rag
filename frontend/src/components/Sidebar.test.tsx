import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { RepositoryContext, type RepositoryContextValue } from "../repository/RepositoryContext";

const roleRef = { current: "member" as "member" | "admin" };
vi.mock("../auth/AuthContext", () => ({
  useAuth: () => ({
    session: { role: roleRef.current, username: "artem", userId: "1" },
    logout: vi.fn(),
  }),
}));

const repoValue: RepositoryContextValue = {
  repos: [{ id: "r-1", name: "Kyiv Support", description: null }],
  currentRepoId: "r-1",
  setCurrentRepoId: vi.fn(),
  loading: false,
  error: null,
};

function renderSidebar() {
  return render(
    <MemoryRouter>
      <RepositoryContext.Provider value={repoValue}>
        <Sidebar />
      </RepositoryContext.Provider>
    </MemoryRouter>,
  );
}

describe("Sidebar", () => {
  beforeEach(() => (roleRef.current = "member"));
  afterEach(() => vi.restoreAllMocks());

  it("shows only workspace links for members", () => {
    renderSidebar();
    expect(screen.getByRole("link", { name: /ask/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /reports/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /data/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /users/i })).not.toBeInTheDocument();
  });

  it("shows manage + admin groups (incl. Data) for admins", () => {
    roleRef.current = "admin";
    renderSidebar();
    expect(screen.getByRole("link", { name: /data/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /providers/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /users/i })).toBeInTheDocument();
    // no separate Sources / Database links anymore
    expect(screen.queryByRole("link", { name: /^sources$/i })).not.toBeInTheDocument();
  });

  it("renders the repository switcher bound to context", () => {
    renderSidebar();
    expect(screen.getByRole("combobox", { name: /repository/i })).toHaveValue("r-1");
  });
});
