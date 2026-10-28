import { api } from "./client";

// Mirrors KnowledgeGapResponse in src/contextvault/api/knowledge_gaps.py.
export interface KnowledgeGap {
  question: string;
  ask_count: number;
  user_count: number;
  last_asked_at: string;
}

/** Ranked knowledge gaps for a repository (admin-only): questions the vault
 *  could not answer, aggregated and most-asked first. */
export function listKnowledgeGaps(repositoryId: string, limit = 50): Promise<KnowledgeGap[]> {
  return api.get<KnowledgeGap[]>(`/repositories/${repositoryId}/knowledge-gaps?limit=${limit}`);
}

// Mirrors RejectedGapResponse in src/contextvault/api/knowledge_gaps.py.
export interface GapRejection {
  question: string;
  reason: string;
  rejected_by: string | null;
  rejected_at: string;
}

/** Reject a knowledge gap (admin-only): record why it won't be answered. */
export function rejectGap(
  repositoryId: string,
  body: { question: string; reason: string },
): Promise<GapRejection> {
  return api.post<GapRejection>(`/repositories/${repositoryId}/knowledge-gaps/reject`, body);
}

/** Previously rejected gaps for a repository (admin-only). */
export function listRejectedGaps(repositoryId: string): Promise<GapRejection[]> {
  return api.get<GapRejection[]>(`/repositories/${repositoryId}/knowledge-gaps/rejected`);
}
