import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

const STORAGE_KEY = "cv.workbench.howto";

function remembered(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // localStorage may be unavailable
    return false;
  }
}

/** Three numbered steps across the top of the canvas, dismissible for good.
 *
 *  **Moved here from 6a deliberately.** Two of its three steps describe things
 *  6a did not build — clicking a block on the sheet, and the sheet/PDF switch —
 *  and a hint that names a control the page does not have is worse than no hint
 *  at all. Both are true as of this card, so the strip arrives with them.
 *
 *  The third step stops short of the mockup's «правиться прямо на аркуші»:
 *  editing *on* the sheet is 6c. It says where the words are edited today.
 */
export function HowToStrip(): ReactNode {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(remembered);

  const dismiss = useCallback((): void => {
    setDismissed(true);
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      // localStorage may be unavailable; the strip simply returns next time
    }
  }, []);

  if (dismissed) return null;

  return (
    <aside className="wb-howto" data-testid="howto">
      <ol>
        <li>{t("reports.workbench.howToSelect")}</li>
        <li>{t("reports.workbench.howToStyle")}</li>
        <li>{t("reports.workbench.howToPdf")}</li>
      </ol>
      <button type="button" onClick={dismiss} aria-label={t("reports.workbench.howToDismiss")}>
        ✕
      </button>
    </aside>
  );
}
