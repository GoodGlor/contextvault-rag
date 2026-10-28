import { api } from "./client";
import type { Citation, SourceReference } from "./query";

/** One saved exchange; mirrors ConversationTurnResponse in api/conversations.py. */
export interface SavedTurn {
  question: string;
  answer: string;
  not_in_vault: boolean;
  citations: Citation[];
  sources: SourceReference[];
}

export interface SavedConversation {
  turns: SavedTurn[];
}

/** This user's saved conversation for a repository (empty turns when none yet). */
export function getConversation(repositoryId: string): Promise<SavedConversation> {
  return api.get<SavedConversation>(`/repositories/${repositoryId}/conversation`);
}

/** Clear this user's saved conversation for a repository. */
export function clearConversation(repositoryId: string): Promise<void> {
  return api.del<void>(`/repositories/${repositoryId}/conversation`);
}
