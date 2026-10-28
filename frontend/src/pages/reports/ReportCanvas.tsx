import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

export interface ReportCanvasProps {
  /** The document from `GET …/design/preview`, or null before the first render. */
  html: string | null;
  selected: number | null;
  onSelect: (index: number | null) => void;
  /** The block's display name, from the caller's i18n — this component holds no
   *  copy of its own, the rule shared presentational components follow here. */
  label: (index: number) => string;
  onMove: (from: number, to: number) => void;
  onToggleHidden: (index: number) => void;
  onDelete: (index: number) => void;
  /** A block's words, rewritten on the sheet. The same `text` override 5e
   *  defined, through the same debounced PUT — this card adds a place to type
   *  it, not a thing to store. */
  onEditText: (index: number, text: string) => void;
  blockCount: number;
  /** How large the sheet is drawn, and how to change it (#168, lifted by #172).
   *
   *  **The value is the caller's; the measuring stays here.** #168 kept the whole
   *  thing local, reasoning that the scale is meaningless in `pdf` view and that
   *  fit-to-width needs a width only this component has measured. The second
   *  half is still true and is why `fit` lives below rather than on the page.
   *  The first half was the defect: the centre column renders this component
   *  behind a ternary on the view, so "meaningless in `pdf` view" meant
   *  *unmounted*, and the reader's scale was discarded on a switch they expected
   *  to be a detour. A number that must outlive the element belongs to whatever
   *  outlives it. */
  zoom: number;
  onZoom: (zoom: number) => void;
  /** No reader-chosen zoom yet — fit as soon as a width exists.
   *
   *  The flag belongs to the **page**, not here: the centre column renders this
   *  component behind a ternary on the view, so a flag kept locally would reset
   *  on every Аркуш ⇄ PDF switch and re-fit over the reader's own zoom — the
   *  defect #172 fixed, reintroduced through a different door. */
  autoFit: boolean;
}

/** Where a floating control sits, in the canvas's own coordinates. */
interface Anchor {
  top: number;
  left: number;
  width: number;
}

const SELECTED_CLASS = "cv-selected";

/** A4's width at 96 dpi — 210 mm, the width `_preview_css` gives `.cv-sheet`.
 *  Fit-to-width divides the measured canvas by it. */
const SHEET_PX = 794;

/** The scales offered, smallest first. Discrete rather than continuous: a slider
 *  invites hunting for a value, and every step here is one a reader can name. */
const STEPS = [0.5, 0.75, 1, 1.25, 1.5] as const;

const MIN = STEPS[0];
const MAX = STEPS[STEPS.length - 1];

/** Round to the tenth of a percent, so a fitted scale writes `0.7506` and not
 *  seventeen digits of float into the DOM. */
function tidy(scale: number): number {
  return Math.round(scale * 1000) / 1000;
}

/** The report itself, as a sheet you can point at.
 *
 *  **The document is mounted into a shadow root, and that is load-bearing.**
 *  `stylesheet()` styles bare element selectors — `html`, `body`, `p`, `table`,
 *  `h2`, `section`. Writing this document into the application's own DOM would
 *  not be a preview, it would be a global restyle of every page the router can
 *  reach. A shadow root keeps the card's decision — same document, same DOM,
 *  directly scriptable, no iframe and no `contentWindow` — and adds the style
 *  boundary that makes it survivable.
 *
 *  Its cost is that `html { … }` and `body { … }` match nothing inside a shadow
 *  tree; the document's own `.cv-sheet` wrapper carries those declarations for
 *  exactly that reason (`report_html._preview_css`).
 *
 *  Hover and click are **delegated** to the root rather than bound per block: the
 *  document is replaced wholesale on every edit, and per-element listeners would
 *  have to be re-attached each time — which is the kind of bookkeeping that ends
 *  up leaking one listener per keystroke.
 */
