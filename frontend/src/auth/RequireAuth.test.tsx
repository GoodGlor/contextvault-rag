import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./AuthProvider";
import { RequireAuth } from "./RequireAuth";
import { makeToken } from "../test/tokens";

const STORAGE_KEY = "contextvault.session";

function seed(session: { role: "admin" | "user"; mustChangePassword?: boolean }): void {
  const token = makeToken({ sub: "u-1", role: session.role });
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      token,
      userId: "u-1",
      role: session.role,
      username: "alice",
      mustChangePassword: session.mustChangePassword ?? false,
    }),
  );
}

function renderAt(path: string, requireAdmin = false) {
  return render(
    <AuthProvider>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <Routes>
          <Route path="/login" element={<div>login screen</div>} />
          <Route path="/change-password" element={<div>change password</div>} />
          <Route
            path="/secret"
            element={
              <RequireAuth requireAdmin={requireAdmin}>
                <div>secret content</div>
              </RequireAuth>
            }
          />
          <Route path="/" element={<div>home</div>} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe("RequireAuth", () => {
  it("redirects an anonymous visitor to /login", () => {
    renderAt("/secret");
    expect(screen.getByText("login screen")).toBeInTheDocument();
  });

  it("renders the protected content for an authenticated user", () => {
    seed({ role: "user" });
    renderAt("/secret");
    expect(screen.getByText("secret content")).toBeInTheDocument();
  });

  it("bounces a flagged user to /change-password", () => {
    seed({ role: "user", mustChangePassword: true });
    renderAt("/secret");
    expect(screen.getByText("change password")).toBeInTheDocument();
  });

  it("sends a non-admin home from an admin-only route", () => {
    seed({ role: "user" });
    renderAt("/secret", true);
    expect(screen.getByText("home")).toBeInTheDocument();
  });

  it("admits an admin to an admin-only route", () => {
    seed({ role: "admin" });
    renderAt("/secret", true);
    expect(screen.getByText("secret content")).toBeInTheDocument();
  });
});
