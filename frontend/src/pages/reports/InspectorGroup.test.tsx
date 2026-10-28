import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { InspectorGroup } from "./InspectorGroup";

describe("InspectorGroup", () => {
  beforeEach(() => localStorage.clear());

  it("opens by default only when asked to", () => {
    render(
      <>
        <InspectorGroup id="a" title="Alpha" defaultOpen>
          <p>alpha body</p>
        </InspectorGroup>
        <InspectorGroup id="b" title="Beta">
          <p>beta body</p>
        </InspectorGroup>
      </>,
    );

    expect(screen.getByTestId("group-a")).toHaveAttribute("open");
    expect(screen.getByTestId("group-b")).not.toHaveAttribute("open");
  });

  it("remembers that a group was closed, across a remount", async () => {
    const { unmount } = render(
      <InspectorGroup id="typo" title="Typography" defaultOpen>
        <p>body</p>
      </InspectorGroup>,
    );

    await userEvent.click(screen.getByText("Typography"));
    unmount();

    render(
      <InspectorGroup id="typo" title="Typography" defaultOpen>
        <p>body</p>
      </InspectorGroup>,
    );
    expect(screen.getByTestId("group-typo")).not.toHaveAttribute("open");
  });

  it("keeps each group's state under its own id", async () => {
    const both = (
      <>
        <InspectorGroup id="one" title="One" defaultOpen>
          <p>1</p>
        </InspectorGroup>
        <InspectorGroup id="two" title="Two" defaultOpen>
          <p>2</p>
        </InspectorGroup>
      </>
    );
    const { unmount } = render(both);

    await userEvent.click(screen.getByText("One"));
    expect(screen.getByTestId("group-one")).not.toHaveAttribute("open");
    expect(screen.getByTestId("group-two")).toHaveAttribute("open");

    // The remount is what makes this test able to fail. `useState`'s initializer
    // runs once, so within a single mount a shared storage key is invisible —
    // closing "one" never makes "two" re-read it. Only on the way back in does a
    // group ask storage what it should be, and only then does one key for all
    // groups drag the untouched one closed with it.
    unmount();
    render(both);

    expect(screen.getByTestId("group-one")).not.toHaveAttribute("open");
    expect(screen.getByTestId("group-two")).toHaveAttribute("open");
  });

  it("opens on a reveal, even when the reader had closed it", async () => {
    // Selecting a block must put its properties on screen without a second
    // click (#194). The group is a `<details>` whose `open` is deliberately not
    // React-managed, so revealing it is imperative — the prop only says *when*.
    function Harness({ reveal }: { reveal: number | null }) {
      return (
        <InspectorGroup id="block" title="Block" defaultOpen revealKey={reveal}>
          <p>body</p>
        </InspectorGroup>
      );
    }
    const { rerender } = render(<Harness reveal={null} />);

    await userEvent.click(screen.getByText("Block"));
    expect(screen.getByTestId("group-block")).not.toHaveAttribute("open");

    rerender(<Harness reveal={2} />);
    expect(screen.getByTestId("group-block")).toHaveAttribute("open");
  });

  // NOTE: whether a reveal is *remembered* is deliberately not tested here.
  // Setting `.open` fires `toggle` in a browser and, as far as this suite could
  // detect, not in jsdom — so a unit test of it passes with the suppression
  // deleted and proves nothing. That property is owned by
  // *choosing a block opens the group the reader collapsed, without adopting it*
  // in `e2e/report-design.spec.ts`, which reloads rather than reading storage.

  it("keeps its children mounted when closed, so a control is never lost", () => {
    render(
      <InspectorGroup id="c" title="Closed">
        <button type="button">inside</button>
      </InspectorGroup>,
    );

    // A native <details> hides its body without unmounting it. That is what lets
    // the page hold controls in collapsed groups without their state resetting.
    expect(screen.getByRole("button", { name: "inside" })).toBeInTheDocument();
  });
});
