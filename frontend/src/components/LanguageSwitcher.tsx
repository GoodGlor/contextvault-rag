import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

/** A compact English/Ukrainian language toggle. Rendered in the app header and on
 *  the pre-login auth cards so the language can be changed from anywhere. */
export function LanguageSwitcher(): ReactNode {
  const { i18n, t } = useTranslation();
  const current = (i18n.resolvedLanguage ?? i18n.language).startsWith("uk") ? "uk" : "en";
  return (
    <select
      className="lang-switcher"
      aria-label={t("layout.language")}
      value={current}
      onChange={(e) => void i18n.changeLanguage(e.target.value)}
    >
      <option value="uk">Українська</option>
      <option value="en">English</option>
    </select>
  );
}
