import { useRef, useState } from "react";
import type { DragEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { blockIcon } from "./blockIcons";
import type { BlockList } from "./useBlockList";
import type { ReportBlock, ReportDesign, ReportSectionSummary } from "../../api/reports";

export interface BlockTreeProps {
  list: BlockList;
  design: ReportDesign;
}

/** The workbench's left panel: what the report is made of, top to bottom.
 *
 *  **Presentational over `BlockList`.** It holds no block state of its own, so
 *  the inspector in the other column cannot disagree with it about what is
 *  selected — which is the entire reason that state was lifted into a hook.
 *
 *  The section tag is the one piece of information here that is not in the
 *  label: it names where a section's numbers came from, `database` or
 *  `documents`. That distinction decides what a reader may conclude from a
 *  figure, so it is worth a mark of its own.
 */
export function BlockTree({ list, design }: BlockTreeProps): ReactNode {
  const { t } = useTranslation();
  const dragging = useRef<number | null>(null);
  const [adding, setAdding] = useState(false);

  function sectionOf(block: ReportBlock): ReportSectionSummary | null {
    if (block.section === undefined) return null;
    return design.sections[block.section] ?? null;
  }

  function label(block: ReportBlock): string {
    const kind = t(`reports.blocks.kinds.${block.kind}`, block.kind);
    const variant =
      block.variant !== undefined
        ? ` · ${t(`reports.blocks.variants.${block.variant}`, block.variant)}`
        : "";
    const section = sectionOf(block);
    return section !== null ? `${kind}${variant} — ${section.title}` : `${kind}${variant}`;
  }

  // --- reorder ---------------------------------------------------------------

  function onDragStart(index: number, event: DragEvent<HTMLLIElement>): void {
    dragging.current = index;
    // Required for a drop to be permitted at all in a real browser; jsdom does
    // not enforce it, which is precisely why it must not be omitted here.
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(index));
  }

  function onDragOver(event: DragEvent<HTMLLIElement>): void {
    // Without preventDefault the element is not a drop target and `drop` never
    // fires — the single most common reason an HTML5 drag silently does nothing.
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  }

  function onDrop(to: number, event: DragEvent<HTMLLIElement>): void {
    event.preventDefault();
    const raw = event.dataTransfer.getData("text/plain");
    const from = raw !== "" ? Number(raw) : (dragging.current ?? -1);
    dragging.current = null;
    // The arithmetic lives in the hook, so the tree and any other surface that
    // reorders cannot drift apart about what a move means.
    list.move(from, to);
  }

  // `design.blocks` is the seed, not the working list. Empty means the row
  // carries no stored section data at all — a different fact from a reader
  // having deleted every block, and one no palette can fix.
  if (design.blocks.length === 0) {
    return (
      <div className="wb-tree-panel">
        <h2>{t("reports.workbench.blocksHeading")}</h2>
        <p className="wb-hint">{t("reports.blocks.notEditable")}</p>
      </div>
    );
  }

  return (
    <div className="wb-tree-panel">
      {/* Header / scroll / footer, and that is the fix for a heading that was
          being clipped: the whole column used to be one scroll container, so
          the title scrolled away with the list under it. */}
      <div className="wb-tree-head">
        <h2>{t("reports.workbench.blocksHeading")}</h2>
        <span className="wb-tree-count">{list.blocks.length}</span>
      </div>

      <ul className="wb-tree" aria-label={t("reports.blocks.treeLabel")}>
        {list.blocks.map((block, index) => {
          const section = sectionOf(block);
          return (
            <li
              // Index-keyed deliberately: a block carries no identity of its own —
              // two `stats` blocks on the same section are indistinguishable — and
              // position is exactly what this list is about.
              key={index}
              draggable
              data-testid={`block-row-${index}`}
              className={block.hidden === true ? "wb-node-row muted" : "wb-node-row"}
              onDragStart={(event) => onDragStart(index, event)}
              onDragOver={onDragOver}
              onDrop={(event) => onDrop(index, event)}
            >
              <button
                type="button"
                className="wb-node"
                data-testid={`block-node-${index}`}
                aria-current={list.selected === index}
                onClick={() => list.select(index)}
              >
                <span className="wb-ico">{blockIcon(block.kind)}</span>
                <span className="wb-label">{label(block)}</span>
                {section !== null && (
                  <span
                    className={section.kind === "data" ? "wb-tag" : "wb-tag docs"}
                    data-testid={`section-tag-${index}`}
                  >
                    {t(
                      section.kind === "data"
                        ? "reports.workbench.tagDatabase"
                        : "reports.workbench.tagDocuments",
                    )}
                  </span>
                )}
              </button>
              <button
                type="button"
                className="wb-node-del"
                aria-label={t("reports.blocks.delete", { name: label(block) })}
                onClick={() => list.remove(index)}
              >
                ×
              </button>
            </li>
          );
        })}
      </ul>

      <div className="wb-tree-foot">
        {/* Nine buttons used to stand here permanently, above ~380px of empty
            panel, for something done once or twice per report. They are behind
            one button now — which is also where the answer to "where will it
            land?" belongs, next to the control rather than in a hint nobody
            reads twice. */}
        {adding && (
          <div role="group" aria-label={t("reports.blocks.addLabel")} className="wb-palette">
            {list.addable.map((kind) => (
              <button
                key={kind}
                type="button"
                onClick={() => {
                  list.add(kind);
                  setAdding(false);
                }}
              >
                + {t(`reports.blocks.kinds.${kind}`)}
              </button>
            ))}
            <p className="wb-hint">{t("reports.workbench.addHint")}</p>
          </div>
        )}
        <button
          type="button"
          className="wb-add-toggle"
          data-testid="block-add-toggle"
          aria-expanded={adding}
          onClick={() => setAdding((open) => !open)}
        >
          {adding ? t("reports.workbench.addClose") : `+ ${t("reports.workbench.addHeading")}`}
        </button>
      </div>

      {/* The undo window. It has to be findable without hunting — a six-second
          offer the reader has to look for is not an offer (parent design §7). */}
      {list.deleted !== null && (
        <div role="status" className="wb-undo">
          <span>
            {t("reports.blocks.deleted", { name: label(list.deleted.block) })}{" "}
            {t("reports.blocks.secondsLeft", { count: list.deleted.secondsLeft })}
          </span>
          <button type="button" onClick={list.undo}>
            {t("reports.blocks.undo")}
          </button>
        </div>
      )}
    </div>
  );
}
