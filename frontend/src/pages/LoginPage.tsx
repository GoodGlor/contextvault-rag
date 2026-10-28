import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { LanguageSwitcher } from "../components/LanguageSwitcher";

interface FromState {
  from?: { pathname: string };
}

/** Username + password sign-in. Accept-invite is layered on top of this. */
export function LoginPage(): ReactNode {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const { mustChangePassword } = await login(username, password);
      if (mustChangePassword) {
        navigate("/change-password", { replace: true });
        return;
      }
      const dest = (location.state as FromState | null)?.from?.pathname ?? "/";
      navigate(dest, { replace: true });
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
      <h1>{t("login.title")}</h1>
      <form onSubmit={onSubmit}>
        <label>
          {t("login.username")}
          <input
            name="username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </label>
        <label>
          {t("login.password")}
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? t("login.signingIn") : t("login.signIn")}
        </button>
      </form>
      <p className="form-hint">
        {t("login.haveInvite")} <Link to="/accept-invite">{t("login.activate")}</Link>
      </p>
    </div>
  );
}
