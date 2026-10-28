// Minimal, dependency-free decode of a JWT payload. The backend signs tokens with
// `sub` (the user's UUID) and `role` ("admin"/"user") claims (see core/tokens.py).
// We never verify the signature on the client — the server does that on every
// request; we only read claims to drive routing (e.g. admin-only sections).

export type Role = "admin" | "user";

export interface TokenClaims {
  sub: string;
  role: Role;
  exp?: number;
}

function base64UrlDecode(segment: string): string {
  const padded = segment.replace(/-/g, "+").replace(/_/g, "/");
  const withPadding = padded + "=".repeat((4 - (padded.length % 4)) % 4);
  return atob(withPadding);
}

/** Decode a JWT's claims, or return null if the token is malformed. */
export function decodeToken(token: string): TokenClaims | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = JSON.parse(base64UrlDecode(parts[1])) as Record<string, unknown>;
    if (typeof payload.sub !== "string") return null;
    const role = payload.role === "admin" ? "admin" : "user";
    const exp = typeof payload.exp === "number" ? payload.exp : undefined;
    return { sub: payload.sub, role, exp };
  } catch {
    return null;
  }
}
