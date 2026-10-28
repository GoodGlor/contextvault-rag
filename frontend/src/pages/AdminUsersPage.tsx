import { useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api/client";
import { createInvitation, type Invitation, type Role } from "../api/invitations";
import { deleteUser, listUsers, resetUserPassword, type AdminUser } from "../api/users";
import { grantAccess, listGrants, revokeAccess, type Grant } from "../api/grants";
import { listAllRepositories, type AdminRepository } from "../api/repositories";

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** Admin surface for accounts and repository access. */
export function AdminUsersPage(): ReactNode {
  const { t } = useTranslation();
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [repos, setRepos] = useState<AdminRepository[] | null>(null);
  const [reposError, setReposError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listUsers()
      .then((u) => !cancelled && setUsers(u))
      .catch(
        (err: unknown) => !cancelled && setUsersError(errorMessage(err, t("users.loadUsersError"))),
      );
    listAllRepositories()
      .then((r) => !cancelled && setRepos(r))
      .catch(
        (err: unknown) => !cancelled && setReposError(errorMessage(err, t("users.loadReposError"))),
      );
    return () => {
      cancelled = true;
    };
  }, [t]);

  if (usersError !== null) return <p className="error">{usersError}</p>;
  if (reposError !== null) return <p className="error">{reposError}</p>;
  if (users === null || repos === null) return <p>{t("users.loading")}</p>;

  return (
    <div className="admin-users">
      <h1>{t("users.title")}</h1>
      <InviteForm />
      <AccountsList
        users={users}
        onDeleted={(id) => setUsers((prev) => prev?.filter((u) => u.id !== id) ?? prev)}
      />
      <GrantsPanel users={users} repos={repos} />
    </div>
  );
}

/** Issue an onboarding invite and reveal its one-time token. */
function InviteForm(): ReactNode {
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invite, setInvite] = useState<Invitation | null>(null);
  const [copied, setCopied] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = username.trim();
    if (trimmed === "") return;
    setBusy(true);
    setError(null);
    try {
      const inv = await createInvitation({ username: trimmed, role });
      setInvite(inv);
      setCopied(false);
      setUsername("");
    } catch (err) {
      setError(errorMessage(err, t("users.createInviteError")));
    } finally {
      setBusy(false);
    }
  };

  // The full link a recipient can paste into their browser.
  const inviteUrl =
    invite !== null ? `${window.location.origin}/accept-invite?token=${invite.token}` : "";

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(inviteUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard unavailable (permissions/insecure context) — leave the link visible to copy manually.
    }
  };

  return (
    <section aria-label={t("users.inviteSection")}>
      <h2>{t("users.inviteHeading")}</h2>
      <form onSubmit={onSubmit}>
        <label htmlFor="invite-username">{t("users.usernameLabel")}</label>
        <input
          id="invite-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="invite-role">{t("users.roleLabel")}</label>
        <select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="user">{t("users.roleUser")}</option>
          <option value="admin">{t("users.roleAdmin")}</option>
        </select>
        <button type="submit" disabled={busy || username.trim() === ""}>
          {t("users.sendInvite")}
        </button>
        {error !== null && <p className="error">{error}</p>}
      </form>
      {invite !== null && (
        <p className="invite-link">
          {t("users.inviteLinkPrefix")} <strong>{invite.username}</strong>: <code>{inviteUrl}</code>{" "}
          <button type="button" className="copy-invite" onClick={onCopy}>
            {copied ? t("users.copiedLink") : t("users.copyLink")}
          </button>{" "}
          {t("users.inviteLinkNote")}
        </p>
      )}
    </section>
  );
}

/** The account list, each row offering reset-password and (confirmed) delete. */
function AccountsList({
  users,
  onDeleted,
}: {
  users: AdminUser[];
  onDeleted: (id: string) => void;
}): ReactNode {
  const { t } = useTranslation();
  return (
    <section aria-label={t("users.accountsSection")}>
      <h2>{t("users.accountsHeading")}</h2>
      <ul className="user-list">
        {users.map((u) => (
          <UserRow key={u.id} user={u} onDeleted={() => onDeleted(u.id)} />
        ))}
      </ul>
    </section>
  );
}

