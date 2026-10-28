import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Layout } from "./Layout";

/** The shell's one piece of behaviour: it steps out of the way on the route
 *  that needs the room.
 *
 *  The measurement behind it: the workbench is four columns, not
 *  three — 244 + 272 + 320 = 836 px of chrome — so at 1440 px the canvas gets
 *  564 px for a sheet that is 794 px wide. The sidebar is the only one of the
 *  four that is not doing anything while somebody lays out a page.
 */

vi.mock("./Sidebar", () => ({ Sidebar: () => <nav data-testid="sidebar" /> }));
vi.mock("../repository/RepositoryContext", () => ({
  useCurrentRepository: () => ({ currentRepoId: "r1", repos: [] }),
}));

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/reports" element={<p>reports</p>} />
          <Route path="/reports/:reportId/design" element={<p>workbench</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout", () => {
  it("keeps the full sidebar on an ordinary page", () => {
    const { container } = renderAt("/reports");

    expect(container.querySelector(".app-shell")).not.toHaveAttribute("data-focus", "true");
    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
  });

  it("marks the shell for focus on the design workbench", () => {
    const { container } = renderAt("/reports/abc-123/design");

    expect(container.querySelector(".app-shell")).toHaveAttribute("data-focus", "true");
  });

  it("still renders the navigation in focus mode rather than removing it", () => {
    // A rail, not a disappearance. Hiding navigation outright would leave the
    // editor with one way out — the back link — and no way to reach anything
    // else without using it.
    renderAt("/reports/abc-123/design");

    expect(screen.getByTestId("sidebar")).toBeInTheDocument();
  });

  it("matches the route rather than looking for a word in the path", () => {
    // `/reports/design-notes` contains "design" and is not the workbench. A
    // substring test would collapse the sidebar on it; `matchPath` does not.
    const { container } = render(
      <MemoryRouter initialEntries={["/reports/design-notes"]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/reports/:reportId" element={<p>a report</p>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );

    expect(container.querySelector(".app-shell")).not.toHaveAttribute("data-focus", "true");
  });
});
