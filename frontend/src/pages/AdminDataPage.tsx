import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { SourcesPanel } from "./data/SourcesPanel";
import { DatabasePanel } from "./data/DatabasePanel";

type Tab = "documents" | "database";

/** One "Data" surface for the current repository, merging the former Sources and
 *  Database admin pages into two tabs. Active tab is reflected in ?tab= so it is
 *  linkable and survives reload. */
export function AdminDataPage(): ReactNode {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "database" ? "database" : "documents";

  const select = (next: Tab) => {
    const p = new URLSearchParams(params);
    p.set("tab", next);
    setParams(p, { replace: true });
  };

  return (
    <section className="admin-data page">
      <h1>{t("data.title")}</h1>
      <div className="tabbar" role="tablist" aria-label={t("data.title")}>
        <button
          type="button"
          role="tab"
          id="tab-documents"
          aria-controls="panel-documents"
          aria-selected={tab === "documents"}
          className={tab === "documents" ? "tab active" : "tab"}
          onClick={() => select("documents")}
        >
          {t("data.tabDocuments")}
        </button>
        <button
          type="button"
          role="tab"
          id="tab-database"
          aria-controls="panel-database"
          aria-selected={tab === "database"}
          className={tab === "database" ? "tab active" : "tab"}
          onClick={() => select("database")}
        >
          {t("data.tabDatabase")}
        </button>
      </div>

      {tab === "documents" ? (
        <div role="tabpanel" id="panel-documents" aria-labelledby="tab-documents">
          <SourcesPanel />
        </div>
      ) : (
        <div role="tabpanel" id="panel-database" aria-labelledby="tab-database">
          <DatabasePanel />
        </div>
      )}
    </section>
  );
}
