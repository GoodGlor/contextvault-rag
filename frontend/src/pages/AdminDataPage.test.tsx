import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AdminDataPage } from "./AdminDataPage";
import { RepositoryContext, type RepositoryContextValue } from "../repository/RepositoryContext";

const repoValue: RepositoryContextValue = {
  repos: [{ id: "r-1", name: "Handbook", description: null }],
  currentRepoId: "r-1",
  setCurrentRepoId: vi.fn(),
  loading: false,
  error: null,
};

function renderData(initial = "/admin/data") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <RepositoryContext.Provider value={repoValue}>
        <AdminDataPage />
      </RepositoryContext.Provider>
    </MemoryRouter>,
  );
}

describe("AdminDataPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((url: string) => {
      // The database endpoint reports "no connection yet" (404); everything
      // else (the sources list) reports an empty list — either way, awaiting
      // the settled state below lets each panel's mount-fetch effect resolve
      // before the test ends, so no act(...) warning is printed.
      if (/\/database$/.test(String(url))) {
        return Promise.resolve(
          new Response(JSON.stringify({ detail: "not found" }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    });
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows the Documents tab selected by default", async () => {
    renderData();
    expect(await screen.findByText(/no sources yet/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /documents/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("honors ?tab=database on load", async () => {
    renderData("/admin/data?tab=database");
    expect(await screen.findByLabelText(/host/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /database/i })).toHaveAttribute("aria-selected", "true");
  });

  it("switches tabs on click", async () => {
    renderData();
    await screen.findByText(/no sources yet/i);
    await userEvent.click(screen.getByRole("tab", { name: /database/i }));
    expect(await screen.findByLabelText(/host/i)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /database/i })).toHaveAttribute("aria-selected", "true");
  });
});
