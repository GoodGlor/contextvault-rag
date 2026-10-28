import { api } from "./client";
import type { Source } from "./sources";

/** Write an Admin Note — a verified, first-class source that answers a gap.
 *  It is ingested like an upload, so it returns a Source with a `pending` status. */
export function createAdminNote(
  repositoryId: string,
  input: { title: string; content: string },
): Promise<Source> {
  return api.post<Source>(`/repositories/${repositoryId}/admin-notes`, input);
}
