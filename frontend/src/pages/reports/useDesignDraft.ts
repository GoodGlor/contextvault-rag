import { useCallback, useEffect, useRef, useState } from "react";
import { getReportDesign, setReportDesign } from "../../api/reports";
import type { ReportDesignPatch } from "../../api/reports";
import type { StyleGroups, StyleUpdate } from "./styleGroups";

export interface DesignDraft {
  /** The slug on the row, or null when none is. */
  template: string | null;
  /** The three inspector groups, or null until the seed lands. */
  style: StyleGroups | null;
  error: string | null;
  choose: (template: string) => void;
  restyle: (change: StyleUpdate) => void;
  /** Send whatever is still waiting behind the debounce, right now, and resolve
   *  once it has been written. Generation is about to start from the report's
   *  stored state; a debounce window left ticking would let `/approve` win a
   *  race against a choice the reader already made, and the very first artifact
   *  would come out wearing the *old* design. With nothing pending it sends
   *  nothing, but still resolves only once any write already on the wire has
   *  landed — that is the ordinary case, since a reader who pauses longer than
   *  the debounce has already had their patch sent. */
  flush: () => Promise<void>;
}

/** The design a reader picks *before* the report is generated.
 *
 *  Deliberately not `useReportPreview`. That hook renders the sheet after every
 *  save, and there is nothing to render here: the report has no stored payload
 *  yet, and `GET …/design/preview` answers 409 for exactly that reason. What is
 *  shared is the endpoint, not the machinery.
 *
 *  Saves are debounced and coalesced into one patch. The inspector's sliders emit
 *  a change per pixel, and a PUT per pixel would be both a stampede and a race:
 *  the response order of overlapping writes is not the order they were made in.
 *  Coalescing keeps the *last* value of each group, which is what the reader sees.
 *
 *  **Never carries `blocks`.** `setReportDesign` sends only the keys present, and
 *  a block list is refused pre-generation anyway (422) since it would reference
 *  sections that do not exist. Not sending one keeps that reason local.
 */
export function useDesignDraft({
  repositoryId,
  reportId,
  errorText,
  delayMs = 300,
}: {
  repositoryId: string;
  reportId: string;
  /** What to say when a save fails. Passed in: the hook owns no copy. */
  errorText: string;
  delayMs?: number;
}): DesignDraft {
  const [template, setTemplate] = useState<string | null>(null);
  const [style, setStyle] = useState<StyleGroups | null>(null);
  const [error, setError] = useState<string | null>(null);

  // What is waiting to be written, merged across a burst. A ref, not state: the
  // timer reads it when it fires, and re-rendering on every keystroke of a
  // slider is precisely the cost the debounce exists to avoid.
  const pendingRef = useRef<ReportDesignPatch>({});
  // The write currently on the wire, so `flush` can wait for it. Starts already
  // settled: with nothing ever sent there is nothing to wait for.
  const inFlightRef = useRef<Promise<void>>(Promise.resolve());
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The latest groups, readable synchronously. `style` state is what renders;
  // this is what a *functional* restyle resolves against, because a change that
  // lands after an upload cannot see the state of a render that has not happened
  // yet. Starts empty rather than null so a functional caller always has a base:
  // the groups the seed has not filled in are absent, not unknown.
  const styleRef = useRef<StyleGroups>({ typography: {}, page: {}, brand: {} });
  const live = useRef(true);
  // Only the newest save may set or clear `error`. Debounce serialises requests
  // that arrive *before* a timer fires, but nothing serialises against one
  // already in flight: if the next timer fires while a PUT is still outstanding,
  // two `setReportDesign` calls are concurrent and their responses can resolve
  // out of order. Same idea as `useReportPreview`'s `generation` ref, sized down
  // to what this hook needs — one save at a time, not a render queue.
  const generation = useRef(0);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void getReportDesign(repositoryId, reportId)
      .then((loaded) => {
        if (cancelled) return;
        setTemplate(loaded.template);
        // `?? {}` is read tolerance: a cached SPA meeting a response without
        // these groups would throw inside the inspector and unmount the step.
        const seeded = {
          typography: loaded.typography ?? {},
          page: loaded.page ?? {},
          brand: loaded.brand ?? {},
        };
        styleRef.current = seeded;
        setStyle(seeded);
      })
      .catch(() => {
        // A seed that fails is not a save that fails. The reader can still
        // choose; the step opens on the template defaults rather than nothing.
        if (cancelled) return;
        const empty = { typography: {}, page: {}, brand: {} };
        styleRef.current = empty;
        setStyle(empty);
      });
    return () => {
      cancelled = true;
    };
  }, [repositoryId, reportId]);

  /** Send one patch now and apply the same stale-guarded error handling the
   *  debounced path uses, regardless of who triggered it. */
  const fire = useCallback(
    (body: ReportDesignPatch): Promise<void> => {
      const mine = ++generation.current;
      const sent = setReportDesign(repositoryId, reportId, body)
        .then(() => {
          // A stale success must not paper over a newer save that already
          // reported failure — that would silently mask a real error.
          if (live.current && mine === generation.current) setError(null);
        })
        .catch(() => {
          // The choice stays on screen. It is stored nowhere yet, and telling
          // the reader their pick vanished when it is still selected would be
          // a second, false, thing to understand.
          //
          // Also stale-guarded: a slow failure must not flip `error` back on
          // after a newer save already succeeded and cleared it.
          if (live.current && mine === generation.current) setError(errorText);
        });
      // Remembered so `flush` can wait for a write that is already on the wire.
      // Neither branch above rethrows, so this promise never rejects and no
      // caller needs a catch of its own.
      inFlightRef.current = sent;
      return sent;
    },
    [repositoryId, reportId, errorText],
  );

  const schedule = useCallback(
    (patch: ReportDesignPatch) => {
      pendingRef.current = { ...pendingRef.current, ...patch };
      if (timerRef.current !== null) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        const body = pendingRef.current;
        pendingRef.current = {};
        void fire(body);
      }, delayMs);
    },
    [fire, delayMs],
  );

  const flush = useCallback((): Promise<void> => {
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = null;
    const body = pendingRef.current;
    pendingRef.current = {};
    // Waiting on the write already in flight is the *common* case, not the edge
    // one: a reader who takes longer than the debounce between picking a
    // template and pressing Generate has no pending patch left — the timer
    // already fired — and returning immediately would post /approve alongside a
    // PUT still on the wire. Closing only the sub-300ms window would leave the
    // ordinary path racing and look, from the tests, like the race was fixed.
    const settled = inFlightRef.current;
    if (Object.keys(body).length === 0) return settled;
    return settled.then(() => fire(body));
  }, [fire]);

  const choose = useCallback(
    (next: string) => {
      setTemplate(next);
      schedule({ template: next });
    },
    [schedule],
  );

  const restyle = useCallback(
    (change: StyleUpdate) => {
      // Resolved against the ref, not against the render this callback closed
      // over: an upload's `.then` runs long after that render, and the accent
      // the reader picked meanwhile lives only in the ref by then.
      const resolved = typeof change === "function" ? change(styleRef.current) : change;
      styleRef.current = { ...styleRef.current, ...resolved };
      // Still `null`-preserving: the step renders no inspector until the seed
      // lands, and turning `null` into groups here would put one on screen.
      setStyle((current) => (current === null ? current : { ...current, ...resolved }));
      schedule(resolved);
    },
    [schedule],
  );

  return { template, style, error, choose, restyle, flush };
}
