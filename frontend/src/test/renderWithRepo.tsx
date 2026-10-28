import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RepositoryContext, type RepositoryContextValue } from "../repository/RepositoryContext";

export function repoValue(overrides: Partial<RepositoryContextValue> = {}): RepositoryContextValue {
  return {
    repos: [{ id: "r-1", name: "Handbook", description: null }],
    currentRepoId: "r-1",
    setCurrentRepoId: () => {},
    loading: false,
    error: null,
    ...overrides,
  };
}

/** Render a repo-scoped page/panel inside a seeded RepositoryContext + router. */
export function renderWithRepo(ui: ReactElement, value: RepositoryContextValue = repoValue()) {
  return render(
    <MemoryRouter>
      <RepositoryContext.Provider value={value}>{ui}</RepositoryContext.Provider>
    </MemoryRouter>,
  );
}
