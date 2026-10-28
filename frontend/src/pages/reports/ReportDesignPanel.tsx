import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { TemplateGallery } from "./TemplateGallery";
import { ApiError } from "../../api/client";
import { downloadReport, rerenderReport, setReportDesign } from "../../api/reports";
import type { Report, TemplateChoice } from "../../api/reports";

export interface ReportDesignPanelProps {
  report: Report;
  /** The shipped templates, fetched once by the page. A panel per finished
   *  report each running its own `listReportTemplates()` meant twenty identical
   *  requests on a list of twenty reports. */
  templates: TemplateChoice[];
  /** Why the list above is empty, or null. Owned by the page for the same
   *  reason: one fetch, one failure, one message. */
  templatesError: string | null;
  onApplied: (report: Report) => void;
}

/** Choosing a template for one finished report, and showing the result.
 *
 *  The preview is the artifact itself: the panel stores the choice, rebuilds
 *  from stored section data, and fetches the PDF that produced. There is no
 *  HTML preview, and that is a decision rather than an omission — the report's
 *  `@font-face` rules point inside matplotlib's wheel with `file://` URLs a
 *  browser cannot load, and its paper colour, running header, page footer and
 *  landscape page all live in `@page`, which a browser ignores. Previewing in
 *  HTML would mean serving the fonts and emulating pagination: a second layout
 *  system, free to drift from the one that prints.
 */
export function ReportDesignPanel({
  report,
  templates,
  templatesError,
  onApplied,
}: ReportDesignPanelProps): ReactNode {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The choice was stored but this report was not rebuilt from it. Set only by an
  // apply that took that path — never on first paint, because before the reader
  // chooses anything there is no choice to have saved.
  const [savedOnly, setSavedOnly] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  // The URL is read by the cleanup that runs after it has already been replaced
  // in state, so the effect cannot be the one holding it.
  const previewRef = useRef<string | null>(null);
  // Whether this panel is still on screen, readable from an in-flight apply.
  const live = useRef(true);

  useEffect(() => {
    // Set on the way in as well as cleared on the way out. StrictMode mounts,
    // unmounts and remounts in development, so a flag that is only ever cleared
    // stays false for the rest of the panel's life — and every apply from then
    // on would throw away the artifact it just fetched.
    live.current = true;
    return () => {
      live.current = false;
      if (previewRef.current !== null) URL.revokeObjectURL(previewRef.current);
    };
  }, []);

  function show(url: string | null): void {
    if (previewRef.current !== null) URL.revokeObjectURL(previewRef.current);
    previewRef.current = url;
    setPreview(url);
  }

  async function choose(key: string): Promise<void> {
    setBusy(true);
    setError(null);
    setSavedOnly(false);
    // Hoisted, because the catch has to know whether the PUT already landed: a
    // 409 from the rebuild arrives *after* the choice was stored, and telling the
    // reader the template could not be applied would be simply untrue.
    let stored: Report | null = null;
    try {
      // The choice is stored whatever else is true: it is an intent, and a
      // report that cannot be rebuilt today still wears it the next time it is
      // built. Only the rebuild is conditional.
      // Only the template travels: the endpoint is a partial update, so a block
      // list the reader has already stored survives a template change untouched.
      stored = await setReportDesign(report.repository_id, report.id, { template: key });
      if (!report.can_rerender) {
        onApplied(stored);
        setSavedOnly(true);
        setOpen(false);
        return;
      }
      const rebuilt = await rerenderReport(report.repository_id, report.id);
      const blob = await downloadReport(report.repository_id, report.id, "pdf");
      const url = URL.createObjectURL(blob);
      // An apply that outlives its panel must not strand the blob: `show()`
      // would park it in a ref whose cleanup has already run, and the PDF would
      // be held for the life of the document.
      if (!live.current) {
        URL.revokeObjectURL(url);
        return;
      }
      show(url);
      onApplied(rebuilt);
      setOpen(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && stored !== null) {
        // The race the spec named: the stored section data went away between the
        // list arriving and the click. The design endpoint still answered, so the
        // row really does carry the new template — hand it up, or the page keeps
        // a stale copy until the next poll, and say what actually happened.
        onApplied(stored);
        setSavedOnly(true);
        setOpen(false);
      } else {
        setError(t("reports.design.errorApply"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="report-design">
      <button type="button" onClick={() => setOpen(true)}>
        {t("reports.design.openGallery")}
      </button>
      {/* Structural editing lives at its own route. These two are not two ways
          to do the same thing: this button swaps a template in one click, which
          is a bad reason to navigate to a full-page workbench. */}
      {report.can_rerender && (
        <Link className="report-edit-design" to={`/reports/${report.id}/design`}>
          {t("reports.workbench.editDesign")}
        </Link>
      )}
      {/* While the gallery is up it covers the page, so these two say nothing to
          anybody; the gallery renders its own copies instead. */}
      {!open && busy && <span role="status">{t("reports.design.applying")}</span>}
      {!open && error !== null && <p className="error">{error}</p>}
      {!report.can_rerender && <p className="report-note">{t("reports.design.cannotRebuild")}</p>}
      {savedOnly && <p className="report-note">{t("reports.design.savedForNextBuild")}</p>}
      {report.template === null && templates.length > 0 && (
        <p className="report-note">{t("reports.design.noChoice", { name: templates[0].name })}</p>
      )}
      {report.can_rerender && !report.has_pdf && preview === null && (
        <p className="report-note">{t("reports.design.noPreview")}</p>
      )}
      {preview !== null && (
        <iframe className="report-preview" title={t("reports.design.previewTitle")} src={preview} />
      )}
      {open && (
        <TemplateGallery
          templates={templates}
          selected={report.template}
          lede={t("reports.design.galleryLede")}
          busy={busy}
          error={error ?? templatesError}
          onChoose={(key) => void choose(key)}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
