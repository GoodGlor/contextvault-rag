import { api } from "./client";
import type { Role } from "./invitations";

// Mirrors UserResponse in src/contextvault/api/users.py (never the password hash).
export interface AdminUser {
  id: string;
  username: string;
  role: Role;
  must_change_password: boolean;
  created_at: string;
}

/** List all user accounts (admin-only). */
export function listUsers(): Promise<AdminUser[]> {
  return api.get<AdminUser[]>("/users");
}

// Mirrors ResetPasswordResponse — the temporary password is returned once.
export interface ResetPasswordResult {
  temporary_password: string;
  must_change_password: boolean;
}

/** Issue a random temporary password for a user; forces a change on next login. */
export function resetUserPassword(userId: string): Promise<ResetPasswordResult> {
  return api.post<ResetPasswordResult>(`/users/${userId}/reset-password`);
}

/** Permanently delete a user; the caller must echo the target's username. */
export function deleteUser(userId: string, confirmUsername: string): Promise<void> {
  return api.del<void>(`/users/${userId}`, { confirm_username: confirmUsername });
}
