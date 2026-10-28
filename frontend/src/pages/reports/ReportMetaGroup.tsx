import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export interface ReportMetaGroupProps {
  blocks: number;
  sections: number;
  /** From `report.plan`, which is null for reports predating the review loop and
   *  for frozen-schedule runs. The row is omitted rather than guessed. */
  language: string | null;
}

/** The «Звіт» group: what this document is, in a few numbers.
 *
 *  **There is no page count, and that is deliberate.** A page count needs
 *  WeasyPrint's line breaking; neither this editor nor the HTML canvas of step
 *  6b can know one, and a guessed page number in the editor of a filed record is
 *  a lie the reader has no way to catch. The mockup draws «Сторінок: 4»; this is
 *  the one place the mockup is not followed, and the reason is recorded here
 *  rather than left to be rediscovered.
 */
export function ReportMetaGroup({ blocks, sections, language }: ReportMetaGroupProps): ReactNode {
  const { t } = useTranslation();

  return (
    <div className="wb-meta">
      <div>
        <span>{t("reports.workbench.metaBlocks")}</span>
        <b data-testid="meta-blocks">{blocks}</b>
      </div>
      <div>
        <span>{t("reports.workbench.metaSections")}</span>
        <b data-testid="meta-sections">{sections}</b>
      </div>
      {language !== null && (
        <div>
          <span>{t("reports.workbench.metaLanguage")}</span>
          {/* A language this build has no name for shows its own code: the model
              picks the report's language, so the set is not closed here. */}
          <b data-testid="meta-language">{t(`reports.workbench.lang.${language}`, language)}</b>
        </div>
      )}
    </div>
  );
}
