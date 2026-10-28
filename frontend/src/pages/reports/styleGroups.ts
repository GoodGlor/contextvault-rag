import type { BrandTokens, PageTokens, TypographyTokens } from "../../api/reports";

/** The three inspector groups a reader can set, held together.
 *
 *  Shared rather than declared per surface: the design workbench and the
 *  pre-generation design step hold the same three groups and hand them to the
 *  same `StyleInspector`. Two declarations would be two things to keep in step,
 *  and the one that fell behind would still typecheck.
 */
export interface StyleGroups {
  typography: TypographyTokens;
  page: PageTokens;
  brand: BrandTokens;
}

/** A partial update: only the groups present are replaced, each whole. */
export type StyleChange = {
  typography?: TypographyTokens;
  page?: PageTokens;
  brand?: BrandTokens;
};

/** A change, or a function that computes one from the state at the moment it is
 *  applied.
 *
 *  The functional form exists for changes that resolve **later** than they are
 *  requested — a logo upload is the only one today. Reading `brand` when the
 *  upload starts and writing `{ ...brand, logo_asset_id }` when it lands
 *  discards anything the reader changed in between, because a group travels
 *  whole: the stale snapshot replaces the live accent rather than merging with
 *  it, on screen and in the `PUT` that follows.
 *
 *  Synchronous callers keep passing a plain object; for them "now" and "when it
 *  is applied" are the same instant, and a function would be ceremony.
 */
export type StyleUpdate = StyleChange | ((current: StyleGroups) => StyleChange);
