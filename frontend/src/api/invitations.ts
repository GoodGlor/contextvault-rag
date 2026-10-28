import { api } from "./client";

export type Role = "admin" | "user";

// Mirrors AcceptedUserResponse in src/contextvault/api/invitations.py.
export interface AcceptedUser {
  id: string;
  username: string;
  role: Role;
}

/** Redeem an invite token by choosing a password; creates the account (public). */
export function acceptInvitation(token: string, password: string): Promise<AcceptedUser> {
  return api.post<AcceptedUser>("/invitations/accept", { token, password });
}

// Mirrors InvitationResponse in src/contextvault/api/invitations.py. The raw
// token is shown **once** — it is never stored or re-shown by the backend.
export interface Invitation {
  token: string;
  username: string;
  role: Role;
  expires_at: string;
}

/** Issue an onboarding invite for a new account (admin-only). */
export function createInvitation(input: {
  username: string;
  role: Role;
  expires_in_hours?: number;
}): Promise<Invitation> {
  return api.post<Invitation>("/invitations", input);
}
