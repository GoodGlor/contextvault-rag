import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BlockInspector } from "./BlockInspector";
import type { BlockList } from "./useBlockList";
import type { ReportBlock, ReportDesign } from "../../api/reports";

const DESIGN: ReportDesign = {
  template: "kvarta",
  blocks_version: 1,
  blocks: [],
  stored: true,
  sections: [
    {
      index: 0,
      title: "Виторг",
      kind: "data",
      allowed_chart_types: ["bar", "line"],
      draws_chart: true,
      prose: null,
      sources: "[1] SEC",
    },
    {
      index: 1,
      title: "Висновки",
      kind: "text",
      allowed_chart_types: [],
      draws_chart: false,
      prose: "Породжені слова",
      sources: null,
    },
  ],
  title: "Звіт про виторг",
  typography: {},
  page: {},
  brand: {},
};

function list(
  blocks: ReportBlock[],
  selected: number | null,
  over: Partial<BlockList> = {},
): BlockList {
  return {
    blocks,
    selected,
    select: vi.fn(),
    deleted: null,
    addable: [],
    add: vi.fn(),
    replace: vi.fn(),
    duplicate: vi.fn(),
    resetText: vi.fn(),
    toggleHidden: vi.fn(),
    remove: vi.fn(),
    undo: vi.fn(),
    move: vi.fn(),
    ...over,
  };
}

describe("BlockInspector", () => {
  it("asks for a selection when there is none", () => {
    render(<BlockInspector list={list([{ kind: "cover" }], null)} design={DESIGN} />);

    expect(screen.getByText(/Select a block/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("does not keep a second copy of the words — the sheet is where they are typed", () => {
    // 6c retired the textarea. Six assertions lived on it, and each was either
    // re-homed or recorded as a deliberate loss before it was deleted (6a's rule
    // when `ReportBlockEditor` went):
    //
    //   • a keystroke publishes an override → ReportCanvas, "reads a typed edit
    //     back as that block's text override"
    //   • an emptied field stays an override → ReportCanvas, "an emptied run is
    //     still an override"
    //   • an existing override shows instead of the generated words →
    //     tests/test_report_html.py::test_a_heading_override_replaces_the_
    //     generated_section_title, where the document itself is the evidence
    //   • a cover seeds from the title, a sources line from the joined citations
    //     → the renderer's own tests, same file
    //   • the box follows the selection → **a deliberate loss.** It guarded a
    //     defect only a second copy can have: a box that keeps the previous
    //     block's words. There is no box, so there is nothing to go stale.
    render(<BlockInspector list={list([{ kind: "prose", section: 1 }], 0)} design={DESIGN} />);

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/on the sheet/i)).toBeInTheDocument();
  });

  it("offers a reset only once an override exists", async () => {
    const resetText = vi.fn();
    const { unmount } = render(
      <BlockInspector list={list([{ kind: "heading", section: 0 }], 0)} design={DESIGN} />,
    );
    expect(
      screen.queryByRole("button", { name: /Restore the generated/i }),
    ).not.toBeInTheDocument();
    unmount();

    render(
      <BlockInspector
        list={list([{ kind: "heading", section: 0, text: "Мій" }], 0, { resetText })}
        design={DESIGN}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Restore the generated/i }));

    expect(resetText).toHaveBeenCalledWith(0);
  });

  it("a callout has no generated original, so it is never offered a reset", () => {
    render(<BlockInspector list={list([{ kind: "callout", text: "Слова" }], 0)} design={DESIGN} />);

    // Its words live on the sheet like every other editable kind; what is
    // asserted here is the *absence* of the way back.
    expect(
      screen.queryByRole("button", { name: /Restore the generated/i }),
    ).not.toBeInTheDocument();
  });

  it("says so rather than showing an empty box for a computed block", () => {
    render(<BlockInspector list={list([{ kind: "stats", section: 0 }], 0)} design={DESIGN} />);

    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText(/produced when the report is built/i)).toBeInTheDocument();
  });

  it("offers only the chart types the backend permits, and never a pie", () => {
    render(
      <BlockInspector
        list={list([{ kind: "viz", section: 0, variant: "chart" }], 0)}
        design={DESIGN}
      />,
    );

    const group = screen.getByRole("group", { name: "Chart type" });
    const names = Array.from(group.querySelectorAll("button")).map((b) => b.textContent);
    expect(names).toEqual(["Bar", "Line"]);
  });

  it("says a result cannot be charted rather than offering an empty row", () => {
    render(
      <BlockInspector
        list={list([{ kind: "viz", section: 1, variant: "chart" }], 0)}
        design={DESIGN}
      />,
    );

    expect(screen.getByText(/cannot be drawn as a chart/i)).toBeInTheDocument();
  });

  it("hides and moves through the list", async () => {
    const toggleHidden = vi.fn();
    const move = vi.fn();
    render(
      <BlockInspector
        list={list([{ kind: "cover" }, { kind: "heading", section: 0 }], 1, {
          toggleHidden,
          move,
        })}
        design={DESIGN}
      />,
    );

    await userEvent.click(screen.getByTestId("block-hide"));
    expect(toggleHidden).toHaveBeenCalledWith(1);

    await userEvent.click(screen.getByTestId("block-up"));
    expect(move).toHaveBeenCalledWith(1, 0);
  });

  it("cannot move the first block up or the last block down", () => {
    render(<BlockInspector list={list([{ kind: "cover" }, { kind: "toc" }], 0)} design={DESIGN} />);

    expect(screen.getByTestId("block-up")).toBeDisabled();
    expect(screen.getByTestId("block-down")).toBeEnabled();
  });
});

