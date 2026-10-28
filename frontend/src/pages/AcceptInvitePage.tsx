import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { LanguageSwitcher } from "../components/LanguageSwitcher";

/**
 * Redeem an invite: choose a password and get signed in. The token is prefilled
 * from a `?token=…` query param (the link the admin shares) but stays editable.
 */
export function AcceptInvitePage(): ReactNode {
  const { acceptInvite } = useAuth();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const [token, setToken] = useState(params.get("token") ?? "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== confirm) {
      setError(t("acceptInvite.mismatch"));
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await acceptInvite(token.trim(), password);
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
      <h1>{t("acceptInvite.title")}</h1>
      <p className="form-hint">{t("acceptInvite.chooseHint")}</p>
      <form onSubmit={onSubmit}>
        <label>
          {t("acceptInvite.inviteToken")}
          <input name="token" value={token} onChange={(e) => setToken(e.target.value)} required />
        </label>
        <label>
          {t("acceptInvite.password")}
          <input
            name="password"
            type="password"
            autoComplete="new-password"
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        <label>
          {t("acceptInvite.confirmPassword")}
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
          {busy ? t("acceptInvite.activating") : t("acceptInvite.activateAccount")}
        </button>
      </form>
      <p className="form-hint">
        {t("acceptInvite.haveAccount")} <Link to="/login">{t("acceptInvite.signIn")}</Link>
      </p>
    </div>
  );
}
