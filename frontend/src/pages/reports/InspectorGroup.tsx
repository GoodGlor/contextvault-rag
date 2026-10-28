import { useCallback, useEffect, useRef } from "react";
import type { ReactNode } from "react";

const STORAGE_PREFIX = "contextvault.inspector.";

/** Read a group's remembered open state.
 *
 *  `localStorage` may be unavailable — a private-mode browser, a blocked origin —
 *  and a panel that throws there is worse than one that forgets. */
function remembered(id: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + id);
    return raw === null ? fallback : raw === "1";
  } catch {
    return fallback;
  }
}

export interface InspectorGroupProps {
  id: string;
  title: string;
  defaultOpen?: boolean;
  /** Change this to reveal the group — it opens if it was closed.
   *
   *  A *when*, not a *whether*: the value is never read, only compared with the
   *  last one, which is what keeps `open` out of React's hands (see the note on
   *  the component). The workbench passes the selected block's index, so
   *  choosing a block puts its properties on screen without a second click; it
   *  re-reveals on every change, including a re-selection of the same index
   *  after another group was opened over it.
   *
   *  A reveal is deliberately **not** remembered — see `mine` below. */
  revealKey?: number | string | null;
  children: ReactNode;
}

/** One collapsible group of the workbench's inspector.
 *
 *  Open state is remembered per id: the inspector is five groups tall, and a
 *  reader who closed Типографіка to reach Брендкіт should not find it open again
 *  on the next visit.
 *
 *  **`open` is set on the element, never passed as a React prop, and that is
 *  load-bearing.** The `toggle` event fires *asynchronously*. With React managing
 *  `open`, a click closed the DOM, a re-render re-applied React's stale `open`
 *  before the queued event ran, and the handler then stored the state that had
 *  just been undone — the group reopened on reload with `"1"` in storage. Passing
 *  a constant `open` was not enough either; only taking React out of the loop
 *  entirely was. **An e2e run caught this and no unit test could**, because jsdom
 *  dispatches `toggle` synchronously.
 *
 *  A native `<details>` rather than a hand-rolled disclosure: it is keyboard
 *  operable and correctly announced for free. Note that a *closed* one genuinely
 *  hides its body in a browser — jsdom does not — so anything a test needs to
 *  click must have its group opened first.
 */
export function InspectorGroup({
  id,
  title,
  defaultOpen = false,
  revealKey,
  children,
}: InspectorGroupProps): ReactNode {
  const element = useRef<HTMLDetailsElement | null>(null);
  // True while a toggle *we* caused is still in flight. Setting `.open` fires
  // `toggle` exactly like a click does, and the handler below persists on every
  // one — so without this, revealing a group the reader had closed would rewrite
  // their choice to "1" and it would come back open forever after. An automatic
  // open is not a preference. Cleared by the toggle it was raised for, so a real
  // click afterwards is remembered normally.
  const mine = useRef(false);
  // Set once, on the element itself. `open` is deliberately NOT a React prop —
  // see the note above. Both dependencies are stable, so this callback's identity
  // never changes and React never re-runs it.
  const mount = useCallback(
    (node: HTMLDetailsElement | null): void => {
      element.current = node;
      if (node !== null) node.open = remembered(id, defaultOpen);
    },
    [id, defaultOpen],
  );

  // Imperative, for the same reason `mount` is: React must not own `open`. This
  // effect only *acts* on a change of `revealKey`; it never renders from it.
  useEffect(() => {
    if (revealKey === undefined || revealKey === null) return;
    const node = element.current;
    // Already open: nothing to do, and no toggle would fire to un-flag anyway.
    if (node === null || node.open) return;
    mine.current = true;
    node.open = true;
  }, [revealKey]);

  return (
    <details
      ref={mount}
      className="inspector-group"
      data-testid={`group-${id}`}
      onToggle={(event) => {
        // Ours, not the reader's: reveal it, but do not learn from it.
        if (mine.current) {
          mine.current = false;
          return;
        }
        try {
          localStorage.setItem(STORAGE_PREFIX + id, event.currentTarget.open ? "1" : "0");
        } catch {
          /* localStorage may be unavailable; the group just will not be remembered */
        }
      }}
    >
      <summary>{title}</summary>
      <div className="inspector-group-body">{children}</div>
    </details>
  );
}
