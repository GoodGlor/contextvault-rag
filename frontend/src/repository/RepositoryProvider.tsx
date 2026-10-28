import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { listAllRepositories, listRepositories, type Repository } from "../api/repositories";
import { useAuth } from "../auth/AuthContext";
import { RepositoryContext, type RepositoryContextValue } from "./RepositoryContext";

const STORAGE_KEY = "contextvault.currentRepo";

function readStored(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}
function writeStored(id: string): void {
  try {
    if (id) localStorage.setItem(STORAGE_KEY, id);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* localStorage may be unavailable; selection just won't persist */
  }
}

/** Owns the single "current repository" for the app. The sidebar switcher writes
 *  it; repo-scoped pages read it via useCurrentRepository().
 *
 *  Scope: workspace surfaces (Ask, Reports) are limited to repositories the user
 *  is GRANTED — matching the backend, which subjects even admins to grants for
 *  querying. Admin management surfaces (/admin/*, e.g. Data and Insights) may
 *  target ANY repository, so they show the full list. Members only ever have
 *  their granted list. */
export function RepositoryProvider({ children }: { children: ReactNode }): ReactNode {
  const { t } = useTranslation();
  const { session } = useAuth();
  const isAdmin = session?.role === "admin";
  const { pathname } = useLocation();
  const scope: "manage" | "workspace" = pathname.startsWith("/admin") ? "manage" : "workspace";

  const [granted, setGranted] = useState<Repository[]>([]);
  const [all, setAll] = useState<Repository[]>([]);
  const [currentRepoId, setCurrent] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load the accessible lists once per role. Members never call the admin
  // endpoint (they'd get 403), so their "all" mirrors their granted list.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load = isAdmin
      ? Promise.all([listRepositories(), listAllRepositories()])
      : listRepositories().then((g) => [g, g] as [Repository[], Repository[]]);
    load
      .then(([g, a]) => {
        if (cancelled) return;
        setGranted(g);
        setAll(a);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.detail : t("repository.errorLoad"));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isAdmin, t]);

  const repos = useMemo<Repository[]>(
    () => (scope === "manage" && isAdmin ? all : granted),
    [scope, isAdmin, all, granted],
  );

  // Keep the selection valid for the visible list. Reconciles only when the
  // current id falls outside the list (e.g. leaving a management page that showed
  // an ungranted repo for a workspace page). Persistence tracks explicit user
  // choices only (setCurrentRepoId), so an ungranted management selection is not
  // burned into storage.
  useEffect(() => {
    if (loading) return;
    if (currentRepoId !== "" && repos.some((r) => r.id === currentRepoId)) return;
    const stored = readStored();
    const next = repos.some((r) => r.id === stored) ? stored : (repos[0]?.id ?? "");
    setCurrent(next);
  }, [loading, repos, currentRepoId]);

  const setCurrentRepoId = useCallback((id: string) => {
    setCurrent(id);
    writeStored(id);
  }, []);

  const value = useMemo<RepositoryContextValue>(
    () => ({ repos, currentRepoId, setCurrentRepoId, loading, error }),
    [repos, currentRepoId, setCurrentRepoId, loading, error],
  );

  return <RepositoryContext.Provider value={value}>{children}</RepositoryContext.Provider>;
}
