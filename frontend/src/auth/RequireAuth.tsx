import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

/**
 * Gate a route on an authenticated session.
 *
 * - No session → redirect to /login (remembering where the user was headed).
 * - A session that owes a forced password change → redirect to /change-password,
 *   the one authenticated screen reachable while flagged (mirrors the backend's
 *   get_current_user bounce).
 * - `requireAdmin` additionally sends non-admins home.
 */
export function RequireAuth({
  children,
  requireAdmin = false,
}: {
  children: ReactNode;
  requireAdmin?: boolean;
}): ReactNode {
  const { session } = useAuth();
  const location = useLocation();

  if (session === null) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (session.mustChangePassword) {
    return <Navigate to="/change-password" replace />;
  }
  if (requireAdmin && session.role !== "admin") {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

/**
 * Gate a route on a session *without* the forced-change bounce, so the
 * change-password screen stays reachable while a user is flagged (otherwise
 * RequireAuth would redirect it back onto itself).
 */
export function RequireSession({ children }: { children: ReactNode }): ReactNode {
  const { session } = useAuth();
  const location = useLocation();
  if (session === null) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <>{children}</>;
}
