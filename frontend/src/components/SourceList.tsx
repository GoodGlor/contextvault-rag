import { useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import type { Citation, SourceReference } from "../api/query";
import { getSourceContent } from "../api/sources";

/** Format the citation character spans that point at a given source (raw, unlabeled). */
function spansFor(sourceId: string, citations: Citation[]): string {
  const spans = citations
    .filter((c) => c.source_id === sourceId && c.char_start !== null && c.char_end !== null)
    .map((c) => `${c.char_start}–${c.char_end}`);
  return spans.join(", ");
}

/** Map each source to the citation numbers that reference it, in ascending order. */
function numbersFor(sourceId: string, citations: Citation[]): number[] {
  return citations
    .filter((c) => c.source_id === sourceId)
    .map((c) => c.number)
    .sort((a, b) => a - b);
}

/**
 * The cited sources for one answer. `highlightedId` (set when the user clicks an
 * inline citation) marks the matching entry; each entry carries a ref so the page
 * can scroll it into view. Each source can also load its raw passage on demand from
 * the user-scoped source-content endpoint.
 */
export function SourceList({
  sources,
  citations,
  highlightedId,
  registerRef,
  repositoryId,
}: {
  sources: SourceReference[];
  citations: Citation[];
  highlightedId: string | null;
  registerRef: (id: string, el: HTMLLIElement | null) => void;
  repositoryId: string;
}): ReactNode {
  const { t } = useTranslation();
  if (sources.length === 0) return null;
  return (
    <div className="sources">
      <h3>{t("sourceList.sources")}</h3>
      <ul>
        {sources.map((source) => (
          <SourceItem
            key={source.id}
            source={source}
            citations={citations}
            highlighted={source.id === highlightedId}
            registerRef={registerRef}
            repositoryId={repositoryId}
          />
        ))}
      </ul>
    </div>
  );
}

function SourceItem({
  source,
  citations,
  highlighted,
  registerRef,
  repositoryId,
}: {
  source: SourceReference;
  citations: Citation[];
  highlighted: boolean;
  registerRef: (id: string, el: HTMLLIElement | null) => void;
  repositoryId: string;
}): ReactNode {
  const { t } = useTranslation();
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onView = async () => {
    if (content !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await getSourceContent(repositoryId, source.id);
      setContent(result.content ?? t("sourceList.noStoredText"));
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("sourceList.couldNotLoad"));
    } finally {
      setLoading(false);
    }
  };

  const spans = spansFor(source.id, citations);

  return (
    <li
      ref={(el) => registerRef(source.id, el)}
      className={highlighted ? "source-item highlighted" : "source-item"}
      data-testid={`source-${source.id}`}
    >
      <span className="source-numbers">
        {numbersFor(source.id, citations).map((n) => (
          <span key={n} className="citation-chip static">
            {n}
          </span>
        ))}
      </span>
      <span className="source-body">
        <span className="source-title">
          {source.title}
          {source.verified && <span className="verified-badge">{t("sourceList.verified")}</span>}
        </span>
        <span className="source-meta">
          {source.author ? `${t("sourceList.by", { author: source.author })} · ` : ""}
          {source.original_filename ?? source.kind}
          {spans ? ` · ${t("sourceList.chars", { spans })}` : ""}
        </span>
        <button type="button" className="view-passage" onClick={onView} disabled={loading}>
          {loading ? t("common.loading") : t("sourceList.viewPassage")}
        </button>
        {content !== null && <p className="source-passage">{content}</p>}
        {error !== null && <p className="error">{error}</p>}
      </span>
    </li>
  );
}
