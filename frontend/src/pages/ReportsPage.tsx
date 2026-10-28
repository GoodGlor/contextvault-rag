import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { useCurrentRepository } from "../repository/RepositoryContext";
import {
  approveReport,
  createReport,
  deleteSchedule,
  deleteReport,
  downloadReport,
  type ReportFormat,
  isAwaitingApproval,
  isGenerating,
  listReportDatabases,
  listReports,
  listReportTemplates,
  listSchedules,
  patchSchedule,
  replanReport,
  type Report,
  type ReportDatabase,
  type ReportPlan,
  type Schedule,
  type TemplateChoice,
} from "../api/reports";
import { diffFilled, PlanEditor } from "./reports/PlanEditor";
import { PlanDesignStep } from "./reports/PlanDesignStep";
import { ReportDesignPanel } from "./reports/ReportDesignPanel";

/** How often to re-poll while any report is still generating. */
export const REPORT_POLL_MS = 2000;

/** Nothing attributed to the model. A module constant so the "not this report"
 *  branch below hands PlanEditor a stable value rather than a fresh Set each
 *  render. */
const NO_FILLED: Set<string> = new Set();

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** Any authenticated user: ask for a natural-language report; it is planned into
 *  sections drawn from the repository's ingested documents and (when connected)
 *  its reporting database, then compiled into a downloadable PDF. Nightly
 *  scheduling of blended reports is deferred — existing schedules remain listed
 *  below. */
