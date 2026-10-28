import { describe, expect, it } from "vitest";
import { decodeToken } from "./jwt";
import { makeToken } from "../test/tokens";

describe("decodeToken", () => {
  it("reads sub and role claims", () => {
    const token = makeToken({ sub: "user-123", role: "admin", exp: 4102444800 });
    expect(decodeToken(token)).toEqual({ sub: "user-123", role: "admin", exp: 4102444800 });
  });

  it("defaults an unknown role to 'user'", () => {
    const token = makeToken({ sub: "u", role: "superuser" });
    expect(decodeToken(token)?.role).toBe("user");
  });

  it("returns null for a token without three segments", () => {
    expect(decodeToken("not-a-jwt")).toBeNull();
  });

  it("returns null when the payload lacks a string sub", () => {
    expect(decodeToken(makeToken({ role: "user" }))).toBeNull();
  });
});
