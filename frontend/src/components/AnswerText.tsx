import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { Citation } from "../api/query";
import { parseAnswer } from "../query/parseAnswer";

/**
 * Render an answer with its inline `[n]` markers turned into clickable citation
 * chips. Clicking one calls `onCite(sourceId)` so the page can highlight and scroll
 * to the matching source.
 */
export function AnswerText({
  text,
  citations,
  onCite,
}: {
  text: string;
  citations: Citation[];
  onCite: (sourceId: string) => void;
}): ReactNode {
  const { t } = useTranslation();
  const bySource = new Map(citations.map((c) => [c.number, c.source_id]));
  const known = new Set(citations.map((c) => c.number));
  const segments = parseAnswer(text, known);

  return (
    <p className="answer-text">
      {segments.map((seg, i) =>
        seg.type === "text" ? (
          <span key={i}>{seg.value}</span>
        ) : (
          <button
            key={i}
            type="button"
            className="citation-chip"
            aria-label={t("answerText.jumpToSource", { number: seg.number })}
            onClick={() => {
              const sourceId = bySource.get(seg.number);
              if (sourceId !== undefined) onCite(sourceId);
            }}
          >
            {seg.number}
          </button>
        ),
      )}
    </p>
  );
}
