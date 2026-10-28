import type { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../auth/AuthContext";
import { useCurrentRepository } from "../repository/RepositoryContext";
import { LanguageSwitcher } from "./LanguageSwitcher";

interface NavItemDef {
  to: string;
  labelKey: string;
  icon: string;
  end?: boolean;
}
interface NavGroupDef {
  labelKey: string;
  adminOnly: boolean;
  items: NavItemDef[];
}

/** Navigation model. Groups render top-to-bottom; admin-only groups are hidden
 *  for members. Editing nav = editing this array. */
const NAV: NavGroupDef[] = [
  {
    labelKey: "nav.groupWorkspace",
    adminOnly: false,
    items: [
      { to: "/", labelKey: "nav.query", icon: "💬", end: true },
      { to: "/reports", labelKey: "nav.reports", icon: "📊" },
    ],
  },
  {
    labelKey: "nav.groupManage",
    adminOnly: true,
    items: [
      { to: "/admin/data", labelKey: "nav.data", icon: "🧠" },
      { to: "/admin/providers", labelKey: "nav.providers", icon: "🔌" },
      { to: "/admin/insights", labelKey: "nav.insights", icon: "📈" },
    ],
  },
  {
    labelKey: "nav.groupAdmin",
    adminOnly: true,
    items: [
      { to: "/admin/repositories", labelKey: "nav.repositories", icon: "📁" },
      { to: "/admin/users", labelKey: "nav.users", icon: "👥" },
    ],
  },
];

export function Sidebar(): ReactNode {
  const { t } = useTranslation();
  const { session, logout } = useAuth();
  const navigate = useNavigate();
  const { repos, currentRepoId, setCurrentRepoId } = useCurrentRepository();
  const isAdmin = session?.role === "admin";

  const onLogout = () => {
    logout();
    navigate("/login", { replace: true });
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">ContextVault</div>

      {repos.length > 0 && (
        <label className="repo-switch">
          <span className="repo-switch-caption">{t("nav.repository")}</span>
          <select value={currentRepoId} onChange={(e) => setCurrentRepoId(e.target.value)}>
            {repos.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </label>
      )}

      <nav className="sidebar-nav">
        {NAV.filter((g) => !g.adminOnly || isAdmin).map((group) => (
          <div key={group.labelKey} className="nav-group">
            <span className="nav-group-label">{t(group.labelKey)}</span>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className="nav-item"
                // The tooltip is for the mouse in focus mode, where the label is
                // visually hidden. It is *not* the accessible name — that stays
                // the label itself, which is why `.nav-text` is clipped rather
                // than `display: none`. A rail of unnamed icons is a rail nobody
                // can use with a screen reader.
                title={t(item.labelKey)}
              >
                <span className="nav-ico" aria-hidden="true">
                  {item.icon}
                </span>
                <span className="nav-text">{t(item.labelKey)}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <LanguageSwitcher />
        {session && (
          <div className="sidebar-user">
            <span className="sidebar-username">{session.username}</span>
            <span className="sidebar-role">{session.role}</span>
            <button type="button" onClick={onLogout}>
              {t("layout.logOut")}
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
