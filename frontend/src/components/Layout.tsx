import { useState } from "react";
import type { ReactNode } from "react";
import { Outlet, matchPath, useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Sidebar } from "./Sidebar";

/** Routes that get the shell out of the way.
 *
 *  **`matchPath`, not a substring test.** `/reports/design-notes` contains the
 *  word and is not the workbench; matching the pattern is the difference
 *  between a rule and a coincidence. */
const FOCUS_ROUTES = ["/reports/:reportId/design"];

/** Authenticated app chrome: a left sidebar + routed content. On narrow screens
 *  the sidebar collapses behind a menu toggle.
 *
 *  **On the design workbench the sidebar becomes a 56 px icon rail** (#167).
 *  That route is four columns, not three — 244 + 272 + 320 = 836 px of chrome —
 *  so at 1440 px the canvas is left 564 px for a sheet that is 794 px wide, and
 *  the page it exists to preview cannot be shown at its own size. The rail
 *  returns 188 px of that, and the navigation stays reachable rather than
 *  disappearing: an editor with exactly one way out is a trap.
 *
 *  Tied to the route rather than to a stored preference on purpose — one way to
 *  be in this state, and leaving the editor is what leaves it.
 */
export function Layout(): ReactNode {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const focus = FOCUS_ROUTES.some((pattern) => matchPath(pattern, pathname) !== null);
  return (
    <div
      className="app-shell"
      data-focus={focus ? "true" : "false"}
      data-nav-open={open ? "true" : "false"}
    >
      <button
        type="button"
        className="nav-toggle"
        aria-label={t("layout.menu")}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        ☰
      </button>
      <Sidebar />
      <main className="app-main" onClick={() => open && setOpen(false)}>
        <Outlet />
      </main>
    </div>
  );
}
