import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { HowToStrip } from "./HowToStrip";

describe("HowToStrip", () => {
  it("names the three things the workbench can actually do", () => {
    render(<HowToStrip />);

    const steps = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(steps).toHaveLength(3);
    // Every step must describe something that exists as of this card. Two of
    // them named unbuilt features in 6a, which is why the strip waited.
    expect(steps.join(" ")).toMatch(/sheet/i);
    expect(steps.join(" ")).toMatch(/PDF/);
  });

  it("stays dismissed across a remount rather than only for this render", async () => {
    // A remount, not a rerender: `useState`'s initializer runs once per mount,
    // so a version that only kept the flag in state would pass without one.
    const { unmount } = render(<HowToStrip />);

    await userEvent.click(screen.getByRole("button"));
    expect(screen.queryByTestId("howto")).not.toBeInTheDocument();

    unmount();
    render(<HowToStrip />);

    expect(screen.queryByTestId("howto")).not.toBeInTheDocument();
  });

  it("shows again when nothing was ever dismissed", () => {
    render(<HowToStrip />);

    expect(screen.getByTestId("howto")).toBeInTheDocument();
  });
});
