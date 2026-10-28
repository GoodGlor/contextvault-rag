import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import userEvent from "@testing-library/user-event";
import { AuthProvider } from "./AuthProvider";
import { useAuth } from "./AuthContext";
import { api } from "../api/client";
import { makeToken } from "../test/tokens";

const STORAGE_KEY = "contextvault.session";

function Probe() {
  const { session, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="who">{session ? `${session.username}:${session.role}` : "anon"}</span>
      <button onClick={() => void login("alice", "pw")}>login</button>
      <button onClick={logout}>logout</button>
    </div>
  );
}

// A child that fires an authenticated request on mount, mimicking a page like
// QueryPage that loads data as soon as it renders. React runs child effects
// before parent effects, so this is the earliest a request can go out.
function ChildThatFetchesOnMount() {
  useEffect(() => {
    void api.get("/repositories");
  }, []);
  return null;
}

describe("AuthProvider", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("stores a session on login and persists it to localStorage", async () => {
    const token = makeToken({ sub: "u-1", role: "admin" });
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ access_token: token, token_type: "bearer", must_change_password: false }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("who")).toHaveTextContent("anon");

    await userEvent.click(screen.getByText("login"));

    await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("alice:admin"));
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
    expect(stored).toMatchObject({ userId: "u-1", role: "admin", username: "alice" });
  });

  it("hydrates an existing session from localStorage on mount", () => {
    const token = makeToken({ sub: "u-9", role: "user" });
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token,
        userId: "u-9",
        role: "user",
        username: "bob",
        mustChangePassword: false,
      }),
    );
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("who")).toHaveTextContent("bob:user");
  });

  it("clears the session and storage on logout", async () => {
    const token = makeToken({ sub: "u-9", role: "user" });
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token,
        userId: "u-9",
        role: "user",
        username: "bob",
        mustChangePassword: false,
      }),
    );
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await userEvent.click(screen.getByText("logout"));
    expect(screen.getByTestId("who")).toHaveTextContent("anon");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("discards a stored session whose token has expired", () => {
    const token = makeToken({ sub: "u-9", role: "user", exp: Math.floor(Date.now() / 1000) - 60 });
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token,
        userId: "u-9",
        role: "user",
        username: "bob",
        mustChangePassword: false,
      }),
    );
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("who")).toHaveTextContent("anon");
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("wires the API client synchronously, so a child's mount-time request carries the token", async () => {
    const token = makeToken({
      sub: "u-9",
      role: "user",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token,
        userId: "u-9",
        role: "user",
        username: "bob",
        mustChangePassword: false,
      }),
    );
    fetchMock.mockResolvedValue(new Response("[]", { status: 200 }));

    render(
      <AuthProvider>
        <ChildThatFetchesOnMount />
      </AuthProvider>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe(`Bearer ${token}`);
  });

  it("keeps a stored session whose token is still valid", () => {
    const token = makeToken({
      sub: "u-9",
      role: "user",
      exp: Math.floor(Date.now() / 1000) + 3600,
    });
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token,
        userId: "u-9",
        role: "user",
        username: "bob",
        mustChangePassword: false,
      }),
    );
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    expect(screen.getByTestId("who")).toHaveTextContent("bob:user");
  });
});
