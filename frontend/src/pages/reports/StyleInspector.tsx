import { useTranslation } from "react-i18next";
import { InspectorGroup } from "./InspectorGroup";
import type { BrandTokens, PageTokens, TypographyTokens } from "../../api/reports";

/** The five accents that were checked against the templates' paper. A closed set
 *  rather than a colour picker: the owner rejected free CSS, and an unvalidated
 *  hue on a near-black template is a defect nothing here could catch. */
export const ACCENTS = ["#1d6fa3", "#c2544d", "#0e8a7a", "#8a6bbf", "#d2542a"] as const;

const PAIRINGS = ["grotesque", "serif", "mixed"] as const;
const DENSITIES = ["compact", "normal", "airy"] as const;
const COVERS = ["full", "band", "minimal"] as const;

/** The slider bounds the backend clamps to. Mirrored so the control cannot even
 *  offer a value the server would quietly move. */
const SCALE_MIN = 0.85;
const SCALE_MAX = 1.3;
const MARGIN_MIN = 10;
const MARGIN_MAX = 30;

export interface StyleInspectorProps {
  typography: TypographyTokens;
  page: PageTokens;
  brand: BrandTokens;
  /** Emitted whole, per group. The reader's design object is never mutated —
   *  the save path is a debounced PUT, and a mutated object defeats change
   *  detection. */
  onChange: (change: {
    typography?: TypographyTokens;
    page?: PageTokens;
    brand?: BrandTokens;
  }) => void;
  /** The cover logo's preview URL, resolved by the parent (it knows the
   *  repository). Null when no logo is set. */
  logoUrl: string | null;
  onLogoFile: (file: File) => void;
  onLogoClear: () => void;
  onReset: () => void;
}

/** The three inspector panels: Типографіка, Сторінка, Брендкіт.
 *
 *  Every control is a bounded token — a closed set or a slider inside a fixed
 *  range — never free CSS. Absent means "as the template", and that is
 *  load-bearing rather than cosmetic: the backend applies nothing when nothing
 *  is set, which is what keeps every report written before this step rendering
 *  byte-identically.
 */
export function StyleInspector({
  typography,
  page,
  brand,
  onChange,
  logoUrl,
  onLogoFile,
  onLogoClear,
  onReset,
}: StyleInspectorProps) {
  const { t } = useTranslation();

  return (
    <>
      <InspectorGroup id="typo" title={t("styleInspector.typography")} defaultOpen>
        <label>{t("styleInspector.pairing")}</label>
        <div className="chips">
          <button
            type="button"
            data-testid="pairing-chip"
            data-pairing=""
            aria-pressed={!typography.pairing}
            onClick={() => onChange({ typography: { ...typography, pairing: undefined } })}
          >
            {t("styleInspector.asTemplate")}
          </button>
          {PAIRINGS.map((pairing) => (
            <button
              key={pairing}
              type="button"
              data-testid="pairing-chip"
              data-pairing={pairing}
              aria-pressed={typography.pairing === pairing}
              onClick={() => onChange({ typography: { ...typography, pairing } })}
            >
              {t(`styleInspector.pairing${pairing[0].toUpperCase()}${pairing.slice(1)}`)}
            </button>
          ))}
        </div>

        <label htmlFor="style-scale">{t("styleInspector.scale")}</label>
        <input
          id="style-scale"
          data-testid="scale-range"
          type="range"
          min={SCALE_MIN}
          max={SCALE_MAX}
          step={0.05}
          value={typography.scale ?? 1}
          onChange={(event) =>
            onChange({ typography: { ...typography, scale: Number(event.target.value) } })
          }
        />
        <output>{Math.round((typography.scale ?? 1) * 100)} %</output>

        <label>{t("styleInspector.density")}</label>
        <div className="chips">
          {DENSITIES.map((density) => (
            <button
              key={density}
              type="button"
              data-testid="density-chip"
              data-density={density}
              aria-pressed={typography.density === density}
              onClick={() => onChange({ typography: { ...typography, density } })}
            >
              {t(`styleInspector.density${density[0].toUpperCase()}${density.slice(1)}`)}
            </button>
          ))}
        </div>
      </InspectorGroup>

      <InspectorGroup id="page" title={t("styleInspector.page")}>
        <label htmlFor="style-margin">{t("styleInspector.margin")}</label>
        <input
          id="style-margin"
          data-testid="margin-range"
          type="range"
          min={MARGIN_MIN}
          max={MARGIN_MAX}
          step={1}
          value={page.margin_mm ?? 18}
          onChange={(event) =>
            onChange({ page: { ...page, margin_mm: Number(event.target.value) } })
          }
        />
        <output>
          {page.margin_mm === undefined ? t("styleInspector.auto") : `${page.margin_mm} mm`}
        </output>
        <span className="hint">{t("styleInspector.marginHint")}</span>

        <label>
          <input
            type="checkbox"
            data-testid="section-rules-toggle"
            checked={page.section_rules === true}
            onChange={(event) =>
              onChange({ page: { ...page, section_rules: event.target.checked } })
            }
          />
          {t("styleInspector.sectionRules")}
        </label>
      </InspectorGroup>

      <InspectorGroup id="brand" title={t("styleInspector.brand")}>
        <label>{t("styleInspector.accent")}</label>
        <div className="swatches">
          <button
            type="button"
            data-testid="accent-swatch"
            data-accent=""
            aria-pressed={!brand.accent}
            title={t("styleInspector.asTemplate")}
            onClick={() => onChange({ brand: { ...brand, accent: undefined } })}
          />
          {ACCENTS.map((accent) => (
            <button
              key={accent}
              type="button"
              data-testid="accent-swatch"
              data-accent={accent}
              aria-pressed={brand.accent === accent}
              style={{ background: accent }}
              title={accent}
              onClick={() => onChange({ brand: { ...brand, accent } })}
            />
          ))}
        </div>
        <span className="hint">{t("styleInspector.accentHint")}</span>

        <label>{t("styleInspector.logo")}</label>
        {logoUrl ? (
          <div className="thumb-preview">
            <img src={logoUrl} alt="" />
            <button type="button" data-testid="logo-clear" onClick={onLogoClear}>
              {t("styleInspector.removeLogo")}
            </button>
          </div>
        ) : null}
        <input
          type="file"
          data-testid="logo-file"
          accept="image/png,image/jpeg,image/webp"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onLogoFile(file);
          }}
        />

        <label>{t("styleInspector.coverLayout")}</label>
        <div className="chips">
          {COVERS.map((cover) => (
            <button
              key={cover}
              type="button"
              data-testid="cover-chip"
              data-cover={cover}
              aria-pressed={(brand.cover ?? "full") === cover}
              onClick={() => onChange({ brand: { ...brand, cover } })}
            >
              {t(`styleInspector.cover${cover[0].toUpperCase()}${cover.slice(1)}`)}
            </button>
          ))}
        </div>

        <label>
          <input
            type="checkbox"
            data-testid="approval-toggle"
            checked={brand.approval === true}
            onChange={(event) => onChange({ brand: { ...brand, approval: event.target.checked } })}
          />
          {t("styleInspector.approval")}
        </label>

        <button type="button" data-testid="style-reset" onClick={onReset}>
          {t("styleInspector.reset")}
        </button>
      </InspectorGroup>
    </>
  );
}
