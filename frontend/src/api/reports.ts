import { api } from "./client";

// Mirrors ReportResponse / ReportAdminResponse in src/contextvault/api/reports.py.
// Database-backed reports: request a report in natural language against a
// repository's connected reporting database; generation (NL→SQL→PDF) runs in the
// background, so the caller polls for the terminal status.

// Mirrors ReportStatus in src/contextvault/models/enums.py. A report now pauses at
// `awaiting_approval` with its plan on the row, waiting for its owner to edit and
// approve it — generation starts only after that.
export type ReportStatus =
  "planning" | "awaiting_approval" | "pending" | "processing" | "done" | "failed";

/** Still working, so the UI keeps polling. `awaiting_approval` is excluded on
 *  purpose: nothing is running and no amount of polling will change it — only
 *  the user will. */
export function isGenerating(status: ReportStatus): boolean {
  return status === "planning" || status === "pending" || status === "processing";
}

/** The one status that needs a human. */
export function isAwaitingApproval(status: ReportStatus): boolean {
  return status === "awaiting_approval";
}

/** One planned section, routed to the source that can answer it.
 *
 *  `""` means the plan has not chosen one yet — only a *draft* carries it, on a
 *  section the reader just added; the next re-plan fills it. A plan returned by
 *  the server never has a blank source. */
export interface PlanSection {
  title: string;
  source: "database" | "documents" | "";
  /** Which database this section reads (#203) — connection ids, as the plan
   *  carries them. A repository owns several since #201, so `source: "database"`
   *  is a *kind* of source and no longer says which one.
   *
   *  A list because a section may read several and merge their rows (#204): the
   *  server accepts up to three on a `database` section and none on a
   *  `documents` one. Empty on a `database` section is a blank the next re-plan
   *  fills, exactly like a blank `source`. */
  databases: string[];
  /** The output contract (#204): the columns every one of this section's queries
   *  returns, so rows from different databases can share one table. Empty unless
   *  the section reads more than one database.
   *
   *  The editor never sets it — the plan chooses, like `source` and `databases`
   *  (#186) — but it must be carried through an edit unchanged, or a merged
   *  section would lose the shape its queries were written against. */
  columns: string[];
  sub_question: string;
}

/** A database a plan can name, as the plan editor shows it. Mirrors
 *  ReportDatabaseResponse — a name, never connection detail. */
export interface ReportDatabase {
  id: string;
  name: string;
  description: string;
}

/** The plan the user reviews. Sent back verbatim on replan/approve; blanks are a
 *  request for the model to fill that field and nothing else. */
export interface ReportPlan {
  title: string;
  language: string;
  sections: PlanSection[];
}

export interface Report {
  id: string;
  repository_id: string;
  prompt: string;
  status: ReportStatus;
  error: string | null;
  created_at: string;
  has_pdf: boolean;
  has_pptx: boolean;
  schedule_id: string | null;
  /** The plan under review, or the approved one. Null for reports created before
   *  the review loop existed, and for frozen-schedule runs. */
  plan: ReportPlan | null;
  /** How many LLM planning rounds this report cost. A re-plan with nothing left
   *  blank makes no provider call and does not increment it. */
  plan_rounds: number;
  /** Whether `rerender` can rebuild this report from stored section data. False
   *  for reports generated before `sections_data` existed **and** for reports
   *  whose payload exceeded the 4 MiB budget — the wire cannot tell those apart,
   *  so no UI may claim which one it is. */
  can_rerender: boolean;
  /** The template slug snapshotted on the row at generation. Null for reports
   *  generated before the choice existed: it means *no choice recorded*, not
   *  "Кварта was chosen" — the row merely resolves to the default at render
   *  time. */
  template: string | null;
  // Only present on the admin `?all=true` view — the audit trail of the exact
  // query that ran.
  generated_sql?: string | null;
}

/** Request a report: creates a PENDING row; NL→SQL→PDF generation runs as a
 *  background task the caller polls for. 400 if the repository has no connected
 *  reporting database yet. */
export function createReport(repositoryId: string, prompt: string): Promise<Report> {
  return api.post<Report>(`/repositories/${repositoryId}/reports`, { prompt });
}

/** Send an edited plan back for another round. Every value the caller supplies is
 *  kept verbatim by the server; only blanks are filled by the model. 409 unless
 *  the report is awaiting approval. */
export function replanReport(
  repositoryId: string,
  reportId: string,
  plan: ReportPlan,
): Promise<Report> {
  return api.post<Report>(`/repositories/${repositoryId}/reports/${reportId}/replan`, plan);
}

