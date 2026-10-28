import { useCallback, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { configureApi } from "../api/client";
import * as authApi from "../api/auth";
import * as invitationsApi from "../api/invitations";
import { decodeToken } from "./jwt";
import { AuthContext, type Session } from "./AuthContext";

const STORAGE_KEY = "contextvault.session";

/** A token is stale once its `exp` (seconds) is in the past. */
function isExpired(token: string): boolean {
  const exp = decodeToken(token)?.exp;
  return exp !== undefined && exp * 1000 <= Date.now();
}

function loadSession(): Session | null {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as Session;
    // Drop malformed or already-expired tokens so we never mount an authenticated
    // shell on a dead session (there is no refresh endpoint — expiry means re-login).
    if (typeof parsed.token !== "string" || decodeToken(parsed.token) === null) return null;
    if (isExpired(parsed.token)) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function sessionFromToken(token: string, username: string, mustChangePassword: boolean): Session {
  const claims = decodeToken(token);
  if (claims === null) throw new Error("Received a malformed token from the server");
  return { token, userId: claims.sub, role: claims.role, username, mustChangePassword };
}

export function AuthProvider({ children }: { children: ReactNode }): ReactNode {
  const [session, setSession] = useState<Session | null>(loadSession);

  // Keep a ref in sync so the API client's token getter always reads the latest
  // value without re-running the configure effect on every session change.
  const sessionRef = useRef(session);
  sessionRef.current = session;

  const persist = useCallback((next: Session | null) => {
    setSession(next);
    if (next === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  }, []);

  const logout = useCallback(() => persist(null), [persist]);

  // Wire the API client synchronously during render (once, guarded by a ref) —
  // NOT in a useEffect. React runs child effects before parent effects, so a
  // child that fires a request on mount (e.g. a page loading its data) would
  // otherwise run before this provider's effect had configured the client,
  // sending the request with no token and tripping onUnauthorized on the 401,
  // which logs out an otherwise-valid session. Wiring here guarantees the
  // client is ready before any child can render or run an effect.
  const wired = useRef(false);
  if (!wired.current) {
    configureApi({
      getToken: () => sessionRef.current?.token ?? null,
      onUnauthorized: () => persist(null),
    });
    wired.current = true;
  }

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await authApi.login(username, password);
      persist(sessionFromToken(res.access_token, username, res.must_change_password));
      return { mustChangePassword: res.must_change_password };
    },
    [persist],
  );

  const acceptInvite = useCallback(
    async (token: string, password: string) => {
      // The accept endpoint creates the account but returns no token; sign in with
      // the just-chosen password so the user lands authenticated in one step.
      const user = await invitationsApi.acceptInvitation(token, password);
      const res = await authApi.login(user.username, password);
      persist(sessionFromToken(res.access_token, user.username, res.must_change_password));
    },
    [persist],
  );

  const changePassword = useCallback(
    async (currentPassword: string, newPassword: string) => {
      const res = await authApi.changePassword(currentPassword, newPassword);
      const username = sessionRef.current?.username ?? "";
      persist(sessionFromToken(res.access_token, username, res.must_change_password));
    },
    [persist],
  );

  const value = useMemo(
    () => ({ session, login, acceptInvite, changePassword, logout }),
    [session, login, acceptInvite, changePassword, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