export function ReportCanvas({
  html,
  selected,
  onSelect,
  label,
  onMove,
  onToggleHidden,
  onDelete,
  onEditText,
  blockCount,
  zoom,
  onZoom,
  autoFit,
}: ReportCanvasProps): ReactNode {
  const { t } = useTranslation();
  const shadow = useRef<ShadowRoot | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  // Bumped every time a document is written into the root. The selection ring is
  // written *onto elements*, so it has to be re-applied after every redraw —
  // including one that was held back, which no prop change accompanies.
  const [drawn, setDrawn] = useState(0);
  const host = useRef<HTMLDivElement | null>(null);
  // A document that arrived while the reader was typing, waiting for them to
  // look away. See `write` below for why it waits.
  const held = useRef<string | null>(null);
  const editing = useRef(false);
  const flush = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** Attach the root once, in a ref callback.
   *
   *  Not in an effect: StrictMode double-invokes effects, and a second
   *  `attachShadow` on the same host throws `NotSupportedError`. Keeping the root
   *  in a ref and reusing it is what makes a remount harmless.
   */
  const mount = useCallback((element: HTMLDivElement | null): void => {
    host.current = element;
    if (element === null || shadow.current !== null) return;
    shadow.current = element.attachShadow({ mode: "open" });
  }, []);

  /** Step to the next offered scale in `direction`, or stay put at the end. */
  const step = useCallback(
    (direction: 1 | -1): void => {
      const next =
        direction === 1
          ? STEPS.find((s) => s > zoom + 0.001)
          : [...STEPS].reverse().find((s) => s < zoom - 0.001);
      onZoom(next ?? zoom);
    },
    [zoom, onZoom],
  );

  /** As wide as the room allows — measured, never computed.
   *
   *  #167 promised 796 px of canvas on paper and delivered 780, because
   *  `.app-main`'s gutters are not in any stylesheet you would think to read.
   *  The one number this control depends on comes out of the element.
   */
  const fit = useCallback((): void => {
    const width = host.current?.clientWidth ?? 0;
    onZoom(tidy(Math.min(MAX, Math.max(MIN, width / SHEET_PX))));
  }, [onZoom]);

  /** Open at fit-to-width, once, as soon as the host has a width.
   *
   *  **Guarded on a real measurement, not run on mount.** `fit` reads
   *  `clientWidth` and clamps, so an unmeasured host (0) would clamp *up* to
   *  MIN and open every report at 50 %. `drawn` is in the deps rather than
   *  `html`: a document can be written with no prop change, and the write is
   *  what gives the sheet its size.
   */
  useEffect(() => {
    if (!autoFit) return;
    if ((host.current?.clientWidth ?? 0) === 0) return;
    fit();
  }, [autoFit, drawn, fit]);

  /** Write a document into the root.
   *
   *  `innerHTML`, never append: every preview publishes a *whole* document, and
   *  appending would grow the canvas by one more report per edit.
   */
  const write = useCallback((document: string): void => {
    if (shadow.current === null) return;
    shadow.current.innerHTML = document;
    // The mounted document is new, so any remembered position points at an
    // element that no longer exists.
    setAnchor(null);
    setDrawn((n) => n + 1);
  }, []);

  /** Draw the new document — unless the reader is inside the old one.
   *
   *  **This is 6c's answer to a caret that cannot survive a redraw.** An edit
   *  costs a ~300 ms debounce and a ~300 ms render, and the document comes back
   *  whole; applying it under a caret destroys the very element being typed
   *  into. Restoring the caret by offset afterwards was rejected: the server
   *  does not return the same markup — a cover edit rewrites the running header,
   *  a heading edit rewrites the contents entry — so an offset would be restored
   *  into a document that moved underneath it.
   *
   *  Nothing is lost by waiting. What the reader is typing is already on screen;
   *  the browser painted it. What changes *elsewhere* appears when they look
   *  away, which is when they can see it.
   */
  useEffect(() => {
    if (shadow.current === null) return;
    if (editing.current) {
      held.current = html ?? "";
      return;
    }
    write(html ?? "");
  }, [html, write]);

  /** Which block an event happened inside, or null for the sheet itself.
   *
   *  **`composedPath()[0]`, never `event.target`.** A shadow root retargets an
   *  event on its way out: by the time it reaches the host, `target` *is* the
   *  host, so `closest("[data-block-index]")` would find nothing and every click
   *  would read as "the reader clicked the background". The composed path is the
   *  only place the real element survives.
   *
   *  `closest` rather than the element itself, so clicking a table cell selects
   *  the table — a reader points at content, not at the block's outermost tag.
   */
  const indexOf = useCallback((event: React.SyntheticEvent): number | null => {
    const origin = event.nativeEvent.composedPath()[0];
    if (!(origin instanceof Element)) return null;
    const block = origin.closest("[data-block-index]");
    if (block === null) return null;
    const raw = block.getAttribute("data-block-index");
    return raw === null ? null : Number(raw);
  }, []);

  /** The editable run an event happened inside, or null.
   *
   *  `data-block-text` marks the run rather than the block, and the two are not
   *  always the same element: a cover's editable text is its `<h1>`, nested
   *  inside the block. Reading the block's own index would work there by luck.
   *
   *  `composedPath()[0]` for `indexOf`'s reason, and its `parentElement` when
   *  the path head is a text node — which is what an `input` event in a
   *  `contenteditable` can hand back.
   */
  const editableOf = useCallback((event: React.SyntheticEvent): Element | null => {
    const origin = event.nativeEvent.composedPath()[0];
    const element =
      origin instanceof Element ? origin : origin instanceof Node ? origin.parentElement : null;
    return element?.closest("[data-block-text]") ?? null;
  }, []);

  /** Focus entered or left the document; on the way out, draw anything held.
   *
   *  The flush is deferred by a turn because focus *moving between* two editable
   *  runs fires `focusout` before `focusin`. Drawing at once would replace the
   *  element the reader was about to type into — the same defect the hold exists
   *  to prevent, arriving through the door left open for the fix.
   */
  const onFocusIn = useCallback(
    (event: React.FocusEvent): void => {
      editing.current = editableOf(event) !== null;
    },
    [editableOf],
  );

  const onFocusOut = useCallback((): void => {
    editing.current = false;
    flush.current = setTimeout(() => {
      flush.current = null;
      if (editing.current || held.current === null) return;
      const next = held.current;
      held.current = null;
      write(next);
    }, 0);
  }, [write]);

  // A blur on the way out of the editor would otherwise leave a timer that wakes
  // up to write into a shadow root nobody is looking at.
  useEffect(
    () => () => {
      if (flush.current !== null) clearTimeout(flush.current);
    },
    [],
  );

  /** Ring the selected block, put the toolbar on it, and scroll it into view. */
  useEffect(() => {
    const root = shadow.current;
    if (root === null) return;
    root
      .querySelectorAll(`.${SELECTED_CLASS}`)
      .forEach((el) => el.classList.remove(SELECTED_CLASS));
    if (selected === null) {
      setAnchor(null);
      return;
    }
    const element = root.querySelector(`[data-block-index="${selected}"]`);
    if (element === null) {
      // A hidden block renders nothing, so it has no element to ring — and that
      // is not an error: the tree still shows it selected.
      setAnchor(null);
      return;
    }
    element.classList.add(SELECTED_CLASS);
    // `center`, not `nearest` (#194). `nearest` is the minimal-effort option: a
    // block already partly on screen moved *nothing at all*, and one off screen
    // was clipped to the edge — so selecting a block often looked like it had
    // done nothing to the page. Centring makes the sheet follow the selection
    // every time.
    //
    // Deliberately NOT `behavior: "smooth"`. The anchor below is a rect read
    // immediately after this call, and nothing here sets `scroll-behavior:
    // smooth`, so the scroll is instant and the rect is the block's final
    // position. Animating it would take the rect mid-flight and strand the
    // floating toolbar where the block used to be — the same defect #168
    // records for `zoom`, reintroduced by the back door.
    element.scrollIntoView({ block: "center" });
    const box = element.getBoundingClientRect();
    setAnchor({ top: box.top, left: box.left, width: box.width });
    // `drawn` rather than `html`: a held document is drawn with no prop change,
    // and a ring that only followed `html` would be lost on exactly that redraw.
    //
    // **`zoom` belongs here too, and its absence was the defect #168 describes.**
    // The anchor is a rect frozen at the moment the selection changed; scaling
    // the sheet moves the block under it while nothing this effect watches
    // changes, so the toolbar stays where the block used to be — 51px out at
    // 75%, measured. Re-selecting does not fix it either: selecting the block
    // that is already selected sets the same value and re-runs nothing.
  }, [selected, drawn, zoom]);

  function act(run: () => void): (event: React.MouseEvent) => void {
    return (event) => {
      // The toolbar sits over the sheet; a click on it must not also be read as
      // a click on the block underneath.
      event.stopPropagation();
      run();
    };
  }

  return (
    <div className="cv-canvas">
      <div className="cv-zoom" role="group" aria-label={t("reports.workbench.zoomLabel")}>
        <button
          type="button"
          disabled={zoom <= MIN}
          title={t("reports.workbench.zoomOut")}
          aria-label={t("reports.workbench.zoomOut")}
          onClick={() => step(-1)}
        >
          −
        </button>
        {/* A readout, not a control: the buttons either side are the control, and
            a third way to set the same value is a third thing to keep in step. */}
        <output data-testid="canvas-zoom">{Math.round(zoom * 100)}%</output>
        <button
          type="button"
          disabled={zoom >= MAX}
          title={t("reports.workbench.zoomIn")}
          aria-label={t("reports.workbench.zoomIn")}
          onClick={() => step(1)}
        >
          +
        </button>
        <button type="button" onClick={fit}>
          {t("reports.workbench.zoomFit")}
        </button>
      </div>

      <div
        className="cv-host"
        data-testid="canvas-host"
        ref={mount}
        // The scale, handed across the shadow boundary. Custom properties
        // inherit into a shadow tree, which is what lets `_preview_css` read it
        // on `.cv-sheet` — the one element that must carry the `zoom`, because
        // an ancestor of the floating toolbar carrying it would rescale the
        // coordinate space the toolbar's `position: fixed` offsets resolve in
        // (the measured cause of #167's cut, written up on #168).
        style={{ "--cv-zoom": String(zoom) } as React.CSSProperties}
        onClick={(event) => onSelect(indexOf(event))}
        onMouseOver={(event) => setHovered(indexOf(event))}
        onMouseLeave={() => setHovered(null)}
        onFocus={onFocusIn}
        onBlur={onFocusOut}
        onInput={(event) => {
          const run = editableOf(event);
          const index = run?.getAttribute("data-block-text");
          if (run === null || index === null || index === undefined) return;
          onEditText(Number(index), run.textContent ?? "");
        }}
      />

      {html === null && <p className="wb-hint">{t("reports.workbench.previewHint")}</p>}

      {hovered !== null && hovered !== selected && (
        <span className="cv-tag" data-testid="canvas-tag">
          {label(hovered)}
        </span>
      )}

      {selected !== null && anchor !== null && (
        <div
          className="cv-toolbar"
          data-testid="canvas-toolbar"
          style={{ top: anchor.top, left: anchor.left + anchor.width }}
        >
          <button
            type="button"
            disabled={selected === 0}
            title={t("reports.workbench.canvasUp")}
            onClick={act(() => onMove(selected, selected - 1))}
          >
            ↑
          </button>
          <button
            type="button"
            disabled={selected >= blockCount - 1}
            title={t("reports.workbench.canvasDown")}
            onClick={act(() => onMove(selected, selected + 1))}
          >
            ↓
          </button>
          <button
            type="button"
            title={t("reports.workbench.canvasHide")}
            onClick={act(() => onToggleHidden(selected))}
          >
            ◎
          </button>
          <button
            type="button"
            title={t("reports.workbench.canvasDelete")}
            onClick={act(() => onDelete(selected))}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}