/** Approve a plan and start generating it. The body is the plan as it stands on
 *  screen, so an edit made after the last round is not lost. 400 if any field is
 *  still blank. */
export function approveReport(
  repositoryId: string,
  reportId: string,
  plan: ReportPlan,
): Promise<Report> {
  return api.post<Report>(`/repositories/${repositoryId}/reports/${reportId}/approve`, plan);
}

/** The databases a plan for this repository can name (#203).
 *
 *  Not `listDatabases` from `api/database.ts`: that one is the admin's editing
 *  view of a connection and is admin-only, while the plan editor is a reader's
 *  screen. This endpoint is guarded like the report routes and returns names. */
export function listReportDatabases(repositoryId: string): Promise<ReportDatabase[]> {
  return api.get<ReportDatabase[]>(`/repositories/${repositoryId}/report-databases`);
}

/** The caller's own reports for a repository, newest first. `all: true` is an
 *  admin-only escape hatch onto every user's reports (each row then also carries
 *  `generated_sql`). */
export function listReports(repositoryId: string, all = false): Promise<Report[]> {
  const qs = all ? "?all=true" : "";
  return api.get<Report[]>(`/repositories/${repositoryId}/reports${qs}`);
}

/** Download a finished report's PDF bytes; 404 unless the report is DONE with a
 *  stored artifact. Owner or admin only. */
/** One report.
 *
 *  The list endpoint is repository-wide; this is what a surface holding a single
 *  report id — the design route — reads instead of fetching and filtering the
 *  whole list. Owner or admin only, and a report belonging to another repository
 *  answers 404 rather than leaking its existence.
 */
export function getReport(repositoryId: string, reportId: string): Promise<Report> {
  return api.get<Report>(`/repositories/${repositoryId}/reports/${reportId}`);
}

export type ReportFormat = "pdf" | "pptx";

export function downloadReport(
  repositoryId: string,
  reportId: string,
  format: ReportFormat = "pdf",
): Promise<Blob> {
  return api.getBlob(`/repositories/${repositoryId}/reports/${reportId}/download?format=${format}`);
}

/** Delete a report; owner or admin only. */
export function deleteReport(repositoryId: string, reportId: string): Promise<void> {
  return api.del<void>(`/repositories/${repositoryId}/reports/${reportId}`);
}

// --- report schedules (Task 12) ----------------------------------------------
// Mirrors ScheduleResponse in src/contextvault/api/report_schedules.py. A schedule
// freezes an already-generated (DONE) report's validated SQL + chart spec so the
// nightly scheduler can re-run it verbatim with no further LLM call.

export interface Schedule {
  id: string;
  repository_id: string;
  prompt: string;
  run_at_time: string;
  enabled: boolean;
  last_run_at: string | null;
  last_error: string | null;
  created_at: string;
}

/** Freeze a DONE report (must be the caller's own, or the caller is admin, and
 *  must carry both generated SQL and a chart spec) into a nightly run at
 *  `runAtTime` ("HH:MM"). */
export function createSchedule(
  repositoryId: string,
  reportId: string,
  runAtTime: string,
): Promise<Schedule> {
  return api.post<Schedule>(`/repositories/${repositoryId}/report-schedules`, {
    report_id: reportId,
    run_at_time: runAtTime,
  });
}

/** The caller's own schedules for a repository. `all: true` is an admin-only
 *  escape hatch onto every user's schedules. */
export function listSchedules(repositoryId: string, all = false): Promise<Schedule[]> {
  const qs = all ? "?all=true" : "";
  return api.get<Schedule[]>(`/repositories/${repositoryId}/report-schedules${qs}`);
}

/** Partial update — only the fields present are applied. Owner or admin only. */
export function patchSchedule(
  scheduleId: string,
  body: { enabled?: boolean; run_at_time?: string },
): Promise<Schedule> {
  return api.patch<Schedule>(`/report-schedules/${scheduleId}`, body);
}

/** Delete a schedule; owner or admin only. */
export function deleteSchedule(scheduleId: string): Promise<void> {
  return api.del<void>(`/report-schedules/${scheduleId}`);
}

// --- report design (step 4) --------------------------------------------------
// Mirrors TemplateChoice in src/contextvault/api/reports.py. Facts only — no
// description field: `ReportStyle` holds no prose, and UI copy belongs in the
// locale files keyed by `key`.

