import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import {
  createRepository,
  deleteRepository,
  getLlmConfig,
  listAllRepositories,
  listModels,
  setLlmConfig,
  updateRepository,
  LLM_PROVIDERS,
  type AdminRepository,
  type LLMConfig,
  type LLMProvider,
} from "../api/repositories";
import { listProviders } from "../api/providers";
import { RepositoryTemplateField } from "./reports/RepositoryTemplateField";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** Admin surface for repositories: create them and configure each one's LLM. */
export function AdminRepositoriesPage(): ReactNode {
  const { t } = useTranslation();
  const [repos, setRepos] = useState<AdminRepository[] | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listAllRepositories()
      .then((rs) => !cancelled && setRepos(rs))
      .catch(
        (err: unknown) =>
          !cancelled && setReposError(errorMessage(err, t("repositories.failedToLoad"))),
      );
    return () => {
      cancelled = true;
    };
  }, [t]);

  const onCreate = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") return;
    setCreating(true);
    setCreateError(null);
    try {
      const repo = await createRepository({
        name: trimmed,
        description: description.trim() === "" ? null : description.trim(),
      });
      setRepos((prev) => [...(prev ?? []), repo]);
      setName("");
      setDescription("");
    } catch (err) {
      setCreateError(errorMessage(err, t("repositories.couldNotCreate")));
    } finally {
      setCreating(false);
    }
  };

  /** Replace a repo in the list (after rename / config change). */
  const upsert = (updated: AdminRepository) =>
    setRepos((prev) => prev?.map((r) => (r.id === updated.id ? updated : r)) ?? prev);
  const remove = (id: string) => setRepos((prev) => prev?.filter((r) => r.id !== id) ?? prev);

  if (reposError !== null) {
    return <p className="error">{reposError}</p>;
  }
  if (repos === null) {
    return <p>{t("repositories.loadingRepositories")}</p>;
  }

  return (
    <section className="admin-repos">
      <h1>{t("repositories.title")}</h1>

      <form className="repo-create" onSubmit={onCreate}>
        <h2>{t("repositories.newRepository")}</h2>
        <label htmlFor="repo-name">{t("repositories.repositoryName")}</label>
        <input id="repo-name" value={name} onChange={(e) => setName(e.target.value)} required />
        <label htmlFor="repo-description">{t("repositories.description")}</label>
        <input
          id="repo-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <button type="submit" disabled={creating || name.trim() === ""}>
          {t("repositories.createRepository")}
        </button>
        {createError !== null && <p className="error">{createError}</p>}
      </form>

      {repos.length === 0 ? (
        <p>{t("repositories.emptyState")}</p>
      ) : (
        <ul className="repo-list">
          {repos.map((repo) => (
            <RepoItem
              key={repo.id}
              repo={repo}
              onChanged={upsert}
              onDeleted={() => remove(repo.id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/** One repository row: configure its LLM, rename it, or delete it. */
function RepoItem({
  repo,
  onChanged,
  onDeleted,
}: {
  repo: AdminRepository;
  onChanged: (updated: AdminRepository) => void;
  onDeleted: () => void;
}): ReactNode {
  const { t } = useTranslation();
  const [configuring, setConfiguring] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(repo.name);
  const [description, setDescription] = useState(repo.description ?? "");
  const [saving, setSaving] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onRename = async (e: FormEvent) => {
    e.preventDefault();
    if (name.trim() === "") return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateRepository(repo.id, {
        name: name.trim(),
        description: description.trim() === "" ? null : description.trim(),
      });
      onChanged(updated);
      setRenaming(false);
    } catch (err) {
      setError(errorMessage(err, t("repositories.couldNotUpdate")));
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async () => {
    if (confirmName !== repo.name) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteRepository(repo.id, confirmName);
      onDeleted();
    } catch (err) {
      setError(errorMessage(err, t("repositories.couldNotDelete")));
      setDeleting(false);
    }
  };

  return (
    <li className="repo-item">
      <div className="repo-head">
        <span className="repo-name">{repo.name}</span>
        {repo.description !== null && <span className="repo-description">{repo.description}</span>}
        <span className={repo.configured ? "badge configured" : "badge unconfigured"}>
          {repo.configured ? t("repositories.configured") : t("repositories.notConfigured")}
        </span>
        <button type="button" onClick={() => setConfiguring((v) => !v)}>
          {t("repositories.configure")}
        </button>
        <button type="button" onClick={() => setRenaming((v) => !v)}>
          {t("repositories.rename")}
        </button>
        {!confirmingDelete ? (
          <button type="button" onClick={() => setConfirmingDelete(true)}>
            {t("repositories.delete")}
          </button>
        ) : (
          <span className="confirm-delete">
            <label htmlFor={`repo-confirm-${repo.id}`}>{t("repositories.confirmName")}</label>
            <input
              id={`repo-confirm-${repo.id}`}
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
            />
            <button
              type="button"
              onClick={onDelete}
              disabled={deleting || confirmName !== repo.name}
            >
              {t("repositories.confirmDelete")}
            </button>
          </span>
        )}
      </div>

      {renaming && (
        <form className="repo-rename" onSubmit={onRename}>
          <label htmlFor={`repo-name-${repo.id}`}>{t("repositories.name")}</label>
          <input
            id={`repo-name-${repo.id}`}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <label htmlFor={`repo-desc-${repo.id}`}>{t("repositories.description")}</label>
          <input
            id={`repo-desc-${repo.id}`}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          <button type="submit" disabled={saving || name.trim() === ""}>
            {t("repositories.save")}
          </button>
        </form>
      )}

      {error !== null && <p className="error">{error}</p>}

      {configuring && (
        <>
          <RepoConfigPanel
            repository={repo}
            onConfigured={() => onChanged({ ...repo, configured: true })}
          />
          <RepositoryTemplateField repositoryId={repo.id} />
        </>
      )}
    </li>
  );
}

/** Pick the model a repository answers with. Keys are global (Providers tab); here you
 *  only choose a provider that already has a verified key, then a model from it. */
function RepoConfigPanel({
  repository,
  onConfigured,
}: {
  repository: AdminRepository;
  onConfigured: () => void;
}): ReactNode {
  const { t } = useTranslation();
  const [config, setConfig] = useState<LLMConfig | null>(null);
  // Which providers have a verified key — the only ones a repo can pick from.
  const [verified, setVerified] = useState<Set<LLMProvider> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [provider, setProvider] = useState<LLMProvider>(LLM_PROVIDERS[0].value);
  const [model, setModel] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);

  // Load the repo's current model choice alongside which providers are usable.
  useEffect(() => {
    let cancelled = false;
    Promise.all([getLlmConfig(repository.id), listProviders()])
      .then(([cfg, provs]) => {
        if (cancelled) return;
        setConfig(cfg);
        const verifiedSet = new Set(provs.filter((p) => p.verified).map((p) => p.provider));
        setVerified(verifiedSet);
        // Default to the repo's current provider, else the first verified one.
        setProvider(cfg.provider ?? [...verifiedSet][0] ?? LLM_PROVIDERS[0].value);
        if (cfg.model !== null) setModel(cfg.model);
      })
      .catch(
        (err: unknown) =>
          !cancelled && setLoadError(errorMessage(err, t("repositories.failedToLoadConfig"))),
      );
    return () => {
      cancelled = true;
    };
  }, [repository.id, t]);

  // Auto-load the model list whenever the selected provider has a verified key — no
  // key entry needed here, the provider's global key is used server-side.
  useEffect(() => {
    if (verified === null || !verified.has(provider)) return;
    let cancelled = false;
    setLoadingModels(true);
    setModelsError(null);
    listModels(repository.id, { provider })
      .then((res) => {
        if (cancelled) return;
        setModels(res.models);
        if (res.models.length === 0) setModelsError(t("repositories.noModels"));
      })
      .catch(
        (err: unknown) =>
          !cancelled && setModelsError(errorMessage(err, t("repositories.couldNotLoadModels"))),
      )
      .finally(() => !cancelled && setLoadingModels(false));
    return () => {
      cancelled = true;
    };
  }, [verified, provider, repository.id, t]);

  const onLoadModels = async () => {
    setLoadingModels(true);
    setModelsError(null);
    try {
      const result = await listModels(repository.id, { provider });
      setModels(result.models);
      if (result.models.length === 0) setModelsError(t("repositories.noModels"));
    } catch (err) {
      setModelsError(errorMessage(err, t("repositories.couldNotLoadModels")));
    } finally {
      setLoadingModels(false);
    }
  };

  if (loadError !== null) {
    return <p className="error">{loadError}</p>;
  }
  if (config === null || verified === null) {
    return <p>{t("repositories.loadingConfiguration")}</p>;
  }
  // Nothing can be picked until at least one provider has a key.
  if (verified.size === 0) {
    return <p className="notice">{t("repositories.noVerifiedProviders")}</p>;
  }

  const providerVerified = verified.has(provider);
  // The single model field's options: the fetched models, plus the current model so
  // it always shows even before the list loads (or if it's since been retired).
  const modelOptions = model && !models.includes(model) ? [model, ...models] : models;

  const onSave = async (e: FormEvent) => {
    e.preventDefault();
    if (model.trim() === "" || !providerVerified) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const updated = await setLlmConfig(repository.id, { provider, model: model.trim() });
      setConfig(updated);
      setSaved(true);
      onConfigured();
    } catch (err) {
      setSaveError(errorMessage(err, t("repositories.couldNotSave")));
    } finally {
      setSaving(false);
    }
  };

  const providerId = `provider-${repository.id}`;
  const modelSelectId = `model-${repository.id}`;

  return (
    <form className="repo-config" onSubmit={onSave}>
      <label htmlFor={providerId}>{t("repositories.provider")}</label>
      <select
        id={providerId}
        value={provider}
        onChange={(e) => {
          const next = e.target.value as LLMProvider;
          if (next === provider) return; // re-selecting the same provider is a no-op
          setProvider(next);
          // The old list belongs to the previous provider — clear it (the auto-load
          // effect refetches for the new one). Keep the model only when returning to
          // the configured provider; otherwise a new one is picked.
          setModels([]);
          setModelsError(null);
          setModel(config.provider === next ? (config.model ?? "") : "");
        }}
      >
        {LLM_PROVIDERS.map((p) => (
          <option key={p.value} value={p.value} disabled={!verified.has(p.value)}>
            {verified.has(p.value) ? p.label : `${p.label} — ${t("repositories.providerNoKey")}`}
          </option>
        ))}
      </select>

      {!providerVerified && <p className="notice">{t("repositories.selectedProviderNoKey")}</p>}

      {/* Cloud providers pick from the fetched catalogue; a custom OpenAI-compatible
          endpoint takes a free-typed model id (local names are arbitrary), with any
          fetched ids offered as datalist suggestions. */}
      {provider === "custom" ? (
        <>
          <label htmlFor={modelSelectId}>{t("repositories.model")}</label>
          <input
            id={modelSelectId}
            list={`${modelSelectId}-list`}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={t("repositories.modelManualPlaceholder")}
            required
          />
          <datalist id={`${modelSelectId}-list`}>
            {models.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </>
      ) : (
        modelOptions.length > 0 && (
          <>
            <label htmlFor={modelSelectId}>{t("repositories.model")}</label>
            <select
              id={modelSelectId}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              required
            >
              {!modelOptions.includes(model) && (
                <option value="">{t("repositories.chooseModelPlaceholder")}</option>
              )}
              {modelOptions.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </>
        )
      )}
      <button type="button" onClick={onLoadModels} disabled={loadingModels || !providerVerified}>
        {loadingModels ? t("repositories.loadingModels") : t("repositories.loadModels")}
      </button>
      {modelsError !== null && <p className="error">{modelsError}</p>}

      <button type="submit" disabled={saving || model.trim() === "" || !providerVerified}>
        {t("repositories.saveConfiguration")}
      </button>
      {saved && <p className="success">{t("repositories.configurationSaved")}</p>}
      {saveError !== null && <p className="error">{saveError}</p>}
    </form>
  );
}