function UserRow({ user, onDeleted }: { user: AdminUser; onDeleted: () => void }): ReactNode {
  const { t } = useTranslation();
  const [temp, setTemp] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onReset = async () => {
    setResetting(true);
    setError(null);
    try {
      const result = await resetUserPassword(user.id);
      setTemp(result.temporary_password);
    } catch (err) {
      setError(errorMessage(err, t("users.resetPasswordError")));
    } finally {
      setResetting(false);
    }
  };

  const onDelete = async () => {
    if (confirmName !== user.username) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteUser(user.id, confirmName);
      onDeleted();
    } catch (err) {
      setError(errorMessage(err, t("users.deleteUserError")));
      setDeleting(false);
    }
  };

  return (
    <li className="user-item">
      <span className="user-name">{user.username}</span>
      <span className="user-role">
        {user.role === "admin" ? t("users.roleAdmin") : t("users.roleUser")}
      </span>
      {user.must_change_password && <span className="badge">{t("users.passwordChangeOwed")}</span>}
      <button type="button" onClick={onReset} disabled={resetting}>
        {t("users.resetPassword")}
      </button>
      {!confirming ? (
        <button type="button" onClick={() => setConfirming(true)}>
          {t("users.delete")}
        </button>
      ) : (
        <span className="confirm-delete">
          <label htmlFor={`confirm-${user.id}`}>{t("users.confirmUsername")}</label>
          <input
            id={`confirm-${user.id}`}
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
          />
          <button
            type="button"
            onClick={onDelete}
            disabled={deleting || confirmName !== user.username}
          >
            {t("users.confirmDelete")}
          </button>
        </span>
      )}
      {temp !== null && (
        <p className="temp-password">
          {t("users.temporaryPassword")} <code>{temp}</code>
        </p>
      )}
      {error !== null && <p className="error">{error}</p>}
    </li>
  );
}

/** Manage per-repository access grants: list, add, revoke. */
function GrantsPanel({
  users,
  repos,
}: {
  users: AdminUser[];
  repos: AdminRepository[];
}): ReactNode {
  const { t } = useTranslation();
  const [selected, setSelected] = useState(repos[0]?.id ?? "");
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState(users[0]?.id ?? "");
  const [expires, setExpires] = useState("");
  const [granting, setGranting] = useState(false);

  const usernameOf = (id: string) => users.find((u) => u.id === id)?.username ?? id;

  useEffect(() => {
    if (selected === "") return;
    let cancelled = false;
    setGrants(null);
    setError(null);
    listGrants(selected)
      .then((g) => !cancelled && setGrants(g))
      .catch(
        (err: unknown) => !cancelled && setError(errorMessage(err, t("users.loadGrantsError"))),
      );
    return () => {
      cancelled = true;
    };
  }, [selected, t]);

  const onGrant = async (e: FormEvent) => {
    e.preventDefault();
    if (selected === "" || target === "") return;
    setGranting(true);
    setError(null);
    try {
      await grantAccess(selected, {
        user_id: target,
        expires_at: expires === "" ? null : new Date(expires).toISOString(),
      });
      const refreshed = await listGrants(selected);
      setGrants(refreshed);
      setExpires("");
    } catch (err) {
      setError(errorMessage(err, t("users.grantAccessError")));
    } finally {
      setGranting(false);
    }
  };

  const onRevoke = async (userId: string) => {
    if (selected === "") return;
    setError(null);
    try {
      await revokeAccess(selected, userId);
      setGrants((prev) => prev?.filter((g) => g.user_id !== userId) ?? prev);
    } catch (err) {
      setError(errorMessage(err, t("users.revokeAccessError")));
    }
  };

  return (
    <section aria-label={t("users.repoAccessSection")}>
      <h2>{t("users.repoAccessHeading")}</h2>
      <label htmlFor="grant-repo">{t("users.repository")}</label>
      <select id="grant-repo" value={selected} onChange={(e) => setSelected(e.target.value)}>
        {repos.map((r) => (
          <option key={r.id} value={r.id}>
            {r.name}
          </option>
        ))}
      </select>

      <form onSubmit={onGrant}>
        <label htmlFor="grant-user">{t("users.grantTo")}</label>
        <select id="grant-user" value={target} onChange={(e) => setTarget(e.target.value)}>
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.username}
            </option>
          ))}
        </select>
        <label htmlFor="grant-expires">{t("users.expires")}</label>
        <input
          id="grant-expires"
          type="date"
          value={expires}
          onChange={(e) => setExpires(e.target.value)}
        />
        <button type="submit" disabled={granting || target === ""}>
          {t("users.grantAccess")}
        </button>
      </form>

      {error !== null && <p className="error">{error}</p>}
      {grants === null ? (
        <p>{t("users.loadingGrants")}</p>
      ) : grants.length === 0 ? (
        <p>{t("users.noGrantsYet")}</p>
      ) : (
        <ul className="grant-list">
          {grants.map((g) => (
            <li key={g.id} className="grant-item">
              <span className="grant-user">{usernameOf(g.user_id)}</span>
              <span className="grant-expiry">
                {g.expires_at === null
                  ? t("users.noExpiry")
                  : t("users.until", { date: g.expires_at })}
              </span>
              <button type="button" onClick={() => onRevoke(g.user_id)}>
                {t("users.revoke")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