export interface TemplateChoice {
  key: string;
  name: string;
  paper: string;
  ink: string;
  accent: string;
}

/** The shipped templates, **default first** — the endpoint promises that
 *  ordering, which is what lets the UI name the default without hardcoding a
 *  slug the backend owns. Any signed-in user; not repository-scoped. */
export function listReportTemplates(): Promise<TemplateChoice[]> {
  return api.get<TemplateChoice[]>("/report-templates");
}

// --- report blocks (step 5a's model, step 5d's editor) -----------------------
// Mirrors Block in src/contextvault/services/report_blocks.py. Every field beyond
// `kind` is optional because the kinds carry different payloads, and the wire
// shape omits defaults — so an unchanged round-trip writes identical JSON.

/** The kinds the editor's palette may add. Deliberately narrower than the
 *  backend's `KNOWN_KINDS`: `cover` is not addable (it is derived, once, at the
 *  top), `error` is produced by a failed section rather than chosen, and `toc` /
 *  `image` have no renderer until 5f. */
export const ADDABLE_BLOCK_KINDS = [
  "heading",
  "viz",
  "stats",
  "prose",
  "sources",
  "callout",
  "pagebreak",
  "toc",
  "image",
] as const;

export type AddableBlockKind = (typeof ADDABLE_BLOCK_KINDS)[number];

export interface ReportBlock {
  kind: string;
  /** Index into the report's sections. Absent on `cover`, `pagebreak` and
   *  `callout`, which read nothing from a section. */
  section?: number;
  /** `chart` or `table`, on a `viz` block only. */
  variant?: string;
  /** An override of the generated chart type, gated server-side against what the
   *  section's stored result can actually support. */
  chart_type?: string;
  /** This block's own words, overriding what the bound section says. Honoured on
   *  `cover` (the document title), `heading`, `prose` and `sources`; `callout`
   *  has carried its own text since 5a and has no original to fall back to.
   *  Absent restores the generated words — which is the point of storing an
   *  override rather than mutating the section. Rejected server-side on every
   *  other kind, because those draw from the stored result. */
  text?: string;
  /** Hidden blocks keep their place in the list and render in neither artifact.
   *  Absent means visible — the payload records departures from the default. */
  hidden?: boolean;
  /** `image` only: the uploaded asset this block prints. The backend refuses an
   *  id belonging to another repository, so this is an access-control field and
   *  not merely a reference. */
  asset_id?: string;
  /** `image` only. The reader's own words, not an override of generated ones —
   *  which is why it is `caption` rather than `text`. */
  caption?: string;
  /** `image` only: percent of the text column, 25–100. A percentage rather than
   *  an absolute size, so a margin change cannot break the layout. */
  width?: number;
  /** `image` only. */
  align?: "left" | "center" | "right";
  /** `heading` and `callout` only: a drawn mark from the closed set in
   *  `documentIcons.tsx`. **Document-only** — python-pptx cannot place inline
   *  SVG, so the deck ignores it. Absent means no mark; `"none"` is a reader
   *  who opened the picker and chose not to have one, which the payload can
   *  distinguish even though both draw nothing. */
  icon?: string;
}

/** What the editor may offer for one section, decided by the backend from the
 *  stored result rather than guessed here (parent design §3). */
export interface ReportSectionSummary {
  index: number;
  title: string;
  kind: "data" | "text" | "error";
  /** Sorted. The reason a pie is not offered for a time series is that it is
   *  absent from this list — not that the SPA knows to leave it out. */
  allowed_chart_types: string[];
  /** Whether the report as generated draws a figure here. Not the same as
   *  `allowed_chart_types` being non-empty: a section the model declined to
   *  chart draws nothing yet may support all three. */
  draws_chart: boolean;
  /** What a `prose` block on this section renders with no override, so the editor
   *  can show the reader what they are changing. Null for a data section. */
  prose: string | null;
  /** The citations line as the renderers join it. Null when the section resolved
   *  none. Pre-joined because an override replaces the whole line. */
  sources: string | null;
}

/** The reader's type choices. Every field absent means "as the template" — and
 *  that is load-bearing: the backend applies nothing when nothing is set, which
 *  is what keeps every report written before 5f rendering unchanged. */
export interface TypographyTokens {
  /** `grotesque` | `serif` | `mixed`. Absent uses the template's own faces. */
  pairing?: string;
  /** Multiplier on every type size, 0.85–1.30. */
  scale?: number;
  /** `compact` | `normal` | `airy`. */
  density?: string;
}

