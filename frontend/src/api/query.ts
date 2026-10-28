import { api } from "./client";
import type { SourceKind } from "./sources";

// Mirrors the query schemas in src/contextvault/api/query.py.

export type { SourceKind };

export interface Citation {
  number: number;
  chunk_id: string;
  source_id: string;
  char_start: number | null;
  char_end: number | null;
}

export interface SourceReference {
  id: string;
  title: string;
  original_filename: string | null;
  kind: SourceKind;
  verified: boolean;
  author: string | null;
}

export interface QueryResult {
  answer: string;
  not_in_vault: boolean;
  citations: Citation[];
  sources: SourceReference[];
}

/** Ask a question against one repository; returns the grounded answer + citations.
 *
 * The backend resolves follow-up references using this user's saved conversation
 * history for the repository server-side — the client no longer sends it. */
export function queryRepository(repositoryId: string, question: string): Promise<QueryResult> {
  return api.post<QueryResult>(`/repositories/${repositoryId}/query`, { question });
}
