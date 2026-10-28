import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./locales/en.json";
import uk from "./locales/uk.json";

export const LOCALE_STORAGE_KEY = "contextvault.locale";
export const SUPPORTED_LOCALES = ["uk", "en"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];

/** Read the persisted locale; Ukrainian is the product default. */
function initialLocale(): Locale {
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (stored === "uk" || stored === "en") return stored;
  } catch {
    // localStorage may be unavailable (SSR, restricted env) — fall through.
  }
  return "uk";
}

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, uk: { translation: uk } },
  lng: initialLocale(),
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes.
});

// Persist the choice so it survives reloads.
i18n.on("languageChanged", (lng) => {
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, lng);
  } catch {
    // Ignore storage failures — the language still changes for this session.
  }
});

export default i18n;
