import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { blockIcon } from "./blockIcons";
import { ICON_NAMES, documentIcon } from "./documentIcons";
import type { BlockList } from "./useBlockList";
import type { ReportBlock, ReportDesign, ReportSectionSummary } from "../../api/reports";

/** Kinds whose words the reader may replace. Mirrors `TEXT_KINDS` in
 *  `services/report_blocks.py`, which is where the rule is *enforced* — `text` on
 *  any other kind is a 422, because those print values derived from the stored
 *  result and a document may not disagree with the database. This set decides
 *  what the editor offers; it is not what makes the restriction true. */
const EDITABLE: ReadonlySet<string> = new Set(["cover", "heading", "prose", "sources", "callout"]);

/** Kinds that may wear a mark. Mirrors `_ICON_KINDS` in `report_blocks.py`,
 *  which is where the rule is *enforced* — an icon on anything else is a 422.
 *  Every other block prints a value computed from the stored result, and a mark
 *  there would be the document having an opinion about it. */
const MARKABLE: ReadonlySet<string> = new Set(["heading", "callout"]);

export interface BlockInspectorProps {
  list: BlockList;
  design: ReportDesign;
}

/** The «Блок» group's body: everything that is true of the selected block only.
 *
 *  **The variant and chart-type chips are the backend's answer, not this
 *  component's.** Which chart types a section permits is derived from its stored
 *  result server-side and arrives on `design.sections`. Nothing here re-derives
 *  it, so the editor cannot reintroduce the defects #131 removed — most visibly,
 *  a pie is not offered for a time series because it is absent from that list,
 *  not because this file knows to leave it out.
 */
export function BlockInspector({ list, design }: BlockInspectorProps): ReactNode {
  const { t } = useTranslation();
  const index = list.selected;
  const block = index !== null ? (list.blocks[index] ?? null) : null;

  function sectionOf(b: ReportBlock): ReportSectionSummary | null {
    if (b.section === undefined) return null;
    return design.sections[b.section] ?? null;
  }

  if (index === null || block === null) {
    // A real empty state rather than a hint floating above four collapsed
    // groups — the panel has nothing to say here and should say that once.
    return (
      <div className="wb-empty">
        <p>{t("reports.workbench.selectABlock")}</p>
      </div>
    );
  }

  const section = sectionOf(block);

  const kindName = t(`reports.blocks.kinds.${block.kind}`, block.kind);

  return (
    <>
      {/* The panel names what it is editing. «Обери блок ліворуч» used to float
          here even with a block selected, which told the reader nothing about
          which one. */}
      <div className="wb-insp-head">
        <span className="wb-ico">{blockIcon(block.kind)}</span>
        <b>{kindName}</b>
        <em>{t("reports.workbench.blockOrdinal", { n: index + 1 })}</em>
      </div>

      {EDITABLE.has(block.kind) ? (
        <div className="wb-field">
          {/* The words are typed on the sheet as of 6c — there is no second copy
              of them here. A textarea beside the page had to be *seeded* from the
              selected block, and got that wrong in exactly the way a document
              cannot: it kept showing the previous block's words. The document is
              the state now, so the class of defect is gone rather than fixed. */}
          <p className="wb-hint">{t("reports.blocks.textOnSheet")}</p>
          {/* A callout has no generated original to go back to. */}
          {block.text !== undefined && block.kind !== "callout" && (
            <button type="button" onClick={() => list.resetText(index)}>
              {t("reports.blocks.textReset")}
            </button>
          )}
        </div>
      ) : (
        block.kind !== "pagebreak" && <p className="wb-hint">{t("reports.blocks.textComputed")}</p>
      )}

      {block.kind === "viz" && (
        <div className="wb-field">
          <label>{t("reports.blocks.variantLabel")}</label>
          <div role="group" aria-label={t("reports.blocks.variantLabel")} className="chips">
            {(["table", "chart"] as const).map((variant) => (
              <button
                key={variant}
                type="button"
                aria-pressed={block.variant === variant}
                onClick={() => list.replace(index, { variant })}
              >
                {t(`reports.blocks.variants.${variant}`)}
              </button>
            ))}
          </div>
        </div>
      )}

      {block.kind === "viz" && block.variant === "chart" && section !== null && (
        <div className="wb-field">
          <label>{t("reports.blocks.chartTypeLabel")}</label>
          <div role="group" aria-label={t("reports.blocks.chartTypeLabel")} className="chips">
            {section.allowed_chart_types.map((type) => (
              <button
                key={type}
                type="button"
                aria-pressed={block.chart_type === type}
                onClick={() => list.replace(index, { chart_type: type })}
              >
                {t(`reports.blocks.chartTypes.${type}`, type)}
              </button>
            ))}
          </div>
          {section.allowed_chart_types.length === 0 && (
            <p className="wb-hint">{t("reports.blocks.noChartTypes")}</p>
          )}
        </div>
      )}

      {MARKABLE.has(block.kind) && (
        <div className="wb-field">
          <label>{t("reports.blocks.iconLabel")}</label>
          <div role="group" aria-label={t("reports.blocks.iconLabel")} className="wb-icons">
            {ICON_NAMES.map((name) => (
              <button
                key={name}
                type="button"
                // The name is the accessible label as well as the value: these
                // are eleven abstract glyphs, and a grid of unlabelled buttons
                // is unusable with a screen reader and unguessable with a mouse.
                title={name === "none" ? t("reports.blocks.iconNone") : name}
                aria-label={name === "none" ? t("reports.blocks.iconNone") : name}
                // Absent and "none" are the same picture. The payload can tell
                // them apart; the reader cannot, so neither should this.
                aria-pressed={(block.icon ?? "none") === name}
                onClick={() => list.replace(index, { icon: name })}
              >
                {documentIcon(name) ?? "—"}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="wb-field">
        <label>{t("reports.workbench.actions")}</label>
        <div className="wb-rowbtns">
          <button
            type="button"
            data-testid="block-up"
            disabled={index === 0}
            onClick={() => list.move(index, index - 1)}
          >
            {t("reports.workbench.moveUp")}
          </button>
          <button
            type="button"
            data-testid="block-down"
            disabled={index === list.blocks.length - 1}
            onClick={() => list.move(index, index + 1)}
          >
            {t("reports.workbench.moveDown")}
          </button>
          <button type="button" data-testid="block-duplicate" onClick={() => list.duplicate(index)}>
            {t("reports.workbench.duplicate")}
          </button>
          <button type="button" data-testid="block-hide" onClick={() => list.toggleHidden(index)}>
            {t(
              block.hidden === true ? "reports.workbench.showBlock" : "reports.workbench.hideBlock",
            )}
          </button>
          <button
            type="button"
            className="danger"
            data-testid="block-delete"
            onClick={() => list.remove(index)}
          >
            {t("reports.blocks.deleteShort")}
          </button>
        </div>
      </div>
    </>
  );
}
