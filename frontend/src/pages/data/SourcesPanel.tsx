import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../../api/client";
import { useCurrentRepository } from "../../repository/RepositoryContext";
import {
  addWebSource,
  deleteSource,
  ingestProgressPercent,
  isIngesting,
  listSources,
  uploadSource,
  type Source,
} from "../../api/sources";

export const SOURCE_POLL_MS = 2000;

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** Embed-stage progress for a source still being ingested.
 *
 * Renders nothing once ingestion has settled, and nothing for sources that cannot
 * report progress (ingested before the counters existed) — there the status badge
 * remains the whole story. A native `<progress>` carries the accessible role and
 * value without hand-rolled ARIA.
 */
function IngestProgress({ source }: { source: Source }): ReactNode {
  const { t } = useTranslation();
  if (!isIngesting(source.status)) return null;
  const percent = ingestProgressPercent(source);
  if (percent === null) return null;
  return (
    <span className="ingest-progress">
      <progress value={percent} max={100} />
      <span className="ingest-progress-label">{t("adminSources.progress", { percent })}</span>
    </span>
  );
}

/** Documents & web sources for the current repository: upload, watch ingestion,
 *  delete. Repo comes from the shared switcher. */
export function SourcesPanel(): ReactNode {
  const { t } = useTranslation();
  const { currentRepoId } = useCurrentRepository();

  const [sources, setSources] = useState<Source[] | null>(null);
  const [pollTick, setPollTick] = useState(0);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [webUrl, setWebUrl] = useState("");
  const [addingWeb, setAddingWeb] = useState(false);
  const [webError, setWebError] = useState<string | null>(null);

  useEffect(() => {
    if (currentRepoId === "") return;
    let cancelled = false;
    setSources(null);
    setSourcesError(null);
    listSources(currentRepoId)
      .then((s) => !cancelled && setSources(s))
      .catch(
        (err: unknown) =>
          !cancelled && setSourcesError(errorMessage(err, t("adminSources.errorLoadSources"))),
      );
    return () => {
      cancelled = true;
    };
  }, [currentRepoId, t]);

  // Poll while anything is still ingesting. `pollTick` advances after every attempt,
  // successful or not, and that is what re-arms this effect: keying the reschedule
  // solely off `sources` meant a single failed request ended polling for good, so a
  // status that changed afterwards only appeared on a manual page refresh.
  useEffect(() => {
    if (currentRepoId === "" || sources === null) return;
    if (!sources.some((s) => isIngesting(s.status))) return;
    const timer = setTimeout(() => {
      listSources(currentRepoId)
        .then(setSources)
        .catch(() => {
          /* transient; the tick below still re-arms the poll */
        })
        .finally(() => setPollTick((n) => n + 1));
    }, SOURCE_POLL_MS);
    return () => clearTimeout(timer);
  }, [sources, pollTick, currentRepoId]);

  const onFileChange = (e: ChangeEvent<HTMLInputElement>) =>
    setFiles(Array.from(e.target.files ?? []));

  const onUpload = async (e: FormEvent) => {
    e.preventDefault();
    if (currentRepoId === "" || files.length === 0) return;
    setUploading(true);
    setUploadError(null);
    const results = await Promise.allSettled(files.map((f) => uploadSource(currentRepoId, f)));
    const created = results
      .filter((r): r is PromiseFulfilledResult<Source> => r.status === "fulfilled")
      .map((r) => r.value);
    if (created.length > 0) setSources((prev) => [...(prev ?? []), ...created]);
    const failed = results.length - created.length;
    if (failed > 0) {
      const firstRejected = results.find((r) => r.status === "rejected") as
        PromiseRejectedResult | undefined;
      const detail =
        created.length === 0 && firstRejected?.reason instanceof ApiError
          ? firstRejected.reason.detail
          : null;
      setUploadError(
        detail ?? t("adminSources.errorUploadSome", { failed, total: results.length }),
      );
    } else {
      setFiles([]);
      if (fileInput.current) fileInput.current.value = "";
    }
    setUploading(false);
  };

  const onAddWeb = async (e: FormEvent) => {
    e.preventDefault();
    if (currentRepoId === "" || webUrl.trim() === "") return;
    setAddingWeb(true);
    setWebError(null);
    try {
      const created = await addWebSource(currentRepoId, webUrl.trim());
      setSources((prev) => [...(prev ?? []), created]);
      setWebUrl("");
    } catch (err) {
      setWebError(errorMessage(err, t("adminSources.errorAddLink")));
    } finally {
      setAddingWeb(false);
    }
  };

  const onDelete = async (id: string) => {
    try {
      await deleteSource(id);
      setSources((prev) => prev?.filter((s) => s.id !== id) ?? prev);
    } catch (err) {
      setSourcesError(errorMessage(err, t("adminSources.errorDelete")));
    }
  };

  if (currentRepoId === "") return <p>{t("adminSources.noRepos")}</p>;

  const statusLabels: Record<Source["status"], string> = {
    pending: t("adminSources.statusPending"),
    processing: t("adminSources.statusProcessing"),
    done: t("adminSources.statusDone"),
    failed: t("adminSources.statusFailed"),
  };

  return (
    <section className="admin-sources">
      <form className="source-upload" onSubmit={onUpload}>
        <label htmlFor="source-file">{t("adminSources.documentLabel")}</label>
        <input
          id="source-file"
          type="file"
          multiple
          ref={fileInput}
          onChange={onFileChange}
          accept=".txt,.pdf,.docx,.png,.jpg,.jpeg,.webp,.tiff,.bmp,.heic,.heif"
        />
        <p className="form-hint">{t("adminSources.ocrHint")}</p>
        <button type="submit" disabled={uploading || files.length === 0}>
          {files.length > 1
            ? t("adminSources.uploadButtonCount", { n: files.length })
            : t("adminSources.uploadButton")}
        </button>
        {uploadError !== null && <p className="error">{uploadError}</p>}
      </form>

      <form className="source-web" onSubmit={onAddWeb}>
        <label htmlFor="source-url">{t("adminSources.webLinkLabel")}</label>
        <input
          id="source-url"
          type="url"
          placeholder={t("adminSources.urlPlaceholder")}
          value={webUrl}
          onChange={(e) => setWebUrl(e.target.value)}
        />
        <button type="submit" disabled={addingWeb || webUrl.trim() === ""}>
          {t("adminSources.addLinkButton")}
        </button>
        {webError !== null && <p className="error">{webError}</p>}
      </form>

      {sourcesError !== null && <p className="error">{sourcesError}</p>}
      {sources === null ? (
        <p>{t("adminSources.loadingSources")}</p>
      ) : sources.length === 0 ? (
        <p>{t("adminSources.noSources")}</p>
      ) : (
        <ul className="source-list">
          {sources.map((s) => (
            <li key={s.id} className="source-item">
              <span className={`badge kind-${s.kind}`}>{s.kind}</span>
              {s.kind === "web" && s.source_url !== null ? (
                <a className="source-title" href={s.source_url} target="_blank" rel="noreferrer">
                  {s.title}
                </a>
              ) : (
                <span className="source-title">{s.title}</span>
              )}
              <span className={`badge status-${s.status}`}>{statusLabels[s.status]}</span>
              <IngestProgress source={s} />
              {s.status === "failed" && s.ingest_error !== null && (
                <span className="source-error">{s.ingest_error}</span>
              )}
              <button type="button" onClick={() => onDelete(s.id)}>
                {t("adminSources.deleteButton")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
