import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../auth/AuthProvider";
import { AcceptInvitePage } from "./AcceptInvitePage";
import { makeToken } from "../test/tokens";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderAt(entry: string) {
  return render(
    <AuthProvider>
      <MemoryRouter
        initialEntries={[entry]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/accept-invite" element={<AcceptInvitePage />} />
          <Route path="/" element={<div>home</div>} />
          <Route path="/login" element={<div>login screen</div>} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe("AcceptInvitePage", () => {
  const fetchMock = vi.fn();

  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("prefills the token from the ?token= query param", () => {
    renderAt("/accept-invite?token=abc123");
    expect(screen.getByRole("textbox", { name: /invite token/i })).toHaveValue("abc123");
  });

  it("redeems the invite then signs in and lands home", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.endsWith("/invitations/accept")) {
        return Promise.resolve(json({ id: "u-1", username: "newbie", role: "user" }, 201));
      }
      if (url.endsWith("/auth/login")) {
        const token = makeToken({ sub: "u-1", role: "user" });
        return Promise.resolve(
          json({ access_token: token, token_type: "bearer", must_change_password: false }),
        );
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    renderAt("/accept-invite?token=abc123");
    await userEvent.type(screen.getByLabelText("Password"), "sup3rsecret");
    await userEvent.type(screen.getByLabelText("Confirm password"), "sup3rsecret");
    await userEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => expect(screen.getByText("home")).toBeInTheDocument());
    // Accept was called before login.
    const paths = fetchMock.mock.calls.map((c) => c[0] as string);
    expect(paths[0]).toContain("/invitations/accept");
    expect(paths[1]).toContain("/auth/login");
  });

  it("blocks mismatched passwords without calling the API", async () => {
    renderAt("/accept-invite?token=abc");
    await userEvent.type(screen.getByLabelText("Password"), "password1");
    await userEvent.type(screen.getByLabelText("Confirm password"), "password2");
    await userEvent.click(screen.getByRole("button", { name: /activate account/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(/do not match/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("surfaces the backend detail on an invalid token", async () => {
    fetchMock.mockResolvedValue(json({ detail: "Invalid or expired invitation" }, 400));
    renderAt("/accept-invite?token=bad");
    await userEvent.type(screen.getByLabelText("Password"), "password1");
    await userEvent.type(screen.getByLabelText("Confirm password"), "password1");
    await userEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Invalid or expired invitation"),
    );
  });
});
