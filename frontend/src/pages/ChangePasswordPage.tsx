import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { LanguageSwitcher } from "../components/LanguageSwitcher";

/**
 * Set a new password. Reached either voluntarily or via the forced-change bounce
 * (a temp-password login).
 */
export function ChangePasswordPage(): ReactNode {
  const { session, changePassword } = useAuth();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const forced = session?.mustChangePassword ?? false;

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (newPassword !== confirm) {
      setError(t("changePassword.mismatch"));
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await changePassword(currentPassword, newPassword);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : t("common.somethingWrong"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-card">
      <div className="auth-lang">
        <LanguageSwitcher />
      </div>
      <h1>{t("changePassword.title")}</h1>
      {forced && (
        <p className="form-notice" role="status">
          {t("changePassword.forcedNotice")}
        </p>
      )}
      <form onSubmit={onSubmit}>
        <label>
          {t("changePassword.currentPassword")}
          <input
            name="current_password"
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
          />
        </label>
        <label>
          {t("changePassword.newPassword")}
          <input
            name="new_password"
            type="password"
            autoComplete="new-password"
            minLength={8}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
          />
        </label>
        <label>
          {t("changePassword.confirmNewPassword")}
          <input
            name="confirm"
            type="password"
            autoComplete="new-password"
            minLength={8}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? t("changePassword.saving") : t("changePassword.savePassword")}
        </button>
      </form>
    </div>
  );
}
