import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { LLM_PROVIDERS, type LLMProvider } from "../api/repositories";
import {
  deleteProviderKey,
  listProviders,
  setProviderKey,
  type ProviderStatus,
} from "../api/providers";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

const PROVIDER_LABEL: Record<LLMProvider, string> = Object.fromEntries(
  LLM_PROVIDERS.map((p) => [p.value, p.label]),
) as Record<LLMProvider, string>;

/** Admin surface for the global provider API keys: enter one key per provider; each is
 *  verified against the live provider on save, then shared by every repo that uses it. */
export function AdminProvidersPage(): ReactNode {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listProviders()
      .then((rows) => !cancelled && setProviders(rows))
      .catch(
        (err: unknown) =>
          !cancelled && setLoadError(errorMessage(err, t("providers.failedToLoad"))),
      );
    return () => {
      cancelled = true;
    };
  }, [t]);

  const upsert = (updated: ProviderStatus) =>
    setProviders(
      (prev) => prev?.map((p) => (p.provider === updated.provider ? updated : p)) ?? prev,
    );

  if (loadError !== null) return <p className="error">{loadError}</p>;
  if (providers === null) return <p>{t("providers.loading")}</p>;

  return (
    <section className="admin-providers">
      <h1>{t("providers.title")}</h1>
      <p className="providers-intro">{t("providers.intro")}</p>
      <ul className="provider-list">
        {providers.map((p) => (
          <ProviderRow key={p.provider} status={p} onChanged={upsert} />
        ))}
      </ul>
    </section>
  );
}

/** One provider row: enter/replace its key (verified on save) or clear it. */
function ProviderRow({
  status,
  onChanged,
}: {
  status: ProviderStatus;
  onChanged: (updated: ProviderStatus) => void;
}): ReactNode {
  const { t } = useTranslation();
  const isCustom = status.provider === "custom";
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(status.base_url ?? "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSave = isCustom ? baseUrl.trim() !== "" : apiKey.trim() !== "";

  const onSave = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await setProviderKey(status.provider, {
        apiKey: apiKey.trim() || undefined,
        baseUrl: isCustom ? baseUrl.trim() : undefined,
      });
      onChanged(updated);
      setApiKey("");
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err, t("providers.couldNotSave")));
    } finally {
      setSaving(false);
    }
  };

  const onRemove = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await deleteProviderKey(status.provider);
      setBaseUrl("");
      onChanged({
        ...status,
        configured: false,
        verified: false,
        api_key_masked: null,
        base_url: null,
      });
    } catch (err) {
      setError(errorMessage(err, t("providers.couldNotRemove")));
    } finally {
      setSaving(false);
    }
  };

  const keyId = `provider-key-${status.provider}`;
  const urlId = `provider-url-${status.provider}`;

  return (
    <li className="provider-item">
      <form className="provider-form" onSubmit={onSave}>
        <div className="provider-head">
          <span className="provider-name">{PROVIDER_LABEL[status.provider]}</span>
          <span className={status.verified ? "badge configured" : "badge unconfigured"}>
            {status.verified ? t("providers.verified") : t("providers.notSet")}
          </span>
          {status.api_key_masked !== null && (
            <span className="current-key">
              {t("providers.currentKey", { value: status.api_key_masked })}
            </span>
          )}
        </div>

        {isCustom && (
          <>
            <label htmlFor={urlId}>{t("providers.baseUrl")}</label>
            <input
              id={urlId}
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={t("providers.baseUrlPlaceholder")}
            />
            <p className="notice">{t("providers.customEmbeddingsNote")}</p>
          </>
        )}

        <label htmlFor={keyId}>
          {isCustom ? t("providers.apiKeyOptional") : t("providers.apiKey")}
        </label>
        <input
          id={keyId}
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={status.configured ? t("providers.replacePlaceholder") : ""}
        />
        <button type="submit" disabled={saving || !canSave}>
          {saving ? t("providers.saving") : t("providers.saveKey")}
        </button>
        {status.configured && (
          <button type="button" onClick={onRemove} disabled={saving}>
            {t("providers.removeKey")}
          </button>
        )}
        {saved && <p className="success">{t("providers.saved")}</p>}
        {error !== null && <p className="error">{error}</p>}
      </form>
    </li>
  );
}
