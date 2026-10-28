import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "../auth/AuthProvider";
import { ChangePasswordPage } from "./ChangePasswordPage";
import { makeToken } from "../test/tokens";

const STORAGE_KEY = "contextvault.session";

function renderPage() {
  const token = makeToken({ sub: "u-1", role: "user" });
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      token,
      userId: "u-1",
      role: "user",
      username: "alice",
      mustChangePassword: false,
    }),
  );
  return render(
    <AuthProvider>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/" element={<ChangePasswordPage />} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

describe("ChangePasswordPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("rejects a mismatched confirmation without calling the API", async () => {
    renderPage();
    await userEvent.type(screen.getByLabelText("Current password"), "oldpassword");
    await userEvent.type(screen.getByLabelText("New password"), "brandnew1");
    await userEvent.type(screen.getByLabelText("Confirm new password"), "brandnew2");
    await userEvent.click(screen.getByRole("button", { name: /save password/i }));

    expect(screen.getByRole("alert")).toHaveTextContent(/do not match/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
