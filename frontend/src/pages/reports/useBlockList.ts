import { useCallback, useEffect, useRef, useState } from "react";
import { ADDABLE_BLOCK_KINDS } from "../../api/reports";
import type { AddableBlockKind, ReportBlock, ReportDesign } from "../../api/reports";

/** How long a deletion stays reversible. Parent design §7. */
const UNDO_MS = 6000;
/** How often the remaining time is redrawn. Fine enough that the number never
 *  appears to skip, coarse enough not to re-render on every frame. */
const TICK_MS = 200;

/** Kinds that read nothing from a section, so they can be added to a report that
 *  has none. Mirrors `_SECTIONLESS_KINDS` in `services/report_blocks.py`; adding
 *  any other kind without a section is a 422 from the write gate.
 *
 *  A comment saying "mirrors" has already failed to keep these in step once —
 *  `toc` and `image` were added to the backend in 5f and silently filtered out of
 *  the palette here until an e2e run caught it. `useBlockList.test.ts` asserts the
 *  full list rather than trusting this note. */
const SECTIONLESS: ReadonlySet<string> = new Set(["cover", "pagebreak", "callout", "toc", "image"]);

export interface DeletedBlock {
  block: ReportBlock;
  index: number;
  /** Whole seconds left, shown rather than left to guess. */
  secondsLeft: number;
}

export interface BlockList {
  blocks: ReportBlock[];
  selected: number | null;
  select: (index: number | null) => void;
  deleted: DeletedBlock | null;
  addable: AddableBlockKind[];
  add: (kind: AddableBlockKind) => void;
  replace: (index: number, patch: Partial<ReportBlock>) => void;
  duplicate: (index: number) => void;
  resetText: (index: number) => void;
  toggleHidden: (index: number) => void;
  remove: (index: number) => void;
  undo: () => void;
  move: (from: number, to: number) => void;
}

/** Everything the editor knows about the block list, owned in one place.
 *
 *  It lives in a hook rather than in a component because the workbench renders
 *  the tree and the per-block controls in **different columns**, and they have to
 *  agree about what is selected. A component owning that state could only share
 *  it by rendering both, which is exactly the single-column arrangement step 6a
 *  exists to undo.
 *
 *  **It edits structure, never numbers.** A block binds to a section and draws
 *  whatever that section stored (parent design §2), which is why re-running the
 *  report with newer data leaves the design alone. Computed values are never
 *  editable at all (parent design, non-goals).
 *
 *  **The working list is local, seeded once.** The page polls the report row
 *  while a rebuild runs; re-reading `design.blocks` on every render would let a
 *  poll landing mid-drag replace the reader's list with the last-saved one.
 */
