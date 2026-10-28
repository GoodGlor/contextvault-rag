import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ACCENTS, StyleInspector } from "./StyleInspector";

// Queried by `data-testid` throughout, never by visible text: `src/test/setup.ts`
// pins the test locale to English while `uk` is the shipping default, so an
// assertion on Ukrainian copy would fail against a correct component.

function renderInspector(overrides: Partial<Parameters<typeof StyleInspector>[0]> = {}) {
  const props = {
    typography: {},
    page: {},
    brand: {},
    onChange: vi.fn(),
    logoUrl: null,
    onLogoFile: vi.fn(),
    onLogoClear: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  };
  render(<StyleInspector {...props} />);
  return props;
}

describe("StyleInspector", () => {
  it("offers the three pairings plus the template default", () => {
    renderInspector();
    expect(screen.getAllByTestId("pairing-chip")).toHaveLength(4);
  });

  it("offers exactly the five validated accents plus the template default", () => {
    renderInspector();
    const swatches = screen.getAllByTestId("accent-swatch");
    expect(swatches).toHaveLength(ACCENTS.length + 1);
    expect(swatches.slice(1).map((el) => el.getAttribute("data-accent"))).toEqual([...ACCENTS]);
  });

  it("reports a scale change without mutating the group it was given", () => {
    const typography = { scale: 1 };
    const { onChange } = renderInspector({ typography });

    fireEvent.change(screen.getByTestId("scale-range"), { target: { value: "1.2" } });

    expect(onChange).toHaveBeenCalledWith({ typography: { scale: 1.2 } });
    expect(typography.scale).toBe(1);
  });

  it("clears a token rather than sending a default when the template chip is picked", () => {
    // "As template" must mean *absent*, not "the value that happens to match".
    // The backend applies nothing when nothing is set, and that is what keeps
    // every previously stored report rendering unchanged.
    const { onChange } = renderInspector({ typography: { pairing: "serif" } });

    fireEvent.click(screen.getAllByTestId("pairing-chip")[0]);

    expect(onChange).toHaveBeenCalledWith({ typography: { pairing: undefined } });
  });

  it("marks the chosen pairing as pressed and the others as not", () => {
    renderInspector({ typography: { pairing: "mixed" } });
    const pressed = screen
      .getAllByTestId("pairing-chip")
      .filter((el) => el.getAttribute("aria-pressed") === "true");
    expect(pressed).toHaveLength(1);
    expect(pressed[0].getAttribute("data-pairing")).toBe("mixed");
  });

  it("reads the margin slider as a number, not a string", () => {
    const { onChange } = renderInspector();
    fireEvent.change(screen.getByTestId("margin-range"), { target: { value: "24" } });
    expect(onChange).toHaveBeenCalledWith({ page: { margin_mm: 24 } });
  });

  it("keeps the other groups untouched when one changes", () => {
    const { onChange } = renderInspector({
      typography: { scale: 1.1 },
      page: { section_rules: true },
      brand: { approval: true },
    });

    fireEvent.click(screen.getAllByTestId("cover-chip")[1]);

    expect(onChange).toHaveBeenCalledWith({ brand: { approval: true, cover: "band" } });
  });

  it("shows a logo preview and offers to clear it only when one is set", () => {
    const { onLogoClear } = renderInspector({ logoUrl: "/api/repositories/r/report-assets/a" });
    fireEvent.click(screen.getByTestId("logo-clear"));
    expect(onLogoClear).toHaveBeenCalled();
  });

  it("offers no clear control when there is no logo", () => {
    renderInspector();
    expect(screen.queryByTestId("logo-clear")).toBeNull();
  });

  it("accepts only the raster types the deck can place", () => {
    renderInspector();
    expect(screen.getByTestId("logo-file").getAttribute("accept")).toBe(
      "image/png,image/jpeg,image/webp",
    );
  });
});