describe("BlockInspector — marks and actions (6d)", () => {
  it("offers the closed set of marks on a heading, with 'no mark' first", () => {
    render(<BlockInspector list={list([{ kind: "heading", section: 0 }], 0)} design={DESIGN} />);

    const grid = screen.getByRole("group", { name: /mark/i });
    const marks = within(grid).getAllByRole("button");
    // Twelve: the mockup's eleven glyphs and the way back to none.
    expect(marks).toHaveLength(12);
    expect(marks[0]).toHaveAttribute("aria-pressed", "true");
  });

  it("shows which mark the block already wears", () => {
    render(
      <BlockInspector
        list={list([{ kind: "heading", section: 0, icon: "db" }], 0)}
        design={DESIGN}
      />,
    );

    const pressed = within(screen.getByRole("group", { name: /mark/i })).getAllByRole("button", {
      pressed: true,
    });
    expect(pressed).toHaveLength(1);
    expect(pressed[0]).toHaveAccessibleName(/db/i);
  });

  it("publishes a chosen mark as a patch on that block", async () => {
    const replace = vi.fn();
    render(
      <BlockInspector
        list={list([{ kind: "heading", section: 0 }], 0, { replace })}
        design={DESIGN}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /warn/i }));

    expect(replace).toHaveBeenCalledWith(0, { icon: "warn" });
  });

  it("offers no mark for a kind the write gate would refuse one on", () => {
    // The same rule stated in the UI as in `validate_blocks`, so a 422 is never
    // the reader's first sight of it — the pattern 5d set for the chart chips.
    render(
      <BlockInspector
        list={list([{ kind: "viz", section: 0, variant: "table" }], 0)}
        design={DESIGN}
      />,
    );

    expect(screen.queryByRole("group", { name: /mark/i })).not.toBeInTheDocument();
  });

  it("duplicates the selected block", async () => {
    const duplicate = vi.fn();
    render(
      <BlockInspector
        list={list([{ kind: "heading", section: 0 }], 0, { duplicate })}
        design={DESIGN}
      />,
    );

    await userEvent.click(screen.getByTestId("block-duplicate"));

    expect(duplicate).toHaveBeenCalledWith(0);
  });
});
