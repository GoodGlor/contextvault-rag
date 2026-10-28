import { useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { StyleInspector } from "./StyleInspector";
import { TemplateGallery } from "./TemplateGallery";
import { useDesignDraft } from "./useDesignDraft";
import { reportAssetUrl, uploadReportAsset } from "../../api/reportAssets";
import type { TemplateChoice } from "../../api/reports";

export interface PlanDesignStepProps {
  repositoryId: string;
  reportId: string;
  /** Fetched once by the page, as every other template surface receives them. */
  templates: TemplateChoice[];
  templatesError: string | null;
  /** The approve round is in flight. */
  busy: boolean;
  /** Why the approve failed, owned by the page. */
  error: string | null;
  onBack: () => void;
  onGenerate: () => void;
}

/** Step 2 of the approval card: how the report will look, chosen before it runs.
 *
 *  It stores an *intent*. Nothing is rendered here and nothing is rebuilt —
 *  `PUT …/design` is explicitly allowed on a report with no stored section data,
 *  and `_bind_style` resolves a design already on the row ahead of the repository
 *  default. Generation then produces a report already wearing it, which is the
 *  whole point: the styling decision used to be available only after the
 *  expensive part had already run.
 *
 *  The block editor is *not* here, and that is not an omission. `default_blocks`
 *  derives its list from executed sections — the chart block "is the one entry
 *  decided by a field that does not exist yet at generation time" — so there is
 *  nothing to edit until the report exists. Block editing stays post-generation.
 */
export function PlanDesignStep({
  repositoryId,
  reportId,
  templates,
  templatesError,
  busy,
  error,
  onBack,
  onGenerate,
}: PlanDesignStepProps): ReactNode {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [logoError, setLogoError] = useState<string | null>(null);
  const draft = useDesignDraft({
    repositoryId,
    reportId,
    errorText: t("reports.steps.errorSaveDesign"),
  });

  const chosen = templates.find((tpl) => tpl.key === draft.template) ?? null;

  return (
    <div className="plan-design-step">
      <p className="form-hint">{t("reports.steps.designIntro")}</p>

      <button type="button" onClick={() => setOpen(true)}>
        {t("reports.design.openGallery")}
      </button>
      <p className="report-note">
        {chosen !== null
          ? t("reports.steps.chosen", { name: chosen.name })
          : t("reports.steps.noneChosen")}
      </p>

      {draft.style !== null && (
        <StyleInspector
          typography={draft.style.typography}
          page={draft.style.page}
          brand={draft.style.brand}
          onChange={draft.restyle}
          logoUrl={
            draft.style.brand.logo_asset_id
              ? `/api${reportAssetUrl(repositoryId, draft.style.brand.logo_asset_id)}`
              : null
          }
          onLogoFile={(file) => {
            setLogoError(null);
            void uploadReportAsset(repositoryId, file)
              // Functional, because this resolves later: reading `brand` here and
              // writing it back on resolution discarded whatever the reader
              // changed while the upload was in flight.
              .then((asset) =>
                draft.restyle((current) => ({
                  brand: { ...current.brand, logo_asset_id: asset.id },
                })),
              )
              .catch(() => setLogoError(t("reports.steps.errorUploadLogo")));
          }}
          onLogoClear={() =>
            draft.restyle({ brand: { ...(draft.style?.brand ?? {}), logo_asset_id: null } })
          }
          onReset={() => draft.restyle({ typography: {}, page: {}, brand: {} })}
        />
      )}

      {draft.error !== null && <p className="error">{draft.error}</p>}
      {logoError !== null && <p className="error">{logoError}</p>}
      {error !== null && <p className="error">{error}</p>}

      <div className="plan-step-actions">
        <button
          type="button"
          onClick={() => {
            // Back unmounts this step, and the unmount cleanup clears the
            // debounce timer — so a change made inside the window would be
            // dropped with no message, and the next Next would re-seed from the
            // row without it. Flushing first is what makes Back cost nothing.
            void draft.flush().then(onBack);
          }}
          disabled={busy}
        >
          {t("reports.steps.back")}
        </button>
        <button
          type="button"
          onClick={() => {
            // A template pick is debounced like every other design write (see
            // useDesignDraft). Without this, a normal-speed click here can fire
            // /approve while that PUT is still waiting behind its timer, and the
            // report generates before the chosen design ever reached the row —
            // exactly the ordering this step exists to guarantee.
            void draft.flush().then(onGenerate);
          }}
          disabled={busy}
        >
          {t("reports.steps.generate")}
        </button>
        {busy && (
          <span role="status" data-testid="generation-status">
            {t("reports.generatingLabel")}
          </span>
        )}
      </div>

      {open && (
        <TemplateGallery
          templates={templates}
          selected={draft.template}
          lede={t("reports.design.galleryLedePlan")}
          busy={false}
          error={templatesError}
          onChoose={(key) => {
            draft.choose(key);
            setOpen(false);
          }}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}
