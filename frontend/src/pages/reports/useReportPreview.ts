import { useCallback, useEffect, useRef, useState } from "react";
import {
  downloadReport,
  getReportPreviewHtml,
  rerenderReport,
  setReportDesign,
} from "../../api/reports";
import type { ReportDesignPatch } from "../../api/reports";

/** Which artifact the centre column is showing.
 *
 *  `sheet` is the canvas — where you select and edit. `pdf` is the artifact of
 *  record, one click away. Both are built from the same stored design; they
 *  differ in what the server is asked to do with it. */
export type PreviewView = "sheet" | "pdf";

export interface UseReportPreviewOptions {
  repositoryId: string;
  reportId: string;
  /** Idle time before an edit is rendered. */
  delayMs?: number;
}

export interface ReportPreview {
  /** The canvas's document, or null before the first render. */
  html: string | null;
  /** Object URL of the current PDF, or null while it has never been asked for. */
  url: string | null;
  /** Which artifact the centre column shows. */
  view: PreviewView;
  /** Switch views. Moving to `pdf` renders one on demand, so a reader who never
   *  opens it never pays to print. */
  setView: (view: PreviewView) => void;
  /** True from the keystroke until the preview on screen matches the edit. */
  pending: boolean;
  error: string | null;
  /** Ask for a preview of this design. Debounced, then coalesced.
   *
   *  Takes the whole patch rather than a template, so the editor's structural
   *  edits and the gallery's template choice converge on one debounced path.
   *  Two paths would race: a reorder and a template change landing together
   *  would each rebuild, and the later rebuild would publish whichever design
   *  its own PUT had stored. */
  request: (design: ReportDesignPatch) => void;
}

/** The editor's preview: two views of one stored design, debounced and coalesced.
 *
 *  Step 5c measured the alternatives and chose the printed round-trip, recording
 *  the objection: an HTML preview would be "a second layout system, free to drift
 *  from the one that prints", and would need a font endpoint.
 *
 *  **Step 6b answers that rather than overruling it.** The canvas is the *print
 *  renderer's own output* under `preview=True` — one renderer, not two — and the
 *  font endpoint is built (`GET /api/report-fonts/{slug}`), serving the very files
 *  WeasyPrint prints from. What 5c measured still holds; what it concluded no
 *  longer follows.
 *
 *  The saving is real but smaller than the framing suggests: redrawing the charts
 *  dominates both paths and only the printing is skipped, so the sheet costs
 *  roughly half the PDF path, not a fortieth (measured in
 *  `services/reports.py::preview_report_html`). The PDF is therefore built **on
 *  demand** — switching to it, not editing — so a reader who stays on the sheet
 *  never pays to print at all.
 *
 *  **One hook still owns both views**, for the reason below: two request paths
 *  would race, and a reorder and a template change landing together would each
 *  rebuild, with the later rebuild publishing whichever design its own PUT stored.
 *
 *  Two rules do that work, and they are different rules:
 *
 *  **Debounce** collapses a burst of edits into one render, so typing costs nothing
 *  until it stops. **Coalescing** handles what debounce cannot: edits that arrive
 *  while a render is already in flight. A queue would run one render per edit and
 *  fall further behind the longer someone works — at 257 ms–2.25 s each, an
 *  unbounded backlog. Instead at most one render runs at a time and at most one
 *  state waits behind it; a newer edit replaces the waiting one rather than joining
 *  a line. The reader always converges on their latest state, having paid for two
 *  renders at most.
 */
