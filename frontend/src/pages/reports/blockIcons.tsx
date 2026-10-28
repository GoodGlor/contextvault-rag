import type { ReactNode } from "react";

/** One line-art glyph per block kind, for the tree's rows.
 *
 *  **Editor chrome only.** The *document's* marks are step 6d (#158) and are a
 *  different, closed set living on the block itself. These carry no meaning the
 *  row's label does not already state — they exist so a list of twenty rows can
 *  be scanned rather than read.
 *
 *  Drawn outlines rather than emoji, for the same reason the document's will be:
 *  an emoji is a different typeface at a different weight in every renderer.
 */
const PATHS: Record<string, string> = {
  cover: "M3 2h8l3 3v9H3z M11 2v3h3",
  toc: "M2 4h12 M2 8h12 M2 12h8",
  heading: "M3 3v10 M3 8h7 M10 3v10",
  prose: "M2 4h12 M2 7h12 M2 10h9 M2 13h6",
  viz: "M2 14V8 M6 14V4 M10 14V10 M14 14V6",
  kpi: "M2 12l4-5 3 3 5-6",
  stats: "M2 3h12v10H2z M2 7h12 M6 7v6",
  sources: "M4 2h8v12l-4-3-4 3z",
  callout: "M2 3h12v8H7l-3 3v-3H2z",
  image: "M2 3h12v10H2z M5 7l3 3 2-2 4 4",
  pagebreak: "M2 8h3 M7 8h2 M11 8h3 M8 2v3 M8 11v3",
};

/** The glyph for a kind, or nothing for one this set does not know.
 *
 *  Returning null rather than a placeholder is deliberate: a new block kind
 *  should render an unadorned row, not a "missing icon" mark that reads as a
 *  defect in the report.
 */
export function blockIcon(kind: string): ReactNode {
  const d = PATHS[kind];
  if (d === undefined) return null;
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true">
      <path d={d} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
