import { api } from "./client";

// Mirrors TokenResponse in src/contextvault/api/auth.py.
export interface TokenResponse {
  access_token: string;
  token_type: string;
  must_change_password: boolean;
}

export function login(username: string, password: string): Promise<TokenResponse> {
  return api.post<TokenResponse>("/auth/login", { username, password });
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<TokenResponse> {
  return api.post<TokenResponse>("/auth/change-password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}
