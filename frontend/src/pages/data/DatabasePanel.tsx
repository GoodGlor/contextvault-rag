import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../../api/client";
import { useCurrentRepository } from "../../repository/RepositoryContext";
import {
  createDatabase,
  deleteDatabase,
  describeDatabase,
  getDatabase,
  introspect,
  listDatabases,
  patchDescription,
  patchSchema,
  type DatabaseConnection,
  type DatabaseType,
  type ExposedTable,
} from "../../api/database";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** One editable column row in the allow-list editor: whether it's included, and
 *  the description the report LLM will see for it. */
interface EditableColumn {
  name: string;
  description: string;
  checked: boolean;
}

/** One editable table row, holding its own included columns. */
interface EditableTable {
  table: string;
  description: string;
  checked: boolean;
  columns: EditableColumn[];
}

/** Seed the editor from a fresh introspect result, carrying over any
 *  descriptions/inclusion already saved in the current allow-list so
 *  re-introspecting doesn't discard prior curation. */
function toEditable(introspected: ExposedTable[], saved: ExposedTable[]): EditableTable[] {
  return introspected.map((table) => {
    const savedTable = saved.find((s) => s.table === table.table);
    return {
      table: table.table,
      description: savedTable?.description ?? table.description,
      checked: savedTable !== undefined,
      columns: table.columns.map((col) => {
        const savedCol = savedTable?.columns.find((c) => c.name === col.name);
        return {
          name: col.name,
          description: savedCol?.description ?? col.description,
          checked: savedCol !== undefined,
        };
      }),
    };
  });
}

/** Only the checked tables/columns — this is what gets saved as the allow-list. */
function toExposedSchema(tables: EditableTable[]): ExposedTable[] {
  return tables
    .filter((t) => t.checked)
    .map((t) => ({
      table: t.table,
      description: t.description,
      columns: t.columns
        .filter((c) => c.checked)
        .map((c) => ({ name: c.name, description: c.description })),
    }));
}

/** Reporting-database connections for the current repository: connect several,
 *  introspect one's live schema, curate its allow-list, or disconnect it.
 *
 *  A repository held exactly one connection until #201, so this panel was a
 *  single form with a single summary. It is now a list plus a selection: the
 *  curation UI below is unchanged, it just operates on the selected connection
 *  rather than on "the" connection. */
