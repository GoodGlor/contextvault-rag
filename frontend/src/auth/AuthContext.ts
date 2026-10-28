import { createContext, useContext } from "react";
import type { Role } from "./jwt";

/** The signed-in user's derived session (token + claims + display name). */
export interface Session {
  token: string;
  userId: string;
  role: Role;
  username: string;
  mustChangePassword: boolean;
}

export interface AuthContextValue {
  session: Session | null;
  /** Authenticate and store the session; returns whether a password change is owed. */
  login: (username: string, password: string) => Promise<{ mustChangePassword: boolean }>;
  /** Redeem an invite (create the account) then sign straight in with the new password. */
  acceptInvite: (token: string, password: string) => Promise<void>;
  /** Complete a password change; clears the forced-change flag and stores a fresh token. */
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>;
  logout: () => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
