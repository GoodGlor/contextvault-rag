import { createContext, useContext } from "react";
import type { Repository } from "../api/repositories";

export interface RepositoryContextValue {
  /** Accessible repositories for the current role (granted for members, all for admins). */
  repos: Repository[];
  /** The repo every repo-scoped page reads; "" when there is none. */
  currentRepoId: string;
  setCurrentRepoId: (id: string) => void;
  loading: boolean;
  error: string | null;
}

export const RepositoryContext = createContext<RepositoryContextValue | null>(null);

/** Read the shared current-repository state. Throws if used outside the provider. */
export function useCurrentRepository(): RepositoryContextValue {
  const ctx = useContext(RepositoryContext);
  if (ctx === null) {
    throw new Error("useCurrentRepository must be used within a RepositoryProvider");
  }
  return ctx;
}
