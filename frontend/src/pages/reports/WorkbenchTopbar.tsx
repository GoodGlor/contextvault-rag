import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { PreviewView } from "./useReportPreview";

export interface WorkbenchTopbarProps {
  repositoryName: string;
  reportTitle: string;
  view: PreviewView;
  onView: (view: PreviewView) => void;
  /** A rebuild or a preview render is in flight. Rebuilding twice at once buys
   *  nothing — the second render replaces the first — so the action is refused
   *  rather than queued. */
  busy: boolean;
  onOpenGallery: () => void;
  onRerender: () => void;
  backTo: string;
}

/** The workbench's top strip — **one row, 52 px** (#167).
 *
 *  It used to be two, and not by design: `flex-wrap: wrap` plus more content
 *  than fits produced a second line, costing ~58 px on the axis a page is
 *  longest in. It no longer wraps; the report name shrinks instead, because it
 *  is the one thing here that can be read from its first few words.
 *
 *  **The view switch moved next to the canvas it governs.** It used to sit at
 *  the far top-right, ~1 200 px from the artifact it swaps, where its position
 *  read as navigation rather than as a control on the thing below it.
 *
 *  **A zoom control was built here and cut** (#167), and #168 shipped it — in
 *  `ReportCanvas`, not here. It belongs beside the sheet it scales: it means
 *  nothing in `pdf` view, and fit-to-width needs a width only the canvas has
 *  measured. **That reasoning holds for the control and not for its value**: the
 *  view switch unmounts the canvas, so #172 moved the `zoom` number up to
 *  `Workbench` while the buttons and the measuring stayed here. The reason it
 *  was cut turned out not to be the browser at all; the
 *  scale had been applied to an ancestor of the floating toolbar, which is a
 *  containing block for a `position: fixed` descendant.
 *
 *  **The Аркуш/PDF switch is live as of 6b.** 6a left it out rather than ship a
 *  two-position switch with one working position; both positions now work, and
 *  they are two views of one stored design. The sheet is where you select and
 *  edit; the PDF stays the artifact of record, one click away — which is step
 *  5c's objection answered in the interface rather than ignored.
 *
 *  **Two controls the mockup draws are still absent, each for a stated reason**
 *  (both held on #159):
 *
 *  - **«Оновити дані»** is a re-run with LLM calls, not a design action. One
 *    report is five to fifteen provider calls against a daily free-tier bucket,
 *    and that is not something to spend by mis-clicking between two harmless
 *    buttons.
 *  - **«Зберегти як шаблон»** is the repository default, which already has a
 *    control in `RepositoryTemplateField`. A second writer of one field is
 *    exactly how those two came to disagree once already.
 */
export function WorkbenchTopbar({
  repositoryName,
  reportTitle,
  view,
  onView,
  busy,
  onOpenGallery,
  onRerender,
  backTo,
}: WorkbenchTopbarProps): ReactNode {
  const { t } = useTranslation();

  return (
    <header className="wb-topbar">
      <Link className="wb-back" to={backTo} aria-label={t("reports.workbench.back")}>
        ←
      </Link>
      <div className="wb-brand">
        <span className="wb-glyph">CV</span>
        {t("reports.workbench.title")}
      </div>
      <div className="wb-crumb">
        {repositoryName} · <b>{reportTitle}</b>
      </div>

      <div className="wb-spacer" />

      <div className="wb-views" role="group" aria-label={t("reports.workbench.viewLabel")}>
        <button
          type="button"
          aria-pressed={view === "sheet"}
          onClick={() => onView("sheet")}
          disabled={busy}
        >
          {t("reports.workbench.viewSheet")}
        </button>
        <button
          type="button"
          aria-pressed={view === "pdf"}
          onClick={() => onView("pdf")}
          disabled={busy}
        >
          {t("reports.workbench.viewPdf")}
        </button>
      </div>

      <div className="wb-spacer" />

      <button type="button" onClick={onOpenGallery}>
        {t("reports.workbench.templates")}
      </button>
      <button type="button" className="primary" disabled={busy} onClick={onRerender}>
        {t("reports.workbench.rerender")}
      </button>
    </header>
  );
}