export function useBlockList(
  design: ReportDesign,
  onChange: (blocks: ReportBlock[]) => void,
): BlockList {
  const [blocks, setBlocks] = useState<ReportBlock[]>(design.blocks);
  const [selected, setSelected] = useState<number | null>(null);
  const [deleted, setDeleted] = useState<DeletedBlock | null>(null);
  const timers = useRef<{
    tick?: ReturnType<typeof setInterval>;
    end?: ReturnType<typeof setTimeout>;
  }>({});

  const clearTimers = useCallback((): void => {
    if (timers.current.tick !== undefined) clearInterval(timers.current.tick);
    if (timers.current.end !== undefined) clearTimeout(timers.current.end);
    timers.current = {};
  }, []);

  useEffect(() => clearTimers, [clearTimers]);

  /** Apply a change to the working list and publish it in one step, so a caller
   *  can never emit a list that is not also on screen. */
  const commit = useCallback(
    (next: ReportBlock[]): void => {
      setBlocks(next);
      onChange(next);
    },
    [onChange],
  );

  /** Which section a newly added block binds to: the selected block's, else the
   *  first. A kind that needs a section is not offered at all when there is
   *  none, so this never invents an index the write gate would reject. */
  const targetSection = useCallback((): number | undefined => {
    if (design.sections.length === 0) return undefined;
    const current = selected !== null ? blocks[selected] : undefined;
    return current?.section ?? 0;
  }, [blocks, design.sections.length, selected]);

  const add = useCallback(
    (kind: AddableBlockKind): void => {
      const block: ReportBlock = { kind };
      if (!SECTIONLESS.has(kind)) block.section = targetSection();
      // A viz block with no variant is rejected on write, and `table` is the one
      // variant every result supports — a chart may not be drawable at all.
      if (kind === "viz") block.variant = "table";
      // After the selection, so adding twice builds downwards rather than
      // stacking in reverse; at the end when nothing is selected.
      const at = selected !== null ? selected + 1 : blocks.length;
      setSelected(at);
      commit([...blocks.slice(0, at), block, ...blocks.slice(at)]);
    },
    [blocks, commit, selected, targetSection],
  );

  /** Copy the block and put the copy directly after it.
   *
   *  A shallow spread is a real copy here because every field on `ReportBlock`
   *  is a scalar — sharing the object would make a later `replace` on one row
   *  edit both, which reads as the editor forgetting which block you selected.
   *
   *  The copy is **selected**, for `add`'s reason: a change the reader cannot
   *  see is indistinguishable from nothing having happened.
   */
  const duplicate = useCallback(
    (index: number): void => {
      const block = blocks[index];
      if (block === undefined) return;
      setSelected(index + 1);
      commit([...blocks.slice(0, index + 1), { ...block }, ...blocks.slice(index + 1)]);
    },
    [blocks, commit],
  );

  const replace = useCallback(
    (index: number, patch: Partial<ReportBlock>): void => {
      commit(blocks.map((block, i) => (i === index ? { ...block, ...patch } : block)));
    },
    [blocks, commit],
  );

  /** Drop the override, so the block goes back to reading its section. A
   *  deletion rather than writing the generated words back in: those live on the
   *  section, and copying them would freeze today's text into the design. */
  const resetText = useCallback(
    (index: number): void => {
      commit(
        blocks.map((block, i) => {
          if (i !== index) return block;
          const rest = { ...block };
          delete rest.text;
          return rest;
        }),
      );
    },
    [blocks, commit],
  );

  const toggleHidden = useCallback(
    (index: number): void => {
      const now = blocks[index].hidden === true;
      // Absent rather than `false`: the payload records departures from the
      // default, and the backend drops the flag on the way out anyway.
      commit(
        blocks.map((block, i) => {
          if (i !== index) return block;
          const rest = { ...block };
          delete rest.hidden;
          return now ? rest : { ...rest, hidden: true };
        }),
      );
    },
    [blocks, commit],
  );

  const remove = useCallback(
    (index: number): void => {
      clearTimers();
      const gone = blocks[index];
      setSelected(null);
      commit(blocks.filter((_, i) => i !== index));
      // Only the most recent deletion is held: setting this state replaces any
      // window already open, which *is* the "a second delete commits the first"
      // rule rather than a separate branch implementing it.
      setDeleted({ block: gone, index, secondsLeft: UNDO_MS / 1000 });
      const until = Date.now() + UNDO_MS;
      timers.current.tick = setInterval(() => {
        setDeleted((current) =>
          current === null
            ? null
            : { ...current, secondsLeft: Math.max(0, Math.ceil((until - Date.now()) / 1000)) },
        );
      }, TICK_MS);
      timers.current.end = setTimeout(() => {
        clearTimers();
        setDeleted(null);
      }, UNDO_MS);
    },
    [blocks, clearTimers, commit],
  );

  const undo = useCallback((): void => {
    if (deleted === null) return;
    clearTimers();
    const at = Math.min(deleted.index, blocks.length);
    setDeleted(null);
    setSelected(at);
    commit([...blocks.slice(0, at), deleted.block, ...blocks.slice(at)]);
  }, [blocks, clearTimers, commit, deleted]);

  const move = useCallback(
    (from: number, to: number): void => {
      // A drop on the row it started from is not a reorder. Emitting anyway would
      // spend a rebuild — 257 ms at the size this system produces — on a
      // mis-click.
      if (from === to || from < 0 || from >= blocks.length) return;
      const next = [...blocks];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      setSelected(to);
      commit(next);
    },
    [blocks, commit],
  );

  const addable = ADDABLE_BLOCK_KINDS.filter(
    (kind) => SECTIONLESS.has(kind) || design.sections.length > 0,
  );

  return {
    blocks,
    selected,
    select: setSelected,
    deleted,
    addable,
    add,
    replace,
    duplicate,
    resetText,
    toggleHidden,
    remove,
    undo,
    move,
  };
}