export function DatabasePanel(): ReactNode {
  const { t, i18n } = useTranslation();
  const { currentRepoId } = useCurrentRepository();

  // `undefined` = still loading. An empty array is a repository with none.
  const [connections, setConnections] = useState<DatabaseConnection[] | undefined>(undefined);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  // Setup-form fields.
  const [name, setName] = useState("");
  const [dbType, setDbType] = useState<DatabaseType>("postgres");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [database, setDatabase] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  // Allow-list editor (populated once "Introspect" has been run).
  const [schema, setSchema] = useState<EditableTable[] | null>(null);
  // Tables whose columns are shown. An introspect can return dozens of columns
  // per table (Postgres ships pg_stat_statements with ~45), so every table starts
  // collapsed to a single row and the admin opens the ones they want to curate.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [introspecting, setIntrospecting] = useState(false);
  const [introspectError, setIntrospectError] = useState<string | null>(null);
  const [savingSchema, setSavingSchema] = useState(false);
  const [schemaSaved, setSchemaSaved] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Description (#202). Held as its own draft so an admin can correct a
  // generated one before saving; seeded from the selected connection.
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [describing, setDescribing] = useState(false);
  const [describeError, setDescribeError] = useState<string | null>(null);
  const [savingDescription, setSavingDescription] = useState(false);
  const [descriptionSaved, setDescriptionSaved] = useState(false);
  // #212: the description is written by a background task after the allow-list
  // saves, so the panel has to look again rather than trust the save's answer.
  const [writingDescription, setWritingDescription] = useState(false);

  // (Re)load the selected repository's connections. A repository with exactly
  // one selects it, so the common case still opens straight onto the curation
  // UI rather than making the admin pick from a list of one.
  useEffect(() => {
    if (currentRepoId === "") return;
    let cancelled = false;
    setConnections(undefined);
    setSelectedId(null);
    setAdding(false);
    setConnectionError(null);
    setSchema(null);
    setSchemaSaved(false);
    listDatabases(currentRepoId)
      .then((list) => {
        if (cancelled) return;
        setConnections(list);
        setSelectedId(list.length === 1 ? list[0].id : null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setConnectionError(errorMessage(err, t("adminDatabase.errorLoadConnection")));
      });
    return () => {
      cancelled = true;
    };
  }, [currentRepoId, t]);

  const selected = connections?.find((c) => c.id === selectedId) ?? null;

  // Seeded from the selection rather than held across it: switching connections
  // must not carry one database's description onto another's editor.
  useEffect(() => {
    setDescriptionDraft(selected?.description ?? "");
    setDescribeError(null);
    setDescriptionSaved(false);
  }, [selected?.id, selected?.description]);

  const resetForm = () => {
    setName("");
    setHost("");
    setPort("");
    setDatabase("");
    setUsername("");
    setPassword("");
    setConnectError(null);
  };

  const onConnect = async (e: FormEvent) => {
    e.preventDefault();
    if (currentRepoId === "" || name.trim() === "" || host.trim() === "" || port.trim() === "")
      return;
    setConnecting(true);
    setConnectError(null);
    try {
      const conn = await createDatabase(currentRepoId, {
        name: name.trim(),
        db_type: dbType,
        host: host.trim(),
        port: Number(port),
        database: database.trim(),
        username: username.trim(),
        password,
      });
      setConnections((prev) =>
        [...(prev ?? []), conn].sort((a, b) => a.name.localeCompare(b.name)),
      );
      setSelectedId(conn.id);
      setAdding(false);
      resetForm();
    } catch (err) {
      // 409 is the per-repository name clash, which has its own message: the
      // generic "could not connect" would send an admin to check the host.
      setConnectError(
        err instanceof ApiError && err.status === 409
          ? t("adminDatabase.duplicateName")
          : errorMessage(err, t("adminDatabase.errorConnect")),
      );
    } finally {
      setConnecting(false);
    }
  };

  const onIntrospect = async () => {
    if (currentRepoId === "" || selected === null) return;
    setIntrospecting(true);
    setIntrospectError(null);
    setSchemaSaved(false);
    try {
      const result = await introspect(currentRepoId, selected.id);
      setSchema(toEditable(result.schema, selected.exposed_schema));
      // Everything starts collapsed: with a dozen allow-listed tables, expanding
      // them all buries the save button exactly the way the excluded ones used to.
      // The per-table counter already reports how much of each table is exposed.
      setExpanded(new Set());
    } catch (err) {
      setIntrospectError(errorMessage(err, t("adminDatabase.errorIntrospect")));
    } finally {
      setIntrospecting(false);
    }
  };

  /** Whether this connection already carries a description of its current
   *  allow-list. The server decides staleness (`description_stale`); an empty
   *  description is not stale — **absent is not out-of-date** (#202) — so both
   *  halves are asked here. */
  const describedForThisSchema = (c: DatabaseConnection): boolean =>
    c.description !== "" && !c.description_stale;

  /** Look again until the background description lands, or give up quietly.
   *
   *  A budget rather than an open-ended poll: the task can fail (a provider
   *  outage), and #212 deliberately stores no "it failed" state — so this stops
   *  claiming anything and leaves **Describe** as the retry that shows the real
   *  reason. Nothing is asserted when the budget runs out; the field simply
   *  shows whatever is there. */
  const awaitDescription = async (repoId: string, connectionId: string): Promise<void> => {
    setWritingDescription(true);
    try {
      for (let attempt = 0; attempt < 8; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        let fresh: DatabaseConnection;
        try {
          fresh = await getDatabase(repoId, connectionId);
        } catch {
          return; // the connection went away, or the network did
        }
        setConnections((prev) => (prev ?? []).map((c) => (c.id === fresh.id ? fresh : c)));
        if (describedForThisSchema(fresh)) return;
      }
    } finally {
      setWritingDescription(false);
    }
  };

  const onSaveSchema = async () => {
    if (currentRepoId === "" || schema === null || selected === null) return;
    setSavingSchema(true);
    setSchemaError(null);
    setSchemaSaved(false);
    try {
      const updated = await patchSchema(
        currentRepoId,
        selected.id,
        toExposedSchema(schema),
        i18n.language,
      );
      setConnections((prev) => (prev ?? []).map((c) => (c.id === updated.id ? updated : c)));
      setSchemaSaved(true);
      // The save's own answer predates the description (#212) — the task is
      // queued as this response is composed — so a description that is missing
      // or belongs to the previous allow-list means one is on its way.
      if (updated.exposed_schema.length > 0 && !describedForThisSchema(updated)) {
        void awaitDescription(currentRepoId, updated.id);
      }
    } catch (err) {
      setSchemaError(errorMessage(err, t("adminDatabase.errorSaveSchema")));
    } finally {
      setSavingSchema(false);
    }
  };

  const onDescribe = async () => {
    if (currentRepoId === "" || selected === null) return;
    setDescribing(true);
    setDescribeError(null);
    setDescriptionSaved(false);
    try {
      const updated = await describeDatabase(currentRepoId, selected.id, i18n.language);
      setConnections((prev) => (prev ?? []).map((c) => (c.id === updated.id ? updated : c)));
      setDescriptionDraft(updated.description);
    } catch (err) {
      setDescribeError(errorMessage(err, t("adminDatabase.errorDescribe")));
    } finally {
      setDescribing(false);
    }
  };

  const onSaveDescription = async () => {
    if (currentRepoId === "" || selected === null) return;
    setSavingDescription(true);
    setDescribeError(null);
    setDescriptionSaved(false);
    try {
      const updated = await patchDescription(currentRepoId, selected.id, descriptionDraft);
      setConnections((prev) => (prev ?? []).map((c) => (c.id === updated.id ? updated : c)));
      setDescriptionSaved(true);
    } catch (err) {
      setDescribeError(errorMessage(err, t("adminDatabase.errorSaveDescription")));
    } finally {
      setSavingDescription(false);
    }
  };

  const onDelete = async () => {
    if (currentRepoId === "" || selected === null) return;
    if (!window.confirm(t("adminDatabase.confirmDelete"))) return;
    setDeleteError(null);
    const doomed = selected.id;
    try {
      await deleteDatabase(currentRepoId, doomed);
      setConnections((prev) => (prev ?? []).filter((c) => c.id !== doomed));
      setSelectedId(null);
      setSchema(null);
    } catch (err) {
      setDeleteError(errorMessage(err, t("adminDatabase.errorDelete")));
    }
  };

  const toggleExpanded = (table: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (!next.delete(table)) next.add(table);
      return next;
    });
  };

  const setTableChecked = (index: number, checked: boolean) => {
    // Including a table always reveals its columns — you cannot curate what you
    // cannot see, and an allow-listed table with no columns is useless.
    const name = schema?.[index].table;
    if (checked && name !== undefined) setExpanded((e) => new Set(e).add(name));
    setSchema((prev) =>
      prev === null ? prev : prev.map((t, i) => (i === index ? { ...t, checked } : t)),
    );
  };
  const setTableDescription = (index: number, description: string) => {
    setSchema((prev) =>
      prev === null ? prev : prev.map((t, i) => (i === index ? { ...t, description } : t)),
    );
  };
  const setColumnChecked = (tableIndex: number, columnIndex: number, checked: boolean) => {
    setSchema((prev) =>
      prev === null
        ? prev
        : prev.map((t, i) =>
            i === tableIndex
              ? {
                  ...t,
                  columns: t.columns.map((c, j) => (j === columnIndex ? { ...c, checked } : c)),
                }
              : t,
          ),
    );
  };
  const setColumnDescription = (tableIndex: number, columnIndex: number, description: string) => {
    setSchema((prev) =>
      prev === null
        ? prev
        : prev.map((t, i) =>
            i === tableIndex
              ? {
                  ...t,
                  columns: t.columns.map((c, j) => (j === columnIndex ? { ...c, description } : c)),
                }
              : t,
          ),
    );
  };

  if (currentRepoId === "") return <p>{t("adminDatabase.noRepos")}</p>;

  return (
    <section className="admin-database">
      {connectionError !== null && <p className="error">{connectionError}</p>}

      {connections === undefined ? (
        <p>{t("adminDatabase.loadingConnection")}</p>
      ) : (
        <>
          {connections.length > 0 && (
            <div className="database-list">
              <h2>{t("adminDatabase.connectionsTitle")}</h2>
              <ul>
                {connections.map((c) => (
                  <li key={c.id} className={c.id === selectedId ? "is-selected" : ""}>
                    <button
                      type="button"
                      aria-pressed={c.id === selectedId}
                      onClick={() => {
                        setSelectedId(c.id);
                        setSchema(null);
                        setSchemaSaved(false);
                      }}
                    >
                      <span className="database-name">{c.name}</span>
                      <span className="database-where">
                        {c.host}/{c.database}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              {!adding && (
                <button
                  type="button"
                  onClick={() => {
                    resetForm();
                    setAdding(true);
                  }}
                >
                  {t("adminDatabase.addButton")}
                </button>
              )}
            </div>
          )}

          {connections.length === 0 && !adding && <p>{t("adminDatabase.noConnections")}</p>}

          {(adding || connections.length === 0) && (
            <form className="database-setup" onSubmit={onConnect}>
              <p>{t("adminDatabase.setupIntro")}</p>

              <label htmlFor="db-name">{t("adminDatabase.nameLabel")}</label>
              <input
                id="db-name"
                value={name}
                placeholder={t("adminDatabase.namePlaceholder")}
                onChange={(e) => setName(e.target.value)}
                required
              />

              <label htmlFor="db-type">{t("adminDatabase.dbTypeLabel")}</label>
              <select
                id="db-type"
                value={dbType}
                onChange={(e) => setDbType(e.target.value as DatabaseType)}
              >
                <option value="postgres">{t("adminDatabase.dbTypePostgres")}</option>
                <option value="mysql">{t("adminDatabase.dbTypeMysql")}</option>
              </select>

              <label htmlFor="db-host">{t("adminDatabase.hostLabel")}</label>
              <input id="db-host" value={host} onChange={(e) => setHost(e.target.value)} required />

              <label htmlFor="db-port">{t("adminDatabase.portLabel")}</label>
              <input
                id="db-port"
                type="number"
                value={port}
                onChange={(e) => setPort(e.target.value)}
                required
              />

              <label htmlFor="db-database">{t("adminDatabase.databaseLabel")}</label>
              <input
                id="db-database"
                value={database}
                onChange={(e) => setDatabase(e.target.value)}
                required
              />

              <label htmlFor="db-username">{t("adminDatabase.usernameLabel")}</label>
              <input
                id="db-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />

              <label htmlFor="db-password">{t("adminDatabase.passwordLabel")}</label>
              <input
                id="db-password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />

              <button
                type="submit"
                disabled={
                  connecting || name.trim() === "" || host.trim() === "" || port.trim() === ""
                }
              >
                {connecting ? t("adminDatabase.connecting") : t("adminDatabase.connectButton")}
              </button>
              {adding && connections.length > 0 && (
                <button type="button" onClick={() => setAdding(false)}>
                  {t("adminDatabase.cancelAdd")}
                </button>
              )}
              {connectError !== null && <p className="error">{connectError}</p>}
            </form>
          )}

          {selected !== null && (
            <div className="database-connected">
              <h2>{selected.name}</h2>
              <ul className="database-summary">
                <li>{t("adminDatabase.summaryHost", { host: selected.host })}</li>
                <li>{t("adminDatabase.summaryDatabase", { database: selected.database })}</li>
                <li>{t("adminDatabase.summaryUsername", { username: selected.username })}</li>
              </ul>

              <div className="database-description">
                <h3>{t("adminDatabase.descriptionTitle")}</h3>
                {selected.description_stale && (
                  <p className="warning">{t("adminDatabase.descriptionStale")}</p>
                )}
                <textarea
                  aria-label={t("adminDatabase.descriptionTitle")}
                  placeholder={t("adminDatabase.descriptionPlaceholder")}
                  value={descriptionDraft}
                  onChange={(e) => setDescriptionDraft(e.target.value)}
                />
                <div className="database-actions">
                  <button type="button" onClick={onDescribe} disabled={describing}>
                    {describing ? t("adminDatabase.describing") : t("adminDatabase.describeButton")}
                  </button>
                  <button
                    type="button"
                    onClick={onSaveDescription}
                    disabled={savingDescription || descriptionDraft === selected.description}
                  >
                    {t("adminDatabase.saveDescription")}
                  </button>
                </div>
                {writingDescription && (
                  <p className="note">{t("adminDatabase.writingDescription")}</p>
                )}
                {descriptionSaved && (
                  <p className="success">{t("adminDatabase.descriptionSaved")}</p>
                )}
                {describeError !== null && <p className="error">{describeError}</p>}
              </div>

              <div className="database-actions">
                <button type="button" onClick={onIntrospect} disabled={introspecting}>
                  {introspecting
                    ? t("adminDatabase.introspecting")
                    : t("adminDatabase.introspectButton")}
                </button>
              </div>
              {introspectError !== null && <p className="error">{introspectError}</p>}

              {schema !== null && (
                <div className="database-schema-editor">
                  <h3>{t("adminDatabase.schemaTitle")}</h3>
                  {schema.length === 0 ? (
                    <p>{t("adminDatabase.noTablesFound")}</p>
                  ) : (
                    <ul className="schema-table-list">
                      {schema.map((table, tableIndex) => {
                        const isOpen = expanded.has(table.table);
                        const includedColumns = table.columns.filter((c) => c.checked).length;
                        return (
                          <li
                            key={table.table}
                            className={`schema-table${table.checked ? " is-included" : ""}`}
                          >
                            <div className="schema-table-head">
                              <label className="schema-check">
                                <input
                                  type="checkbox"
                                  aria-label={t("adminDatabase.includeTableLabel", {
                                    table: table.table,
                                  })}
                                  checked={table.checked}
                                  onChange={(e) => setTableChecked(tableIndex, e.target.checked)}
                                />
                                <span className="schema-name">{table.table}</span>
                              </label>
                              <button
                                type="button"
                                className="schema-disclosure"
                                aria-expanded={isOpen}
                                aria-label={t("adminDatabase.toggleColumnsLabel", {
                                  table: table.table,
                                })}
                                onClick={() => toggleExpanded(table.table)}
                              >
                                <span className="schema-count">
                                  {t("adminDatabase.columnCount", {
                                    checked: includedColumns,
                                    total: table.columns.length,
                                  })}
                                </span>
                                <span className="schema-chevron" aria-hidden="true" />
                              </button>
                            </div>
                            <div className="schema-table-body" hidden={!isOpen}>
                              <input
                                className="schema-desc"
                                placeholder={t("adminDatabase.tableDescriptionPlaceholder")}
                                value={table.description}
                                onChange={(e) => setTableDescription(tableIndex, e.target.value)}
                              />
                              <ul className="schema-column-list">
                                {table.columns.map((col, columnIndex) => (
                                  <li
                                    key={col.name}
                                    className={`schema-column${col.checked ? " is-included" : ""}`}
                                  >
                                    <label className="schema-check">
                                      <input
                                        type="checkbox"
                                        aria-label={t("adminDatabase.includeColumnLabel", {
                                          name: col.name,
                                        })}
                                        checked={col.checked}
                                        onChange={(e) =>
                                          setColumnChecked(
                                            tableIndex,
                                            columnIndex,
                                            e.target.checked,
                                          )
                                        }
                                      />
                                      <span className="schema-name">{col.name}</span>
                                    </label>
                                    <input
                                      className="schema-desc"
                                      placeholder={t("adminDatabase.columnDescriptionPlaceholder")}
                                      value={col.description}
                                      onChange={(e) =>
                                        setColumnDescription(
                                          tableIndex,
                                          columnIndex,
                                          e.target.value,
                                        )
                                      }
                                    />
                                  </li>
                                ))}
                              </ul>
                            </div>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                  <button type="button" onClick={onSaveSchema} disabled={savingSchema}>
                    {savingSchema
                      ? t("adminDatabase.savingSchema")
                      : t("adminDatabase.saveSchemaButton")}
                  </button>
                  {schemaSaved && <p className="success">{t("adminDatabase.schemaSaved")}</p>}
                  {schemaError !== null && <p className="error">{schemaError}</p>}
                </div>
              )}

              <div className="database-actions">
                <button type="button" className="danger" onClick={onDelete}>
                  {t("adminDatabase.deleteButton")}
                </button>
              </div>
              {deleteError !== null && <p className="error">{deleteError}</p>}
            </div>
          )}
        </>
      )}
    </section>
  );
}
