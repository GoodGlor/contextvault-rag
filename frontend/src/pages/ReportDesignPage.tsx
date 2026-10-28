import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { BlockInspector } from "./reports/BlockInspector";
import { BlockTree } from "./reports/BlockTree";
import { ReportCanvas } from "./reports/ReportCanvas";
import { HowToStrip } from "./reports/HowToStrip";
import { InspectorGroup } from "./reports/InspectorGroup";
import { ReportMetaGroup } from "./reports/ReportMetaGroup";
import { StyleInspector } from "./reports/StyleInspector";
import { TemplateGallery } from "./reports/TemplateGallery";
import { WorkbenchTopbar } from "./reports/WorkbenchTopbar";
import { useBlockList } from "./reports/useBlockList";
import { useReportPreview } from "./reports/useReportPreview";
import { useCurrentRepository } from "../repository/RepositoryContext";
import { reportAssetUrl, uploadReportAsset } from "../api/reportAssets";
import { getReport, getReportDesign, listReportTemplates, rerenderReport } from "../api/reports";
import type { Report, ReportBlock, ReportDesign, TemplateChoice } from "../api/reports";
import type { StyleGroups, StyleUpdate } from "./reports/styleGroups";

/** The design workbench: one report, three columns, its own URL.
 *
 *  **The repository comes from context, not the URL.** A report id is what a
 *  person would paste, and the selection persists in `localStorage`, so a reload
 *  survives. A deep link opened while a *different* repository is selected gets a
 *  404 from `GET …/reports/{id}` and is told so — an explicit dead end rather
 *  than an editor quietly showing nothing.
 *
 *  **The centre column is the live sheet, with the real PDF one click away.**
 *  The sheet is `build_report_html`'s own output under `preview=True` — the
 *  print renderer, not a second one — so what you point at is what prints. Step
 *  5c's objection is answered rather than overruled, and its measurement stands.
 */
