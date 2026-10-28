import { api } from "./client";

// Mirrors GrantResponse in src/contextvault/api/grants.py.
export interface Grant {
  id: string;
  user_id: string;
  repository_id: string;
  expires_at: string | null;
}

/** List every grant on a repository, including expired ones (admin view). */
export function listGrants(repositoryId: string): Promise<Grant[]> {
  return api.get<Grant[]>(`/repositories/${repositoryId}/grants`);
}

/** Grant a user access to a repository, optionally time-boxed (idempotent). */
export function grantAccess(
  repositoryId: string,
  input: { user_id: string; expires_at?: string | null },
): Promise<Grant> {
  return api.post<Grant>(`/repositories/${repositoryId}/grants`, input);
}

/** Revoke a user's access to a repository. */
export function revokeAccess(repositoryId: string, userId: string): Promise<void> {
  return api.del<void>(`/repositories/${repositoryId}/grants/${userId}`);
}
