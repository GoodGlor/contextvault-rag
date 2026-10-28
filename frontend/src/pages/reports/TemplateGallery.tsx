import { useEffect } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { TemplateChoice } from "../../api/reports";

export interface TemplateGalleryProps {
  templates: TemplateChoice[];
  /** The slug recorded on the row, or null when none is. A slug this build no
   *  longer ships marks nothing — the backend tolerates unknown templates on
   *  read so an old report never becomes unrenderable, and the UI matches. */
  selected: string | null;
  /** The sentence under the title, supplied by whichever surface mounted the
   *  gallery. Required rather than defaulted: a default would necessarily be
   *  one host's copy, and the host that forgot to override it would silently
   *  describe the other one's behaviour — which is exactly how the repository
   *  default came to promise a rebuild it never performs. */
  lede: string;
  busy: boolean;
  /** Whatever went wrong on the surface that mounted the gallery — a failed load
   *  or a failed apply. It has to be shown *here*: the backdrop is fixed,
   *  full-viewport and opaque, so a message left in the host's own page flow is
   *  painted over and the reader sees an empty grid with no explanation. */
  error?: string | null;
  onChoose: (key: string) => void;
  onClose: () => void;
}

/** The five shipped templates as swatch cards.
 *
 *  The thumbnail is deliberately an abstraction — a heading, a rule, five bars
 *  and three placeholder lines in the template's own colours — and not a
 *  miniature of the report. A thumbnail that imitated the document would be a
 *  second renderer, free to drift from the real one; five bars and three grey
 *  lines cannot drift, because they never claimed to be the report. The real
 *  artifact is what the preview pane shows.
 *
 *  Presentational on purpose: it takes a selection and reports a choice, and
 *  knows nothing about reports or repositories. That is what lets the
 *  per-report panel and the repository-default field mount the same component.
 */
export function TemplateGallery({
  templates,
  selected,
  lede,
  busy,
  error = null,
  onChoose,
  onClose,
}: TemplateGalleryProps): ReactNode {
  const { t } = useTranslation();

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="tpl-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="tpl-gallery" role="dialog" aria-modal="true" aria-labelledby="tpl-title">
        <header>
          <div>
            <h2 id="tpl-title">{t("reports.design.galleryTitle")}</h2>
            <p className="tpl-lede">{lede}</p>
          </div>
          <button type="button" className="tpl-close" onClick={onClose}>
            {t("reports.design.close")}
          </button>
        </header>
        {busy && (
          <p className="report-note" role="status">
            {t("reports.design.applying")}
          </p>
        )}
        {error !== null && <p className="error">{error}</p>}
        <div className="tpl-grid">
          {templates.map((tpl) => (
            <button
              key={tpl.key}
              type="button"
              className="tpl-card"
              aria-pressed={tpl.key === selected}
              disabled={busy}
              onClick={() => onChoose(tpl.key)}
            >
              <span
                className="tpl-thumb"
                aria-hidden="true"
                style={{ background: tpl.paper, color: tpl.ink }}
              >
                <span className="t-h">{tpl.name}</span>
                <span className="t-rule" style={{ background: tpl.accent }} />
                <span className="t-bars">
                  {[58, 82, 44, 96, 70].map((h, i) => (
                    <i
                      key={i}
                      style={{ height: `${h}%`, background: tpl.accent, opacity: 0.35 + i * 0.13 }}
                    />
                  ))}
                </span>
                <span className="t-lines">
                  <i style={{ background: tpl.ink }} />
                  <i style={{ background: tpl.ink }} />
                  <i style={{ background: tpl.ink, width: "64%" }} />
                </span>
              </span>
              <span className="tpl-name">{tpl.name}</span>
              <span className="tpl-desc">{t(`reports.design.templates.${tpl.key}`)}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
