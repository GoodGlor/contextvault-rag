// Test helper: forge a JWT-shaped string with the given claims. The signature is
// never verified on the client, so a dummy segment is fine.
export function makeToken(claims: Record<string, unknown>): string {
  const b64url = (obj: unknown) =>
    btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url(claims)}.sig`;
}
