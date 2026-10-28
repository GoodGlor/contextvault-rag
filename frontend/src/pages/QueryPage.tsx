import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { queryRepository, type QueryResult } from "../api/query";
import { clearConversation, getConversation } from "../api/conversations";
import { QueryTurn } from "../components/QueryTurn";
import { useCurrentRepository } from "../repository/RepositoryContext";

interface Turn {
  id: string;
  question: string;
  result: QueryResult;
  repositoryId: string;
}

/** The core user experience: ask a question of the current repository (chosen
 *  in the sidebar switcher) and get a cited answer. */
export function QueryPage(): ReactNode {
  const { t } = useTranslation();
  const { repos, currentRepoId, loading, error } = useCurrentRepository();
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const turnSeq = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keep the newest message in view as the conversation grows. (scrollIntoView is
  // unimplemented in jsdom; the optional call no-ops in tests.)
  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns, asking]);

  // A conversation belongs to one repository. Switching repos (via the sidebar
  // switcher) changes `currentRepoId`; the effect below loads that repository's
  // own saved conversation (or starts fresh when it has none), so stale history
  // from the previous repo never leaks in.
  //
  // Restore this user's saved conversation for the current repository — this
  // runs on mount and again on every repo switch.
  useEffect(() => {
    if (currentRepoId === "") return;
    let cancelled = false;
    setAskError(null);
    getConversation(currentRepoId)
      .then((c) => {
        if (cancelled) return;
        setTurns(
          c.turns.map((t, i) => ({
            id: `saved-${i}`,
            question: t.question,
            result: {
              answer: t.answer,
              not_in_vault: t.not_in_vault,
              citations: t.citations,
              sources: t.sources,
            },
            repositoryId: currentRepoId,
          })),
        );
        turnSeq.current = c.turns.length;
      })
      .catch(() => {
        if (!cancelled) setTurns([]);
      });
    return () => {
      cancelled = true;
    };
  }, [currentRepoId]);

  const submit = async () => {
    const q = question.trim();
    if (currentRepoId === "" || q === "" || asking) return;
    setAsking(true);
    setAskError(null);
    try {
      const result = await queryRepository(currentRepoId, q);
      turnSeq.current += 1;
      setTurns((prev) => [
        ...prev,
        { id: String(turnSeq.current), question: q, result, repositoryId: currentRepoId },
      ]);
      setQuestion("");
    } catch (err) {
      setAskError(err instanceof ApiError ? err.detail : t("common.somethingWrong"));
    } finally {
      setAsking(false);
    }
  };

  // Delete this user's saved thread for the current repository and reset the
  // on-screen transcript to match.
  const onClear = async () => {
    if (currentRepoId === "") return;
    try {
      await clearConversation(currentRepoId);
      setTurns([]);
      turnSeq.current = 0;
    } catch (err) {
      setAskError(err instanceof ApiError ? err.detail : t("common.somethingWrong"));
    }
  };

  const onAsk = (e: FormEvent) => {
    e.preventDefault();
    void submit();
  };

  // Enter sends; Shift+Enter inserts a newline (standard chat-composer behaviour).
  const onComposerKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  if (error !== null) {
    return (
      <div className="page">
        <p className="form-error" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="page">
        <p>{t("query.loadingRepos")}</p>
      </div>
    );
  }

  if (repos.length === 0) {
    return (
      <div className="page">
        <h1>{t("query.title")}</h1>
        <p>{t("query.noAccess")}</p>
      </div>
    );
  }

  return (
    <div className="page query-page chat">
      <div className="chat-header">
        <h1>{t("query.title")}</h1>
        {turns.length > 0 && (
          <button type="button" className="chat-clear" onClick={() => void onClear()}>
            {t("query.clearConversation")}
          </button>
        )}
      </div>

      <div className="chat-log">
        {turns.length === 0 && !asking && <p className="chat-empty">{t("query.emptyChat")}</p>}
        {turns.map((turn) => (
          <QueryTurn
            key={turn.id}
            question={turn.question}
            result={turn.result}
            repositoryId={turn.repositoryId}
          />
        ))}
        {asking && (
          <div className="turn">
            <div className="msg-row assistant">
              <div className="bubble thinking" role="status">
                {t("query.asking")}
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form className="chat-composer" onSubmit={onAsk}>
        {askError !== null && (
          <p className="form-error" role="alert">
            {askError}
          </p>
        )}
        <div className="composer-row">
          <textarea
            aria-label={t("query.question")}
            rows={1}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={onComposerKeyDown}
            placeholder={t("query.messagePlaceholder")}
          />
          <button type="submit" disabled={asking || question.trim() === ""}>
            {asking ? t("query.asking") : t("query.send")}
          </button>
        </div>
      </form>
    </div>
  );
}