export interface PageTokens {
  /** Horizontal page margin in mm, 10–30. The vertical margin follows by the
   *  template's own ratio, so a template keeps its proportion. */
  margin_mm?: number;
  section_rules?: boolean;
}

export interface BrandTokens {
  /** One of the five validated accents. Anything else is ignored server-side —
   *  it recolours rules, numbering and the first chart series only, never the
   *  rest of the categorical palette. */
  accent?: string;
  logo_asset_id?: string | null;
  cover?: "full" | "band" | "minimal";
  approval?: boolean;
}

export interface ReportDesign {
  template: string | null;
  blocks_version: number;
  /** The list the report renders from — stored if there is one, derived
   *  otherwise. Empty only for a report with no stored section data. */
  blocks: ReportBlock[];
  /** Whether `blocks` came off the row. "Unedited" and "edited into exactly the
   *  default" are different facts. */
  stored: boolean;
  sections: ReportSectionSummary[];
  /** The document's stored name — what a `cover` renders unedited. Null for a
   *  report with no stored section data. */
  title: string | null;
  /** The three inspector panels, echoed back exactly as stored. Empty objects
   *  when nothing has been chosen. */
  typography: TypographyTokens;
  page: PageTokens;
  brand: BrandTokens;
}

/** The block list this report renders from, plus what each section permits.
 *
 *  Its own call rather than fields on `Report`, because answering it needs the
 *  full stored result — up to 4 MiB — which the list endpoint deliberately never
 *  fetches. Owner or admin only. */
export function getReportDesign(repositoryId: string, reportId: string): Promise<ReportDesign> {
  return api.get<ReportDesign>(`/repositories/${repositoryId}/reports/${reportId}/design`);
}

/** A partial design update: only the keys present are applied. */
export interface ReportDesignPatch {
  /** `null` clears the choice, returning the report to the repository default. */
  template?: string | null;
  /** `null` clears the list, returning the report to the derived default.
   *  Travels with `blocks_version`, which this client supplies. */
  blocks?: ReportBlock[] | null;
  /** `null` clears the group. Sent whole rather than per-field: the editor holds
   *  the group and sends it back, so a partial merge server-side would make
   *  "unset this one token" impossible to express. */
  typography?: TypographyTokens | null;
  page?: PageTokens | null;
  brand?: BrandTokens | null;
}

/** The version this client writes block lists against. Sent alongside `blocks`
 *  because the backend refuses to assume one — a payload whose version was
 *  inferred is exactly the ambiguity the field exists to remove. */
export const BLOCKS_VERSION = 1;

/** Store the design this report should wear. Does **not** rebuild it — that is
 *  what keeps `rerender` meaning exactly "rebuild from storage". Accepted in any
 *  status. */
export function setReportDesign(
  repositoryId: string,
  reportId: string,
  patch: ReportDesignPatch,
): Promise<Report> {
  const body: Record<string, unknown> = {};
  // Presence, not truthiness: `null` is a meaningful value for both keys and
  // means "clear it", which is a different request from leaving it alone.
  if ("template" in patch) body.template = patch.template;
  if ("blocks" in patch) {
    body.blocks = patch.blocks;
    body.blocks_version = BLOCKS_VERSION;
  }
  if ("typography" in patch) body.typography = patch.typography;
  if ("page" in patch) body.page = patch.page;
  if ("brand" in patch) body.brand = patch.brand;
  return api.put<Report>(`/repositories/${repositoryId}/reports/${reportId}/design`, body);
}

/** The design canvas's document: the printed report's own HTML, in preview mode.
 *
 *  Text, not JSON, and not a blob — the canvas mounts this string into a shadow
 *  root. Writes nothing and calls no provider; 409 when the report has no stored
 *  payload, 404 when it is not in this repository.
 *
 *  Roughly half the cost of the PDF path and not a rounding error against it:
 *  redrawing the charts dominates both, and only the printing is skipped.
 *  Measured in `services/reports.py::preview_report_html`. */
export function getReportPreviewHtml(repositoryId: string, reportId: string): Promise<string> {
  return api.getText(`/repositories/${repositoryId}/reports/${reportId}/design/preview`);
}

/** Rebuild both artifacts from stored section data — about a second, no LLM call
 *  and no query against the reporting database. 409 when the report has no
 *  stored payload. */
export function rerenderReport(repositoryId: string, reportId: string): Promise<Report> {
  return api.post<Report>(`/repositories/${repositoryId}/reports/${reportId}/rerender`);
}
