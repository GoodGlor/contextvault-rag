import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBlockList } from "./useBlockList";
import type { ReportDesign } from "../../api/reports";

function design(overrides: Partial<ReportDesign> = {}): ReportDesign {
  return {
    template: "kvarta",
    blocks_version: 1,
    blocks: [
      { kind: "cover" },
      { kind: "heading", section: 0 },
      { kind: "viz", section: 0, variant: "table" },
    ],
    stored: true,
    sections: [
      {
        index: 0,
        title: "Виторг",
        kind: "data",
        allowed_chart_types: ["bar", "line"],
        draws_chart: true,
        prose: null,
        sources: null,
      },
    ],
    title: "Звіт",
    typography: {},
    page: {},
    brand: {},
    ...overrides,
  };
}

describe("useBlockList", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("publishes the whole list on every change", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.toggleHidden(1));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0][1]).toEqual({ kind: "heading", section: 0, hidden: true });
    expect(result.current.blocks[1].hidden).toBe(true);
  });

  it("hiding twice removes the flag rather than writing false", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.toggleHidden(1));
    act(() => result.current.toggleHidden(1));

    expect(result.current.blocks[1]).toEqual({ kind: "heading", section: 0 });
    expect("hidden" in result.current.blocks[1]).toBe(false);
  });

  it("adds after the selection, so adding twice builds downwards", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.select(0));
    act(() => result.current.add("callout"));
    act(() => result.current.add("pagebreak"));

    expect(result.current.blocks.map((b) => b.kind)).toEqual([
      "cover",
      "callout",
      "pagebreak",
      "heading",
      "viz",
    ]);
  });

  it("gives a new viz block the one variant every result supports", () => {
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.add("viz"));

    expect(result.current.blocks.at(-1)).toEqual({ kind: "viz", section: 0, variant: "table" });
  });

  it("offers only sectionless kinds when the report has no sections", () => {
    const { result } = renderHook(() =>
      useBlockList(design({ sections: [], blocks: [{ kind: "cover" }] }), vi.fn()),
    );

    // In `ADDABLE_BLOCK_KINDS` order, and `cover` is absent from that list
    // entirely — the palette cannot re-add a deleted cover, which is a known 5d
    // gap and not something this hook decides.
    expect(result.current.addable).toEqual(["callout", "pagebreak", "toc", "image"]);
  });

  it("a move onto its own row is not a reorder and spends no rebuild", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.move(1, 1));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("undo restores the block at its index and cancels the window", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.remove(1));
    expect(result.current.blocks.map((b) => b.kind)).toEqual(["cover", "viz"]);
    expect(result.current.deleted?.secondsLeft).toBe(6);

    act(() => result.current.undo());

    expect(result.current.blocks.map((b) => b.kind)).toEqual(["cover", "heading", "viz"]);
    expect(result.current.deleted).toBeNull();
  });

  it("the undo window closes on its own after six seconds", () => {
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.remove(1));
    act(() => vi.advanceTimersByTime(6000));

    expect(result.current.deleted).toBeNull();
    expect(result.current.blocks.map((b) => b.kind)).toEqual(["cover", "viz"]);
  });

  it("a second delete commits the first", () => {
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.remove(1));
    act(() => result.current.remove(1));

    expect(result.current.deleted?.block.kind).toBe("viz");
    expect(result.current.blocks.map((b) => b.kind)).toEqual(["cover"]);
  });

  it("gives the second deletion its own full six seconds", () => {
    // Ported from ReportBlockEditor's suite, where it was written because a
    // mutation failed to bite: deleting `clearTimers()` from `remove()` left
    // every other test green. Without it the first deletion's timeout survives
    // and fires on its own schedule — six seconds after the *first* delete,
    // which is five after the second — closing the second window early and
    // silently. Only a test that waits past that moment can see it.
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.remove(2));
    act(() => vi.advanceTimersByTime(1000));
    act(() => result.current.remove(1));

    // 5.5 s after the second delete: past the first window's deadline, still
    // inside the second's.
    act(() => vi.advanceTimersByTime(5500));
    expect(result.current.deleted).not.toBeNull();

    act(() => vi.advanceTimersByTime(600));
    expect(result.current.deleted).toBeNull();
  });

  it("resetText deletes the override rather than writing today's words back", () => {
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.replace(1, { text: "Мій заголовок" }));
    expect(result.current.blocks[1].text).toBe("Мій заголовок");

    act(() => result.current.resetText(1));
    expect("text" in result.current.blocks[1]).toBe(false);
  });
});

describe("useBlockList — duplicate (6d)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("puts the copy directly after the original, overrides and mark included", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() =>
      useBlockList(
        design({
          blocks: [
            { kind: "cover" },
            { kind: "heading", section: 0, text: "Мій", icon: "db" },
            { kind: "viz", section: 0, variant: "table" },
          ],
        }),
        onChange,
      ),
    );

    act(() => result.current.duplicate(1));

    // Directly after, not at the end: a copy that lands three blocks away reads
    // as a different action from the one the reader asked for.
    expect(result.current.blocks.map((b) => b.kind)).toEqual([
      "cover",
      "heading",
      "heading",
      "viz",
    ]);
    expect(result.current.blocks[2]).toEqual({
      kind: "heading",
      section: 0,
      text: "Мій",
      icon: "db",
    });
  });

  it("copies rather than shares, so editing one does not edit both", () => {
    const { result } = renderHook(() =>
      useBlockList(design({ blocks: [{ kind: "heading", section: 0, text: "Мій" }] }), vi.fn()),
    );

    act(() => result.current.duplicate(0));
    act(() => result.current.replace(1, { text: "Інший" }));

    expect(result.current.blocks[0]?.text).toBe("Мій");
    expect(result.current.blocks[1]?.text).toBe("Інший");
  });

  it("selects the copy, because an invisible duplicate looks like nothing happened", () => {
    const { result } = renderHook(() => useBlockList(design(), vi.fn()));

    act(() => result.current.duplicate(1));

    expect(result.current.selected).toBe(2);
  });

  it("publishes the new list, so the copy reaches the document", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.duplicate(0));

    expect(onChange).toHaveBeenCalledWith(result.current.blocks);
    expect(onChange.mock.calls[0]?.[0]).toHaveLength(4);
  });

  it("does nothing for an index that is not in the list", () => {
    const onChange = vi.fn();
    const { result } = renderHook(() => useBlockList(design(), onChange));

    act(() => result.current.duplicate(9));

    expect(result.current.blocks).toHaveLength(3);
    expect(onChange).not.toHaveBeenCalled();
  });
});
