import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { useCurrentRepository } from "../repository/RepositoryContext";
import {
  listKnowledgeGaps,
  rejectGap,
  listRejectedGaps,
  type KnowledgeGap,
  type GapRejection,
} from "../api/knowledgeGaps";
import { getAnalytics, type AnalyticsOverview } from "../api/analytics";
import { createAdminNote } from "../api/adminNotes";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** The curation cockpit: knowledge gaps → Admin Notes → usage analytics. */
export function AdminInsightsPage(): ReactNode {
  const { t } = useTranslation();
  const { error } = useCurrentRepository();

  return (
    <div className="admin-insights">
      <h1>{t("insights.title")}</h1>
      {error !== null && <p className="error">{error}</p>}
      <KnowledgeGapsPanel />
      <AnalyticsPanel />
    </div>
  );
}

/** Ranked knowledge gaps for the current repository (shared switcher), each
 *  answerable inline with an Admin Note. */
function KnowledgeGapsPanel(): ReactNode {
  const { t } = useTranslation();
  const { currentRepoId } = useCurrentRepository();
  const [gaps, setGaps] = useState<KnowledgeGap[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [answered, setAnswered] = useState<string | null>(null);
  const [rejected, setRejected] = useState<GapRejection[] | null>(null);

  useEffect(() => {
    if (currentRepoId === "") return;
    let cancelled = false;
    setGaps(null);
    setError(null);
    setAnswered(null);
    setRejected(null);
    listKnowledgeGaps(currentRepoId)
      .then((g) => !cancelled && setGaps(g))
      .catch(
        (err: unknown) => !cancelled && setError(errorMessage(err, t("insights.errorLoadGaps"))),
      );
    listRejectedGaps(currentRepoId)
      .then((r) => !cancelled && setRejected(r))
      .catch(
        (err: unknown) => !cancelled && setError(errorMessage(err, t("insights.errorLoadGaps"))),
      );
    return () => {
      cancelled = true;
    };
  }, [currentRepoId, t]);

  const onAnswered = (question: string) => {
    // The gap closes once the note is ingested; drop it from the to-do list now.
    setGaps((prev) => prev?.filter((g) => g.question !== question) ?? prev);
    setAnswered(question);
  };

  const onRejected = (question: string) => {
    setGaps((prev) => prev?.filter((g) => g.question !== question) ?? prev);
    listRejectedGaps(currentRepoId)
      .then((r) => setRejected(r))
      .catch((err: unknown) => setError(errorMessage(err, t("insights.errorLoadGaps"))));
  };

  return (
    <section aria-label={t("insights.knowledgeGapsAriaLabel")}>
      <h2>{t("insights.knowledgeGaps")}</h2>

      {answered !== null && (
        <p className="success">{t("insights.noteSaved", { question: answered })}</p>
      )}
      {error !== null && <p className="error">{error}</p>}

      {gaps === null ? (
        <p>{t("insights.loadingGaps")}</p>
      ) : gaps.length === 0 ? (
        <p>{t("insights.noGaps")}</p>
      ) : (
        <ul className="gap-list">
          {gaps.map((gap) => (
            <GapRow
              key={gap.question}
              gap={gap}
              repositoryId={currentRepoId}
              onAnswered={() => onAnswered(gap.question)}
              onRejected={() => onRejected(gap.question)}
            />
          ))}
        </ul>
      )}

      <h3>{t("insights.rejectedGaps")}</h3>
      {rejected === null ? null : rejected.length === 0 ? (
        <p>{t("insights.noRejectedGaps")}</p>
      ) : (
        <ul className="rejected-gap-list">
          {rejected.map((r) => (
            <li key={r.question}>
              <span className="gap-question">{r.question}</span>
              <span className="gap-reason">{r.reason}</span>
              <span className="gap-signal">
                {t("insights.rejectedBy", {
                  admin: r.rejected_by ?? "—",
                  date: new Date(r.rejected_at).toLocaleDateString(),
                })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function GapRow({
  gap,
  repositoryId,
  onAnswered,
  onRejected,
}: {
  gap: KnowledgeGap;
  repositoryId: string;
  onAnswered: () => void;
  onRejected: () => void;
}): ReactNode {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [rejectError, setRejectError] = useState<string | null>(null);

  const onSave = async (e: FormEvent) => {
    e.preventDefault();
    if (content.trim() === "") return;
    setSaving(true);
    setError(null);
    try {
      await createAdminNote(repositoryId, { title: gap.question, content: content.trim() });
      onAnswered();
    } catch (err) {
      setError(errorMessage(err, t("insights.errorSaveNote")));
      setSaving(false);
    }
  };

  const onReject = async (e: FormEvent) => {
    e.preventDefault();
    if (reason.trim() === "") return;
    try {
      await rejectGap(repositoryId, { question: gap.question, reason: reason.trim() });
      onRejected(); // parent removes the gap + refreshes the rejected list
    } catch (err) {
      setRejectError(errorMessage(err, t("insights.errorRejectGap")));
    }
  };

  const lastAsked = new Date(gap.last_asked_at).toLocaleDateString();
  const titleId = `note-title-${gap.question}`;
  const answerId = `note-answer-${gap.question}`;
  const reasonId = `reject-reason-${gap.question}`;

  return (
    <li className="gap-item">
      <span className="gap-question">{gap.question}</span>
      <span className="gap-signal">
        {t("insights.gapSignal", {
          askCount: gap.ask_count,
          count: gap.user_count,
          lastAsked,
        })}
      </span>
      <div className="gap-actions">
        {!editing ? (
          <button type="button" onClick={() => setEditing(true)}>
            {t("insights.answerGap")}
          </button>
        ) : (
          <form className="note-editor" onSubmit={onSave}>
            <label htmlFor={titleId}>{t("insights.noteTitle")}</label>
            <input id={titleId} value={gap.question} readOnly />
            <label htmlFor={answerId}>{t("insights.answer")}</label>
            <textarea id={answerId} value={content} onChange={(e) => setContent(e.target.value)} />
            <button type="submit" disabled={saving || content.trim() === ""}>
              {t("insights.saveNote")}
            </button>
            {error !== null && <p className="error">{error}</p>}
          </form>
        )}

        {!rejecting ? (
          <button type="button" onClick={() => setRejecting(true)}>
            {t("insights.rejectGap")}
          </button>
        ) : (
          <form className="reject-editor" onSubmit={onReject}>
            <label htmlFor={reasonId}>{t("insights.rejectReason")}</label>
            <textarea
              id={reasonId}
              value={reason}
              placeholder={t("insights.rejectReasonPlaceholder")}
              onChange={(e) => setReason(e.target.value)}
            />
            <button type="submit" disabled={reason.trim() === ""}>
              {t("insights.confirmReject")}
            </button>
            {rejectError !== null && <p className="error">{rejectError}</p>}
          </form>
        )}
      </div>
    </li>
  );
}

/** Read-only usage dashboard over GET /analytics. */
function AnalyticsPanel(): ReactNode {
  const { t } = useTranslation();
  const [data, setData] = useState<AnalyticsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getAnalytics()
      .then((d) => !cancelled && setData(d))
      .catch(
        (err: unknown) =>
          !cancelled && setError(errorMessage(err, t("insights.errorLoadAnalytics"))),
      );
    return () => {
      cancelled = true;
    };
  }, [t]);

  return (
    <section aria-label={t("insights.analyticsAriaLabel")}>
      <h2>{t("insights.analytics")}</h2>
      {error !== null ? (
        <p className="error">{error}</p>
      ) : data === null ? (
        <p>{t("insights.loadingAnalytics")}</p>
      ) : (
        <>
          <div className="stat-row">
            <Stat label={t("insights.totalQueries")} value={data.total_queries} />
            <Stat label={t("insights.answered")} value={data.answered} />
            <Stat label={t("insights.notInVault")} value={data.not_in_vault} />
            <Stat
              label={t("insights.gapRate")}
              value={`${Math.round(data.not_in_vault_rate * 100)}%`}
            />
          </div>

          <h3>{t("insights.perRepository")}</h3>
          {data.per_repository.length === 0 ? (
            <p>{t("insights.noQueries")}</p>
          ) : (
            <table className="analytics-table">
              <thead>
                <tr>
                  <th>{t("insights.colRepository")}</th>
                  <th>{t("insights.colQueries")}</th>
                  <th>{t("insights.colGaps")}</th>
                </tr>
              </thead>
              <tbody>
                {data.per_repository.map((r) => (
                  <tr key={r.repository_id}>
                    <td>{r.repository_name}</td>
                    <td>{r.query_count}</td>
                    <td>{r.not_in_vault_count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>{t("insights.topQuestions")}</h3>
          <ul className="top-questions">
            {data.top_questions.map((q) => (
              <li key={q.question}>
                <span className="q-text">{q.question}</span>
                <span className="q-count">{q.ask_count}×</span>
              </li>
            ))}
          </ul>

          <h3>{t("insights.mostActiveUsers")}</h3>
          <ul className="active-users">
            {data.active_users.map((u) => (
              <li key={u.user_id}>
                <span className="u-name">{u.username}</span>
                <span className="u-count">
                  {t("insights.userQueryCount", { count: u.query_count })}
                </span>
              </li>
            ))}
          </ul>

          <h3>{t("insights.byDay")}</h3>
          <ul className="by-day">
            {data.by_day.map((d) => (
              <li key={d.day}>
                {t("insights.byDayRow", {
                  day: d.day,
                  total: d.total,
                  gaps: d.not_in_vault,
                })}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: number | string }): ReactNode {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}
