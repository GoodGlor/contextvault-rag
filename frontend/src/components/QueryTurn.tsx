import { useRef, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { QueryResult } from "../api/query";
import { AnswerText } from "./AnswerText";
import { SourceList } from "./SourceList";

/**
 * One question/answer exchange. Owns the citation→source highlight so clicking a
 * `[n]` chip in the answer marks and scrolls to the matching source below it.
 */
export function QueryTurn({
  question,
  result,
  repositoryId,
}: {
  question: string;
  result: QueryResult;
  repositoryId: string;
}): ReactNode {
  const { t } = useTranslation();
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const refs = useRef(new Map<string, HTMLLIElement>());

  const registerRef = (id: string, el: HTMLLIElement | null) => {
    if (el) refs.current.set(id, el);
    else refs.current.delete(id);
  };

  const onCite = (sourceId: string) => {
    setHighlightedId(sourceId);
    // scrollIntoView is unimplemented in jsdom; the optional call no-ops in tests.
    refs.current.get(sourceId)?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  };

  return (
    <div className="turn">
      {/* The user's question, as a chat bubble aligned to the right. */}
      <div className="msg-row user">
        <div className="bubble">{question}</div>
      </div>
      {/* The assistant's grounded answer + its sources, aligned to the left. */}
      <div className="msg-row assistant">
        <div className="bubble">
          {result.not_in_vault && (
            <p className="not-in-vault" role="status">
              {t("queryTurn.notInVault")}
            </p>
          )}
          <AnswerText text={result.answer} citations={result.citations} onCite={onCite} />
          <SourceList
            sources={result.sources}
            citations={result.citations}
            highlightedId={highlightedId}
            registerRef={registerRef}
            repositoryId={repositoryId}
          />
        </div>
      </div>
    </div>
  );
}