export function ReportDesignPage(): ReactNode {
  const { t } = useTranslation();
  const { reportId = "" } = useParams();
  const { currentRepoId } = useCurrentRepository();

  const [report, setReport] = useState<Report | null>(null);
  const [missing, setMissing] = useState(false);
  const [design, setDesign] = useState<ReportDesign | null>(null);
  const [style, setStyle] = useState<StyleGroups | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [templates, setTemplates] = useState<TemplateChoice[]>([]);

  useEffect(() => {
    if (currentRepoId === "" || reportId === "") return;
    let cancelled = false;
    void getReport(currentRepoId, reportId)
      .then((loaded) => {
        if (!cancelled) setReport(loaded);
      })
      .catch(() => {
        if (!cancelled) setMissing(true);
      });
    return () => {
      cancelled = true;
    };
  }, [currentRepoId, reportId]);

  useEffect(() => {
    if (report === null) return;
    let cancelled = false;
    void getReportDesign(currentRepoId, reportId)
      .then((loaded) => {
        if (cancelled) return;
        setDesign(loaded);
        // `?? {}` is read tolerance, not defensiveness for its own sake: a cached
        // SPA meeting a response without these groups throws inside the inspector
        // and unmounts the whole editor, taking the block tree with it. An e2e run
        // caught exactly that in 5f — four specs failed with the list simply absent.
        setStyle({
          typography: loaded.typography ?? {},
          page: loaded.page ?? {},
          brand: loaded.brand ?? {},
        });
      })
      .catch(() => {
        if (!cancelled) setError(t("reports.blocks.errorLoad"));
      });
    return () => {
      cancelled = true;
    };
  }, [report, currentRepoId, reportId, t]);

  useEffect(() => {
    let cancelled = false;
    void listReportTemplates()
      .then((loaded) => {
        if (!cancelled) setTemplates(loaded);
      })
      .catch(() => {
        // The gallery renders its own empty state; a failed template list must
        // not stop the editor loading.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (missing) {
    return (
      <div className="wb-missing">
        <p>{t("reports.workbench.notInRepository")}</p>
        <Link to="/reports">{t("reports.workbench.back")}</Link>
      </div>
    );
  }

  if (report === null || design === null || style === null) {
    return (
      <div className="wb-missing">
        {error !== null ? <p className="error">{error}</p> : <p>{t("reports.loadingReports")}</p>}
      </div>
    );
  }

  return (
    <Workbench
      report={report}
      design={design}
      style={style}
      setStyle={setStyle}
      templates={templates}
      repositoryId={currentRepoId}
    />
  );
}

interface WorkbenchProps {
  report: Report;
  design: ReportDesign;
  style: StyleGroups;
  setStyle: (next: (current: StyleGroups | null) => StyleGroups | null) => void;
  templates: TemplateChoice[];
  repositoryId: string;
}

/** The columns themselves.
 *
 *  Split from the loader because `useBlockList` and `useReportPreview` may not
 *  sit behind an early return — a hook called conditionally is a different hook
 *  order on the next render. Mounting this only once the data is present is what
 *  lets the loader above return early at all.
 */
function Workbench({
  report,
  design,
  style,
  setStyle,
  templates,
  repositoryId,
}: WorkbenchProps): ReactNode {
  const { t } = useTranslation();
  const { repos } = useCurrentRepository();
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  /** The sheet's scale, held here rather than in `ReportCanvas` (#172).
   *
   *  The centre column renders the canvas behind a ternary on the view, so a
   *  trip to `pdf` and back unmounts it — and a scale kept inside it was
   *  discarded on a switch the reader means as a detour, silently returning to
   *  100 %. This is the smallest thing that outlives the switch. The canvas
   *  still does the *measuring* for fit-to-width, which needs a width only it
   *  has; what moved here is the number, not the behaviour. */
  /** …and whether the reader has chosen one yet.
   *
   *  The editor opens fit-to-width, which is what a reader wants on arrival, but
   *  only *once*: after any zoom of their own — including the fit itself — the
   *  number is theirs and nothing re-fits it. The flag lives here rather than in
   *  the canvas for the same reason `zoom` does: the view switch unmounts the
   *  canvas, and a flag that resets there would re-fit over a chosen scale. */
  const [zoom, setZoom] = useState(1);
  const [fitted, setFitted] = useState(false);

  const onZoom = useCallback((next: number): void => {
    setZoom(next);
    setFitted(true);
  }, []);

  const preview = useReportPreview({ repositoryId, reportId: report.id });

  // The groups as they are *now*, for changes that resolve later than they are
  // requested. Kept in step with the prop, and written eagerly by `applyStyle`
  // so two changes in one tick compose rather than the second losing the first.
  const styleRef = useRef(style);
  useEffect(() => {
    styleRef.current = style;
  }, [style]);

  const onBlocks = useCallback((blocks: ReportBlock[]) => preview.request({ blocks }), [preview]);
  const list = useBlockList(design, onBlocks);

  /** Merge one group's change into local state and send it to the preview hook.
   *
   *  The changed group travels **whole**, which is what the backend stores: a
   *  per-field merge server-side would make "unset this one token" impossible to
   *  express, and unset is exactly what "as template" means. */
  const applyStyle = useCallback(
    (change: StyleUpdate) => {
      // Resolved against the ref rather than the `style` this callback closed
      // over. A logo upload's `.then` runs several renders later, and reading
      // `brand` at click time meant the accent picked meanwhile was overwritten
      // by the snapshot — a group travels whole, so the stale copy replaces the
      // live one instead of merging with it (#188).
      //
      // It matters more here than on the pre-generation step: `useReportPreview`
      // keeps only the *last* waiting patch rather than coalescing them, so the
      // accent's own request is discarded as well. The logo's patch is the only
      // one that will be sent, and it has to carry both.
      const resolved = typeof change === "function" ? change(styleRef.current) : change;
      styleRef.current = { ...styleRef.current, ...resolved };
      setStyle((current) => (current === null ? current : { ...current, ...resolved }));
      preview.request(resolved);
    },
    [preview, setStyle],
  );

  const repositoryName = repos.find((repo) => repo.id === repositoryId)?.name ?? "";

  /** What to call a block on the canvas. The same words the tree uses — one
   *  vocabulary, so pointing at a row and pointing at the sheet name the same
   *  thing. Passed in rather than looked up inside `ReportCanvas`, which is
   *  presentational and owns no copy. */
  const blockLabel = useCallback(
    (index: number): string => {
      const block = list.blocks[index];
      if (block === undefined) return "";
      const kind = t(`reports.blocks.kinds.${block.kind}`, block.kind);
      const section = block.section !== undefined ? design.sections[block.section] : undefined;
      return section !== undefined ? `${kind} — ${section.title}` : kind;
    },
    [list.blocks, design.sections, t],
  );

  async function rerender(): Promise<void> {
    setBusy(true);
    try {
      await rerenderReport(repositoryId, report.id);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="workbench">
      <WorkbenchTopbar
        repositoryName={repositoryName}
        reportTitle={design.title ?? report.prompt}
        busy={busy || preview.pending}
        backTo="/reports"
        view={preview.view}
        onView={preview.setView}
        onOpenGallery={() => setGalleryOpen(true)}
        onRerender={() => void rerender()}
      />

      <div className="wb-columns">
        <aside className="wb-panel">
          <BlockTree list={list} design={design} />
        </aside>

        <section className="wb-canvas">
          <HowToStrip />
          {preview.pending && <span role="status">{t("reports.blocks.rebuilding")}</span>}
          {preview.error !== null && <p className="error">{preview.error}</p>}

          {preview.view === "sheet" ? (
            <ReportCanvas
              html={preview.html}
              // The canvas holds no selection of its own: clicking a row and
              // clicking the block it draws move the same value, so the tree and
              // the sheet cannot disagree about what is selected.
              selected={list.selected}
              onSelect={list.select}
              label={blockLabel}
              blockCount={list.blocks.length}
              onMove={list.move}
              onToggleHidden={list.toggleHidden}
              onDelete={list.remove}
              // The same `text` override the tree's textarea wrote until 6c, and
              // the same debounced PUT behind it. What moved is where a reader
              // types it, not what gets stored.
              onEditText={(index, text) => list.replace(index, { text })}
              zoom={zoom}
              onZoom={onZoom}
              autoFit={!fitted}
            />
          ) : (
            <>
              {preview.url !== null && (
                <iframe
                  className="report-preview"
                  title={t("reports.design.previewTitle")}
                  src={preview.url}
                />
              )}
              {preview.url === null && !preview.pending && (
                <p className="wb-hint">{t("reports.workbench.previewHint")}</p>
              )}
            </>
          )}
        </section>

        <aside className="wb-panel wb-panel-right">
          <InspectorGroup
            id="block"
            title={t("reports.workbench.groupBlock")}
            defaultOpen
            // Choosing a block puts its properties on screen without a second
            // click, even for a reader who had collapsed this group (#194).
            revealKey={list.selected}
          >
            <BlockInspector list={list} design={design} />
          </InspectorGroup>
          <StyleInspector
            typography={style.typography}
            page={style.page}
            brand={style.brand}
            logoUrl={
              style.brand.logo_asset_id
                ? `/api${reportAssetUrl(repositoryId, style.brand.logo_asset_id)}`
                : null
            }
            onChange={applyStyle}
            onLogoFile={(file) => {
              void uploadReportAsset(repositoryId, file).then((asset) =>
                applyStyle((current) => ({
                  brand: { ...current.brand, logo_asset_id: asset.id },
                })),
              );
            }}
            onLogoClear={() => applyStyle({ brand: { ...style.brand, logo_asset_id: null } })}
            onReset={() => applyStyle({ typography: {}, page: {}, brand: {} })}
          />
          <InspectorGroup id="meta" title={t("reports.workbench.groupReport")}>
            <ReportMetaGroup
              blocks={list.blocks.length}
              sections={design.sections.length}
              language={report.plan?.language ?? null}
            />
          </InspectorGroup>
        </aside>
      </div>

      {galleryOpen && (
        <TemplateGallery
          templates={templates}
          selected={design.template}
          lede={t("reports.design.galleryLede")}
          busy={preview.pending}
          error={null}
          onChoose={(key) => {
            // Through the preview hook like every other edit, so a template
            // change and a structural edit landing together cannot race.
            preview.request({ template: key });
            setGalleryOpen(false);
          }}
          onClose={() => setGalleryOpen(false)}
        />
      )}
    </div>
  );
}
