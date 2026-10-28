import type { ReactNode } from "react";

/** The document's marks — the closed set a heading or a callout may wear.
 *
 *  **Mirrors `services/report_icons.py`, and cannot drift from it.**
 *  `tests/test_report_icons.py` reads this file, parses the table below and
 *  compares every path string. A comment claiming two constants agree does not
 *  make them agree; that test does.
 *
 *  Distinct from `blockIcons.tsx`, which is editor chrome: those name a block's
 *  *kind* for someone scanning the tree and never reach the page. These are the
 *  reader's own choice and are printed.
 */
const PATHS: Record<string, string> = {
  doc: "M4 2h6l2 2v10H4z M10 2v2h2",
  db: "M3 4c0-1.1 2.2-2 5-2s5 .9 5 2-2.2 2-5 2-5-.9-5-2z M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4",
  chart: "M3 13V7 M6.5 13V3 M10 13V9 M13.5 13V5",
  trend: "M2 12l4-4 3 3 5-6 M14 5v3 M14 5h-3",
  warn: "M8 2l6 11H2z M8 6v4 M8 11.5v.5",
  check: "M3 8.5l3.5 3.5L13 5",
  clock: "M8 2a6 6 0 100 12A6 6 0 008 2z M8 4.5V8l2.5 1.5",
  flag: "M4 14V2 M4 3h8l-2 2.5L12 8H4",
  scales: "M8 3v10 M4 13h8 M3 6h10 M3 6l-1.5 3.5h3z M13 6l1.5 3.5h-3z",
  shield: "M8 2l5 2v4c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4z",
  bank: "M2 6l6-3.5L14 6 M3 6v6 M6.5 6v6 M9.5 6v6 M13 6v6 M2 13h12",
};

/** Every name the picker offers, "no mark" first — it is the default and the way
 *  back, so it belongs where the eye lands rather than at the end of the grid. */
export const ICON_NAMES: readonly string[] = ["none", ...Object.keys(PATHS)];

/** The glyph for a mark, or nothing for `none` and for a name this build does
 *  not know — an older editor meeting a payload from a newer one draws an
 *  unadorned heading rather than a broken-image box. */
export function documentIcon(name: string): ReactNode {
  const d = PATHS[name];
  if (d === undefined) return null;
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true">
      <path d={d} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
