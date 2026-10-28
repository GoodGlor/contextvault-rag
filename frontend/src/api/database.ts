import { api } from "./client";

// Mirrors the database-connection schemas in src/contextvault/api/database.py.
// A repository holds SEVERAL connections (#201), so these are collection routes
// under /repositories/{id}/databases. The password is write-only — no route ever
// returns it, so `DatabaseConnection` deliberately has no password field.

export type DatabaseType = "postgres" | "mysql";

export interface ExposedColumn {
  name: string;
  description: string;
}

export interface ExposedTable {
  table: string;
  description: string;
  columns: ExposedColumn[];
}

export interface DatabaseConnection {
  id: string;
  name: string;
  /** #202. Empty means never described — which is NOT the same as stale. */
  description: string;
  /** Derived server-side: the allow-list changed since the description was written. */
  description_stale: boolean;
  db_type: DatabaseType;
  host: string;
  port: number;
  database: string;
  username: string;
  exposed_schema: ExposedTable[];
}

/** Payload for `createDatabase` / `updateDatabase`. On update, `password`
 *  omitted/empty keeps the currently stored one and `exposed_schema` omitted
 *  keeps the currently stored allow-list; on create a password is required. */
export interface DatabaseConnectionPayload {
  name: string;
  db_type: DatabaseType;
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  exposed_schema?: ExposedTable[] | null;
}

/** Every connection this repository holds, name-ordered — never a password. */
export function listDatabases(repositoryId: string): Promise<DatabaseConnection[]> {
  return api.get<DatabaseConnection[]>(`/repositories/${repositoryId}/databases`);
}

/** Read one stored connection. 404 if it is not this repository's. */
export function getDatabase(
  repositoryId: string,
  connectionId: string,
): Promise<DatabaseConnection> {
  return api.get<DatabaseConnection>(`/repositories/${repositoryId}/databases/${connectionId}`);
}

/** Live-test then store a NEW connection. 409 if the name is already taken here. */
export function createDatabase(
  repositoryId: string,
  payload: DatabaseConnectionPayload,
): Promise<DatabaseConnection> {
  return api.post<DatabaseConnection>(`/repositories/${repositoryId}/databases`, payload);
}

/** Live-test then update an existing connection in place. */
export function updateDatabase(
  repositoryId: string,
  connectionId: string,
  payload: DatabaseConnectionPayload,
): Promise<DatabaseConnection> {
  return api.put<DatabaseConnection>(
    `/repositories/${repositoryId}/databases/${connectionId}`,
    payload,
  );
}

/** Save the admin's edited exposed-schema allow-list without re-testing the connection. */
export function patchSchema(
  repositoryId: string,
  connectionId: string,
  exposedSchema: ExposedTable[],
  language?: string,
): Promise<DatabaseConnection> {
  return api.patch<DatabaseConnection>(
    `/repositories/${repositoryId}/databases/${connectionId}/schema`,
    // The interface language travels with the save because the save is what
    // queues the description (#212/#215), and a background task has nobody to
    // ask. The server degrades an absent or unknown value rather than refusing.
    { exposed_schema: exposedSchema, language },
  );
}

/** Remove one connection; the reports/schedules naming it cascade with it. The
 *  repository's other connections are untouched. */
export function deleteDatabase(repositoryId: string, connectionId: string): Promise<void> {
  return api.del<void>(`/repositories/${repositoryId}/databases/${connectionId}`);
}

/** Generate this connection's description from its allow-list (#202).
 *
 *  Since #212 this also happens on its own after an allow-list save; this stays
 *  as the manual retry, and as the one path that surfaces a provider's reason
 *  for refusing. Written in `language`, the reader's interface language (#215). */
export function describeDatabase(
  repositoryId: string,
  connectionId: string,
  language?: string,
): Promise<DatabaseConnection> {
  const query = language === undefined ? "" : `?language=${encodeURIComponent(language)}`;
  return api.post<DatabaseConnection>(
    `/repositories/${repositoryId}/databases/${connectionId}/describe${query}`,
  );
}

/** Correct a description by hand, without re-testing the connection. A wrong
 *  description misroutes a report in #203, so this is not a nicety. */
export function patchDescription(
  repositoryId: string,
  connectionId: string,
  description: string,
): Promise<DatabaseConnection> {
  return api.patch<DatabaseConnection>(
    `/repositories/${repositoryId}/databases/${connectionId}/description`,
    { description },
  );
}

export interface IntrospectResult {
  schema: ExposedTable[];
}

/** Live-read tables/columns from one stored connection, for the admin to annotate
 *  and save as the exposed-schema allow-list (`patchSchema`). */
export function introspect(repositoryId: string, connectionId: string): Promise<IntrospectResult> {
  return api.post<IntrospectResult>(
    `/repositories/${repositoryId}/databases/${connectionId}/introspect`,
  );
}