export function ReportsPage(): ReactNode {
  const { t } = useTranslation();
  const { repos, currentRepoId, loading, error } = useCurrentRepository();

  const [reports, setReports] = useState<Report[] | null>(null);
  const [pollTick, setPollTick] = useState(0);
  const [reportsError, setReportsError] = useState<string | null>(null);

  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [schedules, setSchedules] = useState<Schedule[] | null>(null);
  const [schedulesError, setSchedulesError] = useState<string | null>(null);

  // The shipped templates are a property of the build, not of a report or of a
  // repository, so they are fetched once here and handed to every design panel.
  // Letting each panel fetch its own cost one identical GET per finished report.
  const [templates, setTemplates] = useState<TemplateChoice[]>([]);
  const [templatesError, setTemplatesError] = useState<string | null>(null);

  // The repository's databases, so the plan editor can print the NAME of the
  // database a section is routed to instead of the id the plan carries (#203).
  // Display only, and a failure is deliberately silent: an unnamed chip is a
  // small loss, while an error banner over a plan the reader can still review
  // and approve would be a large one.
  const [databases, setDatabases] = useState<ReportDatabase[]>([]);

  // The report whose plan the reader is working on, pinned by id.
  const [activeId, setActiveId] = useState<string | null>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  // Which half of the approval card is on screen for the report it belongs to,
  // and the plan as the reader left it — carried together, the same way `filled`
  // carries its id below, and for the same reason: a passive effect on `activeId`
  // is not guaranteed to commit before paint. The poll can retire the pinned
  // report and free the pin to a new one in the very tick that would mount
  // <PlanDesignStep> for it, and that component's own design-fetch effect runs
  // before a parent's reset effect in the same flush — long enough to issue
  // `GET .../design` for a report the reader never opened, or to let a stray
  // Generate post the previous report's plan onto this one. Comparing `id`
  // against `active.id` at render time leaves no ordering to get wrong.
  //
  // Local to the visit otherwise: the design is stored server-side as it is
  // chosen, the plan is not, so a reload lands back on step 1 with the design
  // already saved. That asymmetry is the honest one and needs no state to
  // express.
  const [review, setReview] = useState<{ id: string; step: 1 | 2; draft: ReportPlan | null }>(
    () => ({ id: "", step: 1, draft: null }),
  );
  // Which fields the last round filled in, and *which report* they belong to.
  // Empty until a round actually comes back: on round one every word is the
  // model's, and PlanEditor reads a non-empty set as licence to start attributing
  // text to the reader.
  //
  // The id is carried alongside the keys rather than cleared by an effect on
  // `activeId`. Passive effects are not guaranteed to run before paint, so a reset
  // effect lands a commit *after* <PlanEditor key={B}> has already mounted, and
  // B's round-one plan flashes A's provenance badges. Comparing ids at render time
  // has no ordering to get wrong.
  const [filled, setFilled] = useState<{ id: string; keys: Set<string> }>(() => ({
    id: "",
    keys: NO_FILLED,
  }));
  // The plan we sent to /replan, kept until the round it asked for lands. /replan
  // is asynchronous — it answers immediately with the row flipped to `planning`
  // and the *old* plan still attached — so the response is no baseline at all and
  // the diff has to wait for a later poll. `before` is the plan as it stood when
  // we sent, which is how that poll is told apart from one that brought nothing.
  const [sent, setSent] = useState<{
    id: string;
    plan: ReportPlan;
    before: ReportPlan | null;
  } | null>(null);

  // (Re)load this repository's reports and schedules whenever the current
  // repository changes.
  useEffect(() => {
    if (currentRepoId === "") return;
    let cancelled = false;
    setReports(null);
    setReportsError(null);
    listReports(currentRepoId)
      .then((rs) => !cancelled && setReports(rs))
      .catch(
        (err: unknown) =>
          !cancelled && setReportsError(errorMessage(err, t("reports.errorLoadReports"))),
      );
    setSchedules(null);
    setSchedulesError(null);
    listSchedules(currentRepoId)
      .then((ss) => !cancelled && setSchedules(ss))
      .catch(
        (err: unknown) =>
          !cancelled && setSchedulesError(errorMessage(err, t("reports.errorLoadSchedules"))),
      );
    return () => {
      cancelled = true;
    };
  }, [currentRepoId, t]);

  // The list of templates does not depend on the repository, so this runs once
  // per mount (and again only if the locale changes, for the failure message).
  useEffect(() => {
    let cancelled = false;
    listReportTemplates()
      .then((list) => {
        if (cancelled) return;
        setTemplates(list);
        // Cleared on success: a transient failure followed by a good refetch must
        // not leave a populated gallery sitting under a stale error.
        setTemplatesError(null);
      })
      .catch(() => !cancelled && setTemplatesError(t("reports.design.errorLoad")));
    return () => {
      cancelled = true;
    };
  }, [t]);

  // Per repository, not per report: every plan in a repository routes among the
  // same connections, so one fetch serves the whole page.
  useEffect(() => {
    if (currentRepoId === "") {
      setDatabases([]);
      return;
    }
    let cancelled = false;
    listReportDatabases(currentRepoId)
      .then((list) => !cancelled && setDatabases(list))
      .catch(() => !cancelled && setDatabases([]));
    return () => {
      cancelled = true;
    };
  }, [currentRepoId]);

  // Poll while anything is still generating, and stop once every report has reached
  // a terminal state — the same idiom as SourcesPanel's ingestion poll, including
  // the `pollTick` that re-arms after a *failed* attempt. Without it one rejected
  // request ended polling for good, stranding the list until a manual refresh.
  useEffect(() => {
    if (currentRepoId === "" || reports === null) return;
    if (!reports.some((r) => isGenerating(r.status))) return;
    const timer = setTimeout(() => {
      listReports(currentRepoId)
        .then(setReports)
        .catch(() => {
          /* transient; the tick below still re-arms the poll */
        })
        .finally(() => setPollTick((n) => n + 1));
    }, REPORT_POLL_MS);
    return () => clearTimeout(timer);
  }, [reports, pollTick, currentRepoId]);

  // Still the reader's current business: being planned, or waiting on them.
  const holdsPin = (r: Report) => r.status === "planning" || isAwaitingApproval(r.status);
  // …and there is actually a plan to show. A row stuck in `awaiting_approval` with
  // no plan can be neither reviewed nor approved, so it must never take the
  // editor's place from a row that can; it stays in the list, with its status and
  // its Delete button.
  const reviewable = (r: Report) => holdsPin(r) && r.plan !== null;

  // Pinned by id, never re-derived per render. Picking "the first row awaiting
  // approval" each time would let a second report finishing its plan steal the
  // editor mid-edit, and PlanEditor re-seeds its draft from a changed plan — the
  // first report's edits would vanish with no trace and no undo.
  const active = reports?.find((r) => r.id === activeId && reviewable(r)) ?? null;
  // Narrowed once, as a `const`, so it stays narrowed inside the closures below —
  // `active.plan` itself does not: TS drops a property's narrowing across a
  // function boundary since the object could change between the check and the
  // call, even though `active` never does here.
  const activePlan = active?.plan ?? null;
  // Read against `active.id` at render time, never trusted across a report
  // switch: see the comment on `review` above. A stale `review` from a report
  // that is no longer pinned falls back to step 1 with no draft, in the very
  // same render that swaps `active` — there is no frame where it could show.
  const step = active !== null && review.id === active.id ? review.step : 1;
  const draft = active !== null && review.id === active.id ? review.draft : null;

  // Adopt a plan only when the pin is free: on first load, and once the pinned
  // report leaves the loop (approved, done, failed, deleted, or a repository
  // switch). A report the reader just asked for keeps the pin through `planning`,
  // before it has a plan, so a stale row cannot jump the queue.
  useEffect(() => {
    const pinned = reports?.find((r) => r.id === activeId);
    if (pinned !== undefined && holdsPin(pinned)) return;
    setActiveId(reports?.find(reviewable)?.id ?? null);
    // Both predicates are pure functions of their argument, re-created each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reports, activeId]);

  // A different report means a different plan, so last report's error and
  // in-flight round are meaningless here. (Provenance needs no reset: `filled`
  // carries the id it was computed for and the render below ignores it otherwise.)
  useEffect(() => {
    setPlanError(null);
    setSent(null);
  }, [activeId]);

  // The round we asked for has landed: now — and only now — is there something
  // honest to diff. Clearing `sent` doubles as the re-entry guard.
  useEffect(() => {
    if (sent === null || active === null || active.id !== sent.id) return;
    if (!isAwaitingApproval(active.status) || active.plan === null) return;
    // Back at `awaiting_approval` is not proof the model ran. A round that failed
    // inside the worker is rolled back to the *previous* plan with an error on the
    // row, and an in-flight poll can land a pre-replan snapshot after the POST
    // resolved. Both would have us credit the model for text it never wrote — the
    // original bug, on the paths that look most like success. So compare by value,
    // the same way PlanEditor decides whether to re-seed: no change, no round.
    // (`plan_rounds` cannot stand in for this — the backend deliberately skips the
    // increment when the draft had no blanks to fill.)
    if (JSON.stringify(active.plan) === JSON.stringify(sent.before)) return;
    setFilled({ id: active.id, keys: diffFilled(sent.plan, active.plan) });
    setSent(null);
    // A genuinely new plan landed for the report on screen. A draft pinned by an
    // earlier Next press — the only route back to Re-plan after advancing — would
    // otherwise keep pinning the *old* plan into step 1 forever: PlanEditor only
    // re-seeds when its `plan` prop changes, and a non-null draft holds that prop
    // fixed no matter what the round brings back. Left uncleared, the model's
    // rewrite is never shown, yet the badges above still credit it for the words
    // it just wrote — attributing text to a plan the reader cannot see. Clearing
    // it here, not in `onReplan`'s success branch, matters: at that moment the row
    // still carries the *pre-replan* plan, so clearing there would re-seed step 1
    // backwards, mid-flight.
    setReview((prev) => (prev.id === active.id ? { ...prev, draft: null } : prev));
  }, [active, sent]);

  const replaceRow = (updated: Report) =>
    setReports((prev) => prev?.map((r) => (r.id === updated.id ? updated : r)) ?? prev);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const p = prompt.trim();
    if (currentRepoId === "" || p === "" || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await createReport(currentRepoId, p);
      setReports((prev) => [created, ...(prev ?? [])]);
      // Claim the pin for the report they just asked for, so a plan left waiting
      // from an earlier session does not hold the editor. The reset above clears
      // the previous report's provenance and error along with it.
      setActiveId(created.id);
      setPrompt("");
    } catch (err) {
      setSubmitError(errorMessage(err, t("reports.errorCreate")));
    } finally {
      setSubmitting(false);
    }
  };

  // Both handlers take the plan the editor has on screen, not the one on the row:
  // posting the server's copy back would discard every edit the reader just made.
  const onReplan = async (report: Report, plan: ReportPlan) => {
    setPlanBusy(true);
    setPlanError(null);
    try {
      const updated = await replanReport(currentRepoId, report.id, plan);
      replaceRow(updated);
      // `updated` only says the round was accepted — the row is `planning` and its
      // plan is still the one on screen. Remember what we sent and let the effect
      // above diff it when the answer arrives.
      setSent({ id: report.id, plan, before: report.plan });
      setFilled({ id: "", keys: NO_FILLED });
    } catch (err) {
      // The draft lives in PlanEditor and the row is untouched, so a failed round
      // costs the reader nothing but the message — but last round's attribution
      // must go, or it stands against edits it was never computed from.
      setPlanError(errorMessage(err, t("reports.errorReplan")));
      setFilled({ id: "", keys: NO_FILLED });
      setSent(null);
    } finally {
      setPlanBusy(false);
    }
  };

  const onApprove = async (report: Report, plan: ReportPlan) => {
    setPlanBusy(true);
    setPlanError(null);
    try {
      // Generation has started: the row leaves the review loop, which releases the
      // pin, unmounts the editor and hands the report back to the poll.
      replaceRow(await approveReport(currentRepoId, report.id, plan));
    } catch (err) {
      setPlanError(errorMessage(err, t("reports.errorApprove")));
    } finally {
      setPlanBusy(false);
    }
  };

  const onDownload = async (report: Report, format: ReportFormat) => {
    try {
      const blob = await downloadReport(currentRepoId, report.id, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report-${report.id}.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setReportsError(errorMessage(err, t("reports.errorDownload")));
    }
  };

  const onDelete = async (report: Report) => {
    if (!window.confirm(t("reports.confirmDelete"))) return;
    try {
      await deleteReport(currentRepoId, report.id);
      setReports((prev) => prev?.filter((r) => r.id !== report.id) ?? prev);
    } catch (err) {
      setReportsError(errorMessage(err, t("reports.errorDelete")));
    }
  };

  const onToggleSchedule = async (schedule: Schedule) => {
    try {
      const updated = await patchSchedule(schedule.id, { enabled: !schedule.enabled });
      setSchedules((prev) => prev?.map((s) => (s.id === updated.id ? updated : s)) ?? prev);
    } catch (err) {
      setSchedulesError(errorMessage(err, t("reports.errorToggleSchedule")));
    }
  };

  const onDeleteSchedule = async (schedule: Schedule) => {
    try {
      await deleteSchedule(schedule.id);
      setSchedules((prev) => prev?.filter((s) => s.id !== schedule.id) ?? prev);
    } catch (err) {
      setSchedulesError(errorMessage(err, t("reports.errorDeleteSchedule")));
    }
  };

  if (error !== null) {
    return <p className="error">{error}</p>;
  }
  if (loading) {
    return <p>{t("reports.loadingRepos")}</p>;
  }
  if (repos.length === 0) {
    return <p>{t("reports.noRepos")}</p>;
  }

  const statusLabels: Record<Report["status"], string> = {
    planning: t("reports.statusPlanning"),
    awaiting_approval: t("reports.statusAwaiting"),
    pending: t("reports.statusPending"),
    processing: t("reports.statusProcessing"),
    done: t("reports.statusDone"),
    failed: t("reports.statusFailed"),
  };

  return (
    <section className="reports-page">
      <h1>{t("reports.title")}</h1>

      <form className="report-request" onSubmit={onSubmit}>
        <label htmlFor="report-prompt">{t("reports.promptLabel")}</label>
        <textarea
          id="report-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder={t("reports.promptPlaceholder")}
        />
        <p className="form-hint">{t("reports.sourcesHint")}</p>
        <button type="submit" disabled={submitting || prompt.trim() === ""}>
          {submitting ? t("reports.generating") : t("reports.generateButton")}
        </button>
        {submitError !== null && <p className="error">{submitError}</p>}
      </form>

      {/* Keyed by report so a different plan gets a different editor rather than a
          re-seeded one. `busy` covers the whole asynchronous round, not just the
          POST: /replan answers long before the model does, and re-enabling the
          buttons in between would earn the reader a 409. */}
      {active !== null && activePlan !== null && (
        <div className="plan-card">
          <ol className="plan-steps" aria-label={t("reports.planTitle")}>
            <li aria-current={step === 1 ? "step" : undefined}>{t("reports.steps.plan")}</li>
            <li aria-current={step === 2 ? "step" : undefined}>{t("reports.steps.design")}</li>
          </ol>

          {step === 1 ? (
            <PlanEditor
              key={active.id}
              // The reader's own draft when there is one: step 2 unmounted this
              // editor, and re-seeding from the row would silently drop the edit
              // they made before pressing Далі.
              plan={draft ?? activePlan}
              rounds={active.plan_rounds}
              busy={planBusy || active.status === "planning"}
              error={planError}
              databases={databases}
              filled={filled.id === active.id ? filled.keys : NO_FILLED}
              onReplan={(plan) => void onReplan(active, plan)}
              // Advance, not approve. No network call, nothing committed: `/approve`
              // takes the plan body at the end precisely so a late edit is not lost.
              onApprove={(plan, restructured) => {
                setReview({ id: active.id, step: 2, draft: plan });
                // `filled`'s `s{i}` keys index the plan PlanEditor received them
                // against. A reorder, removal or addition shifts what those indices
                // point at — PlanEditor's own `restructured` flag already suppresses
                // the stale badges for exactly this reason, and it hands that flag
                // through here because the flag itself is component-local state
                // that dies with the editor the moment step 2 unmounts it.
                // `review.draft` faithfully carries a reordered plan back across a
                // later Back trip; `filled` would not know to follow on its own, so
                // a badge could land on a section the model never touched. Only
                // clear it when the plan was actually restructured: the far more
                // common trip — look at the design step, come straight back,
                // nothing reordered — must keep every legitimate badge, or the
                // record of what the model wrote versus what the reader kept is
                // lost on every ordinary Next/Back, not just the rare one that
                // needs protecting.
                if (restructured) setFilled({ id: "", keys: NO_FILLED });
              }}
            />
          ) : (
            <PlanDesignStep
              repositoryId={currentRepoId}
              reportId={active.id}
              templates={templates}
              templatesError={templatesError}
              busy={planBusy}
              error={planError}
              onBack={() => setReview({ id: active.id, step: 1, draft })}
              onGenerate={() => void onApprove(active, draft ?? activePlan)}
            />
          )}
        </div>
      )}

      {reportsError !== null && <p className="error">{reportsError}</p>}
      {/* Once, for the page — not once per row. It is only worth saying when
          there is a finished report whose design the reader could have chosen. */}
      {templatesError !== null && reports !== null && reports.some((r) => r.status === "done") && (
        <p className="error">{templatesError}</p>
      )}
      {reports === null ? (
        <p>{t("reports.loadingReports")}</p>
      ) : reports.length === 0 ? (
        <p>{t("reports.noReports")}</p>
      ) : (
        <ul className="report-list">
          {reports.map((r) => (
            <li key={r.id} className="report-item">
              <span className="report-prompt">{r.prompt}</span>
              {/* Several reports a day is normal, so the time matters as much as
                  the date; the machine-readable value stays in dateTime. */}
              <time className="report-created" dateTime={r.created_at}>
                {new Date(r.created_at).toLocaleString()}
              </time>
              <span className={`badge status-${r.status}`}>{statusLabels[r.status]}</span>
              {/* `planning` is the LLM composing the plan — generation has not
                  started and may never start, since the reader still has to
                  approve. Saying "Generating…" next to a "Planning" badge claims
                  work that is not happening. */}
              {isGenerating(r.status) && (
                <span className="report-generating" role="status">
                  {r.status === "planning"
                    ? t("reports.planningLabel")
                    : t("reports.generatingLabel")}
                </span>
              )}
              {/* Not only when the report failed: a re-plan that blows up sends the
                  row back to `awaiting_approval` with the reason attached, and
                  hiding it there would leave the reader re-pressing a button that
                  has already told them why it will not work. */}
              {r.error !== null && <span className="report-error">{r.error}</span>}
              {r.status === "done" && (r.has_pdf || r.has_pptx) && (
                <span className="report-downloads">
                  {r.has_pdf && (
                    <button type="button" onClick={() => void onDownload(r, "pdf")}>
                      {t("reports.downloadPdfButton")}
                    </button>
                  )}
                  {/* Reports generated before PPTX existed have no deck, so the
                      button is offered only when the artifact is really there. */}
                  {r.has_pptx && (
                    <button type="button" onClick={() => void onDownload(r, "pptx")}>
                      {t("reports.downloadPptxButton")}
                    </button>
                  )}
                </span>
              )}
              {/* A design belongs to a finished report: there is nothing to
                  restyle until the sections have been executed and stored. */}
              {r.status === "done" && (
                <ReportDesignPanel
                  report={r}
                  templates={templates}
                  templatesError={templatesError}
                  onApplied={(updated) =>
                    setReports((prev) =>
                      prev === null ? prev : prev.map((x) => (x.id === updated.id ? updated : x)),
                    )
                  }
                />
              )}
              <button type="button" className="report-delete" onClick={() => void onDelete(r)}>
                {t("reports.deleteButton")}
              </button>
            </li>
          ))}
        </ul>
      )}

      <h2>{t("reports.schedulesTitle")}</h2>
      {schedulesError !== null && <p className="error">{schedulesError}</p>}
      {schedules === null ? (
        <p>{t("reports.loadingSchedules")}</p>
      ) : schedules.length === 0 ? (
        <p>{t("reports.noSchedules")}</p>
      ) : (
        <ul className="schedule-list">
          {schedules.map((s) => (
            <li key={s.id} className="schedule-item">
              <span className="schedule-prompt">{s.prompt}</span>
              <span className="schedule-time">{s.run_at_time}</span>
              <label className="schedule-enabled">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  onChange={() => void onToggleSchedule(s)}
                  aria-label={t("reports.enabledLabel")}
                />
                {t("reports.enabledLabel")}
              </label>
              {s.last_error !== null && <span className="schedule-error">{s.last_error}</span>}
              <button type="button" onClick={() => void onDeleteSchedule(s)}>
                {t("reports.deleteButton")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
