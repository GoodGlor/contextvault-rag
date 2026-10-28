import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { PlanSection, ReportDatabase, ReportPlan } from "../../api/reports";

/** Mirrors MAX_SECTIONS in the report planner: a plan the server would reject can
 *  never be built here in the first place. */
export const MAX_SECTIONS = 6;

/** The server requires at least one section, so the last one cannot be removed. */
const MIN_SECTIONS = 1;

/** Mirror ReportPlanDraft's field caps (report_planner.py). Without them an
 *  over-long entry is only refused at replan/approve, as a raw pydantic message
 *  about a field the reader cannot see — the browser can simply stop the typing. */
const MAX_TITLE = 300;
const MAX_SUB_QUESTION = 2000;

/** The languages the select can represent. A value outside this list would leave
 *  the select displaying the first option while the draft still held the other
 *  value — the control would be lying about what will be sent. */
const LANGUAGES = ["uk", "en"];

const blank = (v: string): boolean => v.trim() === "";

/** Snap an incoming plan onto values this editor can actually represent.
 *
 *  Only `language` needs it: a value outside LANGUAGES would leave the select
 *  displaying the first option while the draft still held the other value — the
 *  control would be lying about what will be sent. Sources are no longer snapped;
 *  the plan chooses them and the editor only displays them. */
function normalisePlan(plan: ReportPlan): ReportPlan {
  return {
    ...plan,
    language: LANGUAGES.includes(plan.language) ? plan.language : LANGUAGES[0],
  };
}

export interface PlanEditorProps {
  plan: ReportPlan;
  rounds: number;
  busy: boolean;
  error: string | null;
  /** The repository's databases, for turning a section's routing into a name
   *  (#203). Display only: the plan chooses the database and there is no control
   *  to change it, the same decision #186 made about the source itself. Empty
   *  while the list is still loading, or if the reader's repository has none. */
  databases: ReportDatabase[];
  /** Fields the LLM filled on the last round, as "title" | "s{i}.title" | "s{i}.sub_question". */
  filled: Set<string>;
  onReplan: (plan: ReportPlan) => void;
  /** `restructured` mirrors this editor's own flag of the same name below: the
   *  reader reordered, removed or added a section since the plan was seeded, so
   *  `filled`'s `s{i}` keys no longer index the sections they were computed
   *  against. It is the only way the caller can know that — the editor unmounts
   *  on advance and takes its local `restructured` state with it. */
  onApprove: (plan: ReportPlan, restructured: boolean) => void;
}

/** Which fields the model filled — the difference between what the user sent and
 *  what came back. Blank on the way out, non-blank on the way back. Derived on the
 *  client so the server needs no extra field. */
// The page imports this pure helper alongside <PlanEditor/>; splitting six lines
// into their own module just to appease fast refresh is not worth the indirection.
// eslint-disable-next-line react-refresh/only-export-components
export function diffFilled(sent: ReportPlan, received: ReportPlan): Set<string> {
  const out = new Set<string>();
  const blank = (v: string) => v.trim() === "";
  if (blank(sent.title) && !blank(received.title)) out.add("title");
  // No `language` branch: the language select is never blank (it is normalised on
  // seed), so the key could only ever be dead weight in the contract.
  received.sections.forEach((r, i) => {
    const s = sent.sections[i];
    if (s === undefined) return;
    if (blank(s.title) && !blank(r.title)) out.add(`s${i}.title`);
    if (blank(s.sub_question) && !blank(r.sub_question)) out.add(`s${i}.sub_question`);
    // No `source` branch, deliberately: the chip that shows a section's source has
    // no badge slot to attribute a fill to, unlike title and sub-question's inputs.
  });
  return out;
}

function ArrowUpIcon(): ReactNode {
  // Explicit width/height: an unsized inline SVG expands to fill its button.
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 5l7 7h-4v7h-6v-7H5z" fill="currentColor" />
    </svg>
  );
}

function ArrowDownIcon(): ReactNode {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 19l-7-7h4V5h6v7h4z" fill="currentColor" />
    </svg>
  );
}

function RemoveIcon(): ReactNode {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M6 6l12 12M18 6L6 18"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  );
}

/** Review and edit the LLM's plan before anything runs.
 *
 *  The editor owns a *local draft* seeded from `props.plan`; every button hands
 *  that draft back, never the prop. Posting the server's plan back unchanged would
 *  silently discard every edit the user just made — the exact failure this review
 *  loop exists to prevent — so the distinction is load-bearing, not stylistic.
 *
 *  Blanks are meaningful: they are a request for the model to fill that one field
 *  on the next round. That is why "Re-plan" stays enabled on an incomplete plan
 *  while "Generate report" does not.
 *
 *  `busy` freezes *every* control, not just the two action buttons. A round is an
 *  LLM call lasting seconds and the plan deliberately stays on screen throughout,
 *  so a still-editable field is an invitation to type into a draft that the
 *  landing round is about to replace wholesale — the same silent discard this
 *  component exists to prevent, just from the other direction. */
