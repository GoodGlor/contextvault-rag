import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BlockTree } from "./BlockTree";
import type { BlockList } from "./useBlockList";
import type { ReportDesign } from "../../api/reports";

/** `design.blocks` is the seed the working list starts from, so a fixture where
 *  it is empty while the list has rows describes a state that cannot occur. */
const DESIGN: ReportDesign = {
  template: "kvarta",
  blocks_version: 1,
  blocks: [
    { kind: "cover" },
    { kind: "heading", section: 0 },
    { kind: "prose", section: 1, hidden: true },
  ],
  stored: true,
  sections: [
    {
      index: 0,
      title: "Виторг",
      kind: "data",
      allowed_chart_types: ["bar"],
      draws_chart: true,
      prose: null,
      sources: null,
    },
    {
      index: 1,
      title: "Висновки",
      kind: "text",
      allowed_chart_types: [],
      draws_chart: false,
      prose: "Слова",
      sources: null,
    },
  ],
  title: "Звіт",
  typography: {},
  page: {},
  brand: {},
};

function list(overrides: Partial<BlockList> = {}): BlockList {
  return {
    blocks: [
      { kind: "cover" },
      { kind: "heading", section: 0 },
      { kind: "prose", section: 1, hidden: true },
    ],
    selected: null,
    select: vi.fn(),
    deleted: null,
    addable: ["heading", "prose", "callout"],
    add: vi.fn(),
    replace: vi.fn(),
    duplicate: vi.fn(),
    resetText: vi.fn(),
    toggleHidden: vi.fn(),
    remove: vi.fn(),
    undo: vi.fn(),
    move: vi.fn(),
    ...overrides,
  };
}

describe("BlockTree", () => {
  it("names every block and tags the section it reads", () => {
    render(<BlockTree list={list()} design={DESIGN} />);

    expect(screen.getByTestId("block-node-0")).toHaveTextContent("Cover");
    // A data section and a text section wear different tags — that difference is
    // what tells a reader where a number can come from.
    expect(screen.getByTestId("section-tag-1")).toHaveTextContent("DB");
    expect(screen.getByTestId("section-tag-2")).toHaveTextContent("DOC");
  });

  it("a block bound to no section wears no tag", () => {
    render(<BlockTree list={list()} design={DESIGN} />);

    expect(screen.queryByTestId("section-tag-0")).not.toBeInTheDocument();
  });

  it("marks the selected row and reports a click", async () => {
    const select = vi.fn();
    render(<BlockTree list={list({ selected: 1, select })} design={DESIGN} />);

    expect(screen.getByTestId("block-node-1")).toHaveAttribute("aria-current", "true");
    await userEvent.click(screen.getByTestId("block-node-2"));

    expect(select).toHaveBeenCalledWith(2);
  });

  it("a hidden block keeps its place and reads as hidden", () => {
    render(<BlockTree list={list()} design={DESIGN} />);

    expect(screen.getByTestId("block-row-2")).toHaveClass("muted");
    expect(screen.getAllByTestId(/^block-row-/)).toHaveLength(3);
  });

  it("keeps the palette out of the way until it is asked for", async () => {
    // Nine buttons standing permanently above an empty panel, for something a
    // reader does once or twice per report (#167).
    render(<BlockTree list={list()} design={DESIGN} />);

    expect(screen.queryByRole("group", { name: "Add a block" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("block-add-toggle"));

    expect(screen.getByRole("group", { name: "Add a block" })).toBeInTheDocument();
  });

  it("offers only the kinds the list permits", async () => {
    render(<BlockTree list={list()} design={DESIGN} />);
    await userEvent.click(screen.getByTestId("block-add-toggle"));

    const palette = screen.getByRole("group", { name: "Add a block" });
    // The palette's own buttons, not the hint that follows them.
    expect(palette.querySelectorAll("button")).toHaveLength(3);
  });

  it("adds the kind that was clicked", async () => {
    const add = vi.fn();
    render(<BlockTree list={list({ add })} design={DESIGN} />);
    await userEvent.click(screen.getByTestId("block-add-toggle"));

    await userEvent.click(screen.getByRole("button", { name: "+ Callout" }));

    expect(add).toHaveBeenCalledWith("callout");
    // And it closes behind itself: a palette that stayed open would be the
    // permanent grid again, one click later.
    expect(screen.queryByRole("group", { name: "Add a block" })).not.toBeInTheDocument();
  });

  it("says so rather than offering a tree when the report has no stored data", () => {
    // Ported from ReportBlockEditor. `design.blocks` is the seed, not the
    // working list: empty means the row carries no stored section data at all,
    // which is a different fact from a reader having deleted every block.
    render(
      <BlockTree list={list({ blocks: [] })} design={{ ...DESIGN, blocks: [], sections: [] }} />,
    );

    expect(screen.queryByRole("list", { name: "Report blocks" })).not.toBeInTheDocument();
    expect(screen.getByText(/cannot be edited/i)).toBeInTheDocument();
  });

  it("shows the undo offer with the seconds left, and undoes", async () => {
    const undo = vi.fn();
    render(
      <BlockTree
        list={list({ deleted: { block: { kind: "cover" }, index: 0, secondsLeft: 4 }, undo })}
        design={DESIGN}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("4");
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));

    expect(undo).toHaveBeenCalled();
  });

  it("a drop reorders through the list, not through its own arithmetic", () => {
    const move = vi.fn();
    render(<BlockTree list={list({ move })} design={DESIGN} />);

    const data = { getData: () => "0", setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(screen.getByTestId("block-row-0"), { dataTransfer: data });
    fireEvent.dragOver(screen.getByTestId("block-row-2"), { dataTransfer: data });
    fireEvent.drop(screen.getByTestId("block-row-2"), { dataTransfer: data });

    expect(move).toHaveBeenCalledWith(0, 2);
  });

  it("removes through the list", async () => {
    const remove = vi.fn();
    render(<BlockTree list={list({ remove })} design={DESIGN} />);

    await userEvent.click(screen.getByRole("button", { name: /Delete Heading/ }));

    expect(remove).toHaveBeenCalledWith(1);
  });
});
