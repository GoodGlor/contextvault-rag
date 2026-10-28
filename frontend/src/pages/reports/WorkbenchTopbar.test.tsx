import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchTopbar } from "./WorkbenchTopbar";
import type { WorkbenchTopbarProps } from "./WorkbenchTopbar";

function setup(over: Partial<WorkbenchTopbarProps> = {}): WorkbenchTopbarProps {
  const props: WorkbenchTopbarProps = {
    repositoryName: "Бухгалтерія",
    reportTitle: "Звіт від 02.08.2026",
    busy: false,
    view: "sheet",
    onView: vi.fn(),
    onOpenGallery: vi.fn(),
    onRerender: vi.fn(),
    backTo: "/reports",
    ...over,
  };
  render(
    <MemoryRouter>
      <WorkbenchTopbar {...props} />
    </MemoryRouter>,
  );
  return props;
}

describe("WorkbenchTopbar", () => {
  it("names the repository and the report", () => {
    setup();

    expect(screen.getByText(/Бухгалтерія/)).toBeInTheDocument();
    expect(screen.getByText("Звіт від 02.08.2026")).toBeInTheDocument();
  });

  it("opens the gallery and rebuilds", async () => {
    const props = setup();

    await userEvent.click(screen.getByRole("button", { name: /template/i }));
    expect(props.onOpenGallery).toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Rebuild" }));
    expect(props.onRerender).toHaveBeenCalled();
  });

  it("refuses a second rebuild while one is running", () => {
    setup({ busy: true });

    expect(screen.getByRole("button", { name: "Rebuild" })).toBeDisabled();
  });

  it("offers a way back to the reports list", () => {
    setup();

    expect(screen.getByRole("link", { name: /back to reports/i })).toHaveAttribute(
      "href",
      "/reports",
    );
  });

  it("switches between the sheet and the artifact of record", async () => {
    // 6a left this out rather than ship a two-position switch with one working
    // position. Both positions work now.
    const props = setup();

    expect(screen.getByRole("button", { name: "Sheet" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "PDF" })).toHaveAttribute("aria-pressed", "false");

    await userEvent.click(screen.getByRole("button", { name: "PDF" }));
    expect(props.onView).toHaveBeenCalledWith("pdf");
  });

  it("shows which view is current, so the switch cannot lie about where you are", () => {
    setup({ view: "pdf" });

    expect(screen.getByRole("button", { name: "PDF" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Sheet" })).toHaveAttribute("aria-pressed", "false");
  });

  it("offers no deck view — a deck is not a paginated document", () => {
    setup();

    expect(screen.queryByRole("button", { name: /pptx|слайд|deck/i })).not.toBeInTheDocument();
  });

  it("does not offer to re-run the report or to save a repository default", () => {
    // «Оновити дані» is an LLM re-run, not a design action, and a quota bucket
    // should not be spendable by mis-click between two harmless buttons.
    // «Зберегти як шаблон» is the repository default, which already has its own
    // control — a second writer of one field is how those two once disagreed.
    setup();

    // Asserted by name, not by the shape of the whole list: the absence of these
    // two is the property, and a list assertion has to be rewritten every time
    // an unrelated control arrives — which is how a test stops meaning what its
    // name says.
    expect(screen.queryByRole("button", { name: /Refresh data|Оновити/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Save as template|Зберегти/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rebuild" })).toBeInTheDocument();
  });
});