export function PlanEditor(props: PlanEditorProps): ReactNode {
  const { t } = useTranslation();
  const { plan, rounds, busy, error, databases, filled, onReplan, onApprove } = props;

  const [draft, setDraft] = useState<ReportPlan>(() => normalisePlan(plan));

  // Once the user reorders, deletes or adds a section, `filled`'s `s{i}` keys point
  // at positions in the *received* plan that no longer line up with the draft, so
  // every "filled in by the LLM" badge would be attributing text to the wrong
  // section. Silence beats a confident lie.
  const [restructured, setRestructured] = useState(false);

  // Re-seed on *value*, not object identity. ReportsPage re-polls every 2s while
  // any report in the repository is generating and hands us a freshly parsed —
  // hence brand new — plan object each time. Keying on identity threw the draft
  // away mid-keystroke; keying on value keeps the edits and still swaps the draft
  // when a re-plan genuinely returns something different.
  const seeded = useRef<string>(JSON.stringify(plan));
  useEffect(() => {
    const next = JSON.stringify(plan);
    if (next === seeded.current) return;
    seeded.current = next;
    setDraft(normalisePlan(plan));
    setRestructured(false);
  }, [plan]);

  // Which action is in flight, so the two buttons can label their own progress —
  // `busy` alone cannot tell them apart.
  const [pending, setPending] = useState<"replan" | "approve" | null>(null);
  useEffect(() => {
    if (!busy) setPending(null);
  }, [busy]);

  function patchSection(index: number, patch: Partial<PlanSection>): void {
    setDraft((d) => ({
      ...d,
      sections: d.sections.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    }));
  }

  function moveSection(index: number, delta: number): void {
    setRestructured(true);
    setDraft((d) => {
      const target = index + delta;
      if (target < 0 || target >= d.sections.length) return d;
      const sections = [...d.sections];
      const [moved] = sections.splice(index, 1);
      sections.splice(target, 0, moved);
      return { ...d, sections };
    });
  }

  function removeSection(index: number): void {
    setRestructured(true);
    setDraft((d) => {
      if (d.sections.length <= MIN_SECTIONS) return d;
      return { ...d, sections: d.sections.filter((_, i) => i !== index) };
    });
  }

  function addSection(): void {
    setRestructured(true);
    setDraft((d) => {
      if (d.sections.length >= MAX_SECTIONS) return d;
      // Blank: the reader just added this section and has not routed it anywhere.
      // The next re-plan is what fills it (PlanSection.source's doc comment) —
      // `databases` included, since choosing one is the same kind of decision.
      return {
        ...d,
        sections: [
          ...d.sections,
          { title: "", source: "", databases: [], columns: [], sub_question: "" },
        ],
      };
    });
  }

  // Approval needs a complete plan; a re-plan is precisely how blanks get filled —
  // a source the reader added included, since only the model can choose one.
  // An unrouted database section is the same kind of blank (#203): the server
  // refuses to finalize one, and this mirrors that rather than letting the reader
  // press Generate and be told no.
  const unrouted = (s: PlanSection): boolean => s.source === "database" && s.databases.length === 0;
  const incomplete =
    blank(draft.title) ||
    draft.sections.length === 0 ||
    draft.sections.some(
      (s) => blank(s.title) || blank(s.source) || blank(s.sub_question) || unrouted(s),
    );

  // Nothing to attribute before the first re-plan: on round one every word is the
  // model's, so a "kept from your edit" badge would be a lie.
  const showProvenance = rounds > 1 || filled.size > 0;

  function badge(key: string, value: string): ReactNode {
    if (filled.has(key)) {
      return <span className="plan-badge plan-badge-filled">{t("reports.badgeFilled")}</span>;
    }
    if (showProvenance && !blank(value)) {
      return <span className="plan-badge plan-badge-locked">{t("reports.badgeLocked")}</span>;
    }
    return null;
  }

  /** Per-section provenance, which only survives while section indices still line
   *  up with the plan `filled` was computed against. */
  function sectionBadge(key: string, value: string): ReactNode {
    return restructured ? null : badge(key, value);
  }

  function sourceLabelFor(source: PlanSection["source"]): string {
    if (source === "database") return t("reports.sourceDatabase");
    if (source === "documents") return t("reports.sourceDocuments");
    return "—";
  }

  /** The names of the databases a section reads, or null when it reads none.
   *
   *  Falls back to nothing rather than to the raw id: an id is not a name, and a
   *  chip showing a UUID tells the reader less than no chip at all. That happens
   *  while the list is still loading and if a connection was deleted after the
   *  plan was made — the latter is exactly what generation reports as a section
   *  error, so the plan editor staying quiet about it is consistent. */
  function databaseLabelFor(section: PlanSection): string | null {
    if (section.source !== "database" || section.databases.length === 0) return null;
    const names = section.databases
      .map((id) => databases.find((d) => d.id === id)?.name)
      .filter((name): name is string => name !== undefined);
    return names.length === 0 ? null : names.join(", ");
  }

  return (
    <section className="plan-editor">
      <h3>{t("reports.planTitle")}</h3>
      <p className="plan-intro">{t("reports.planIntro")}</p>
      <p className="plan-round">{t("reports.planRound", { count: rounds })}</p>

      <div className="plan-head">
        <label className="plan-field">
          <span>{t("reports.reportTitleLabel")}</span>
          <input
            type="text"
            value={draft.title}
            maxLength={MAX_TITLE}
            disabled={busy}
            aria-label={t("reports.reportTitleLabel")}
            onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
          />
          {badge("title", draft.title)}
        </label>
        <label className="plan-field">
          <span>{t("reports.languageLabel")}</span>
          <select
            value={draft.language}
            disabled={busy}
            aria-label={t("reports.languageLabel")}
            onChange={(e) => setDraft((d) => ({ ...d, language: e.target.value }))}
          >
            {/* Endonyms, deliberately untranslated — same convention as LanguageSwitcher. */}
            <option value="uk">Українська</option>
            <option value="en">English</option>
          </select>
        </label>
      </div>

      <ol className="plan-sections">
        {draft.sections.map((section, index) => (
          <li className="plan-section" key={index}>
            <div className="plan-section-row">
              <span className="plan-section-number">{index + 1}</span>
              <input
                type="text"
                className="plan-section-title"
                value={section.title}
                maxLength={MAX_TITLE}
                disabled={busy}
                aria-label={t("reports.sectionTitleLabel")}
                onChange={(e) => patchSection(index, { title: e.target.value })}
              />
              {/* role="img" gives this span an ARIA-permitted accessible name: a bare
                  <span> is role=generic, and generic prohibits aria-label — assistive
                  tech is allowed to drop it, which matters most in the unset state
                  where the visible text is just "—". */}
              <span
                className={`plan-source-chosen plan-source-${section.source || "unset"}`}
                role="img"
                aria-label={
                  section.source === ""
                    ? t("reports.sourceUnsetLabel")
                    : `${t("reports.sourceLabel")}: ${sourceLabelFor(section.source)}`
                }
              >
                {sourceLabelFor(section.source)}
              </span>
              {databaseLabelFor(section) !== null && (
                <span
                  className="plan-database-chosen"
                  role="img"
                  aria-label={`${t("reports.databaseLabel")}: ${databaseLabelFor(section)}`}
                >
                  {databaseLabelFor(section)}
                </span>
              )}
              <button
                type="button"
                className="icon-button"
                aria-label={t("reports.moveUp")}
                disabled={busy || index === 0}
                onClick={() => moveSection(index, -1)}
              >
                <ArrowUpIcon />
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label={t("reports.moveDown")}
                disabled={busy || index === draft.sections.length - 1}
                onClick={() => moveSection(index, 1)}
              >
                <ArrowDownIcon />
              </button>
              <button
                type="button"
                className="icon-button"
                aria-label={t("reports.removeSection")}
                disabled={busy || draft.sections.length <= MIN_SECTIONS}
                onClick={() => removeSection(index)}
              >
                <RemoveIcon />
              </button>
            </div>
            <label className="plan-field">
              <span>{t("reports.subQuestionLabel")}</span>
              <textarea
                rows={2}
                value={section.sub_question}
                maxLength={MAX_SUB_QUESTION}
                disabled={busy}
                aria-label={t("reports.subQuestionLabel")}
                placeholder={t("reports.subQuestionPlaceholder")}
                onChange={(e) => patchSection(index, { sub_question: e.target.value })}
              />
            </label>
            <div className="plan-badges">
              {sectionBadge(`s${index}.title`, section.title)}
              {sectionBadge(`s${index}.sub_question`, section.sub_question)}
            </div>
          </li>
        ))}
      </ol>

      <button
        type="button"
        className="plan-add"
        disabled={busy || draft.sections.length >= MAX_SECTIONS}
        onClick={addSection}
      >
        {t("reports.addSection")}
      </button>

      {error !== null && <p className="error">{error}</p>}
      {incomplete && <p className="plan-hint">{t("reports.planIncomplete")}</p>}

      <div className="plan-actions">
        <button
          type="button"
          className="plan-replan"
          disabled={busy}
          onClick={() => {
            setPending("replan");
            onReplan(draft);
          }}
        >
          {busy && pending === "replan" ? t("reports.replanning") : t("reports.replanButton")}
        </button>
        <button
          type="button"
          className="plan-approve"
          disabled={busy || incomplete}
          onClick={() => {
            setPending("approve");
            onApprove(draft, restructured);
          }}
        >
          {busy && pending === "approve" ? t("reports.approving") : t("reports.steps.next")}
        </button>
      </div>
    </section>
  );
}
