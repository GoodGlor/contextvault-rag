import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { TemplateGallery } from "./TemplateGallery";
import { listReportTemplates } from "../../api/reports";
import type { TemplateChoice } from "../../api/reports";
import { getReportTemplate, setReportTemplate } from "../../api/repositories";

export interface RepositoryTemplateFieldProps {
  repositoryId: string;
}

/** The repository's default template for reports generated from now on.
 *
 *  It rebuilds nothing, and shows no preview. Existing reports each snapshotted
 *  their template at generation — that is step 3b's binding rule, and the whole
 *  reason a re-render reads the report row and never the repository. A specimen
 *  document rendered here to "show" the default would be a fabricated report,
 *  which is exactly the fake artifact the design rejects.
 */
export function RepositoryTemplateField({ repositoryId }: RepositoryTemplateFieldProps): ReactNode {
  const { t } = useTranslation();
  const [templates, setTemplates] = useState<TemplateChoice[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  // Whether `selected` is an answer or just its initial value. Without it a
  // failed read of the current default is indistinguishable from "no default
  // chosen", and the field would state a fact it never learned.
  const [currentLoaded, setCurrentLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [currentError, setCurrentError] = useState<string | null>(null);

  // Two requests, settled separately rather than through `Promise.all`. Joined,
  // either rejection produced "could not load the list of templates" — a lie
  // whenever it was the *current default* that failed, and one that hides which
  // of the two capabilities is actually gone. They also fail differently: no
  // list means no gallery at all, while no current default means the gallery
  // works but marks the wrong card.
  useEffect(() => {
    let live = true;
    void listReportTemplates()
      .then((list) => {
        if (!live) return;
        setTemplates(list);
        // Cleared on success, or a transient failure followed by a good refetch
        // leaves a populated gallery sitting under a stale error.
        setListError(null);
      })
      .catch(() => live && setListError(t("reports.design.errorLoad")));
    void getReportTemplate(repositoryId)
      .then((current) => {
        if (!live) return;
        setSelected(current.template);
        setCurrentLoaded(true);
        setCurrentError(null);
      })
      .catch(() => {
        if (!live) return;
        setCurrentLoaded(false);
        setCurrentError(t("reports.design.errorLoadCurrent"));
      });
    return () => {
      live = false;
    };
  }, [repositoryId, t]);

  async function choose(key: string): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const saved = await setReportTemplate(repositoryId, key);
      setSelected(saved.template);
      setCurrentLoaded(true);
      setCurrentError(null);
      setOpen(false);
    } catch {
      setError(t("reports.design.errorApply"));
    } finally {
      setBusy(false);
    }
  }

  const currentName = templates.find((tpl) => tpl.key === selected)?.name ?? null;

  return (
    <div className="repo-template">
      <button type="button" onClick={() => setOpen(true)}>
        {t("reports.design.openGallery")}
      </button>
      {/* A default that is set has to be readable without opening the gallery —
          otherwise the only way to see which template is configured is to go
          looking for the pressed card. A slug this build no longer ships matches
          nothing and names nothing, the same tolerance the gallery has. */}
      {currentName !== null && (
        <p className="report-note">{t("reports.design.current", { name: currentName })}</p>
      )}
      {currentLoaded && selected === null && templates.length > 0 && (
        <p className="report-note">
          {t("reports.design.repoNoChoice", { name: templates[0].name })}
        </p>
      )}
      {/* While the gallery is up it covers the page, so these say nothing to
          anybody; the gallery renders its own copies instead. */}
      {!open && busy && <span role="status">{t("reports.design.applying")}</span>}
      {!open && error !== null && <p className="error">{error}</p>}
      {!open && listError !== null && <p className="error">{listError}</p>}
      {!open && currentError !== null && <p className="error">{currentError}</p>}
      {open && (
        <TemplateGallery
          templates={templates}
          selected={selected}
          lede={t("reports.design.galleryLedeRepo")}
          busy={busy}
          error={error ?? listError ?? currentError}
          onChoose={(key) => void choose(key)}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