export function useReportPreview({
  repositoryId,
  reportId,
  delayMs = 300,
}: UseReportPreviewOptions): ReportPreview {
  const [html, setHtml] = useState<string | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [view, setViewState] = useState<PreviewView>("sheet");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `render` runs asynchronously and must read the view as it is *now*, not as it
  // was when the closure was made — a switch during a render would otherwise
  // publish the artifact the reader just navigated away from.
  const viewRef = useRef<PreviewView>("sheet");

  // Which view's artifact reflects the current design. An edit staleness-marks
  // **both**, because only the shown one is rebuilt: editing on the sheet leaves
  // the stored PDF describing a design nobody chose, and showing it on a switch
  // would be worse than a pause.
  const fresh = useRef<Record<PreviewView, boolean>>({ sheet: false, pdf: false });

  // The URL is revoked by code that runs after state has already moved on, so the
  // ref — not the state — is what owns it.
  const urlRef = useRef<string | null>(null);
  const live = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlight = useRef(false);
  // The newest state not yet rendered. A patch may legitimately carry `null`
  // (clearing the template or the block list), so emptiness is the absence of the
  // box rather than a falsy value inside it.
  const waiting = useRef<{ design: ReportDesignPatch | null } | null>(null);
  // Only the newest render may publish. Serialising renders makes an out-of-order
  // resolution unreachable today; this still guards one that outlives its hook.
  const generation = useRef(0);

  useEffect(() => {
    // Set on the way in as well as cleared on the way out: StrictMode mounts,
    // unmounts and remounts, and a flag only ever cleared would strand every
    // later render's blob for the life of the document.
    live.current = true;
    return () => {
      live.current = false;
      if (timer.current !== null) clearTimeout(timer.current);
      if (urlRef.current !== null) URL.revokeObjectURL(urlRef.current);
    };
  }, []);

  const show = useCallback((next: string): void => {
    if (urlRef.current !== null) URL.revokeObjectURL(urlRef.current);
    urlRef.current = next;
    setUrl(next);
  }, []);

  const render = useCallback(
    async (design: ReportDesignPatch | null): Promise<void> => {
      inFlight.current = true;
      const mine = ++generation.current;
      try {
        // `null` is a view switch rather than an edit: the stored design is
        // already what the reader wants, and re-PUTting it would be a write with
        // nothing to say.
        if (design !== null) await setReportDesign(repositoryId, reportId, design);
        if (viewRef.current === "sheet") {
          const document = await getReportPreviewHtml(repositoryId, reportId);
          if (!live.current || mine !== generation.current) return;
          fresh.current.sheet = true;
          setHtml(document);
          setError(null);
          return;
        }
        await rerenderReport(repositoryId, reportId);
        const blob = await downloadReport(repositoryId, reportId, "pdf");
        const next = URL.createObjectURL(blob);
        // Superseded or unmounted: the bytes are still ours to release.
        if (!live.current || mine !== generation.current) {
          URL.revokeObjectURL(next);
          return;
        }
        fresh.current.pdf = true;
        show(next);
        setError(null);
      } catch (err) {
        if (live.current) setError(err instanceof Error ? err.message : String(err));
      } finally {
        inFlight.current = false;
        const next = waiting.current;
        waiting.current = null;
        if (next !== null && live.current) {
          void render(next.design);
        } else if (live.current) {
          setPending(false);
        }
      }
    },
    [repositoryId, reportId, show],
  );

  // Draw the sheet as soon as the editor opens, rather than waiting for a first
  // edit. Under 5c that wait was defensible — the preview cost a print, and
  // printing a report nobody had touched was spending for nothing. The sheet
  // costs no print, and an editor that opens on an empty canvas until you change
  // something is the whole point of this card missed.
  //
  // The PDF view is deliberately *not* fetched on mount, for the original
  // reason: it still costs one.
  useEffect(() => {
    if (viewRef.current !== "sheet" || fresh.current.sheet || inFlight.current) return;
    setPending(true);
    void render(null);
    // `render` is stable across a mount, and this must run once per mounted
    // editor rather than on every identity change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Switch views, rendering the newly-shown artifact if it is not current.
   *
   *  The sheet is not re-fetched on a switch back: the document it holds is
   *  still the stored design's, and an edit would have replaced it. The PDF is
   *  built on demand — it costs a print, and a reader who never opens it never
   *  pays for one.
   */
  const setView = useCallback(
    (next: PreviewView): void => {
      if (next === viewRef.current) return;
      viewRef.current = next;
      setViewState(next);
      if (fresh.current[next]) return;
      setPending(true);
      if (inFlight.current) {
        // Nothing to store: a render already running finishes against the view
        // this call just set, and its completion picks up whatever waits.
        waiting.current = waiting.current ?? { design: null };
        return;
      }
      void render(null);
    },
    [render],
  );

  const request = useCallback(
    (design: ReportDesignPatch): void => {
      // Pending from the keystroke, not from the request: the preview on screen is
      // already stale the moment the design changes, and that is what the indicator
      // has to say.
      setPending(true);
      fresh.current = { sheet: false, pdf: false };
      waiting.current = { design };
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        timer.current = null;
        // A render is already running; its completion picks up whatever is waiting.
        if (inFlight.current) return;
        const next = waiting.current;
        waiting.current = null;
        if (next !== null) void render(next.design);
      }, delayMs);
    },
    [delayMs, render],
  );

  return { html, url, view, setView, pending, error, request };
}
