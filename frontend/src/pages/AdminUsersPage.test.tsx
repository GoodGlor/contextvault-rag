import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminUsersPage } from "./AdminUsersPage";
import type { AdminRepository } from "../api/repositories";
import type { AdminUser } from "../api/users";
import type { Grant } from "../api/grants";

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const USERS: AdminUser[] = [
  { id: "u-1", username: "admin", role: "admin", must_change_password: false, created_at: "x" },
  { id: "u-2", username: "member", role: "user", must_change_password: true, created_at: "x" },
];

const REPOS: AdminRepository[] = [
  { id: "r-1", name: "Handbook", description: null, configured: true },
];

const GRANTS: Grant[] = [{ id: "g-1", user_id: "u-2", repository_id: "r-1", expires_at: null }];

describe("AdminUsersPage", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function mock(opts: { users?: AdminUser[]; repos?: AdminRepository[]; grants?: Grant[] } = {}) {
    const users = opts.users ?? USERS;
    const repos = opts.repos ?? REPOS;
    let grants = opts.grants ?? GRANTS;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/users") && method === "GET") return Promise.resolve(json(users));
      if (/\/users\/[^/]+\/reset-password$/.test(url)) {
        return Promise.resolve(
          json({ temporary_password: "Temp-1234", must_change_password: true }),
        );
      }
      if (/\/users\/[^/]+$/.test(url) && method === "DELETE")
        return Promise.resolve(json(null, 204));
      if (url.endsWith("/invitations") && method === "POST") {
        const body = JSON.parse(String(init?.body)) as { username: string; role: string };
        return Promise.resolve(
          json(
            { token: "invite-tok-xyz", username: body.username, role: body.role, expires_at: "x" },
            201,
          ),
        );
      }
      if (url.endsWith("/admin/repositories")) return Promise.resolve(json(repos));
      const g = /\/repositories\/[^/]+\/grants(?:\/([^/]+))?$/.exec(url);
      if (g) {
        if (method === "GET") return Promise.resolve(json(grants));
        if (method === "POST") {
          const body = JSON.parse(String(init?.body)) as { user_id: string };
          const grant: Grant = {
            id: "g-new",
            user_id: body.user_id,
            repository_id: "r-1",
            expires_at: null,
          };
          grants = [...grants, grant];
          return Promise.resolve(json(grant));
        }
        if (method === "DELETE") return Promise.resolve(json(null, 204));
      }
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("lists users with role and password-change state", async () => {
    mock();
    render(<AdminUsersPage />);
    const accounts = await screen.findByRole("region", { name: "Accounts" });
    expect(within(accounts).getByText("admin")).toBeInTheDocument();
    expect(within(accounts).getByText("member")).toBeInTheDocument();
    // The member owes a forced password change.
    expect(within(accounts).getByText(/change owed/i)).toBeInTheDocument();
  });

  it("invites a user and reveals the one-time token", async () => {
    mock();
    render(<AdminUsersPage />);
    const invite = await screen.findByRole("region", { name: "Invite a user" });

    await userEvent.type(within(invite).getByLabelText("Username"), "newbie");
    await userEvent.selectOptions(within(invite).getByLabelText("Role"), "user");
    await userEvent.click(within(invite).getByRole("button", { name: "Send invite" }));

    const posted = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/invitations"),
    );
    expect(JSON.parse(String(posted?.[1]?.body))).toMatchObject({
      username: "newbie",
      role: "user",
    });
    // The raw token is shown once so the admin can hand over the link.
    expect(await screen.findByText(/invite-tok-xyz/)).toBeInTheDocument();
  });

  it("copies the invite link to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    mock();
    render(<AdminUsersPage />);
    const invite = await screen.findByRole("region", { name: "Invite a user" });

    await userEvent.type(within(invite).getByLabelText("Username"), "newbie");
    await userEvent.click(within(invite).getByRole("button", { name: "Send invite" }));
    await screen.findByText(/invite-tok-xyz/);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    // The full accept-invite URL (origin + path + token) is written to the clipboard.
    expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining("/accept-invite?token=invite-tok-xyz"),
    );
    // The button flips to a "Copied" confirmation.
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("resets a user's password and shows the temporary password once", async () => {
    mock();
    render(<AdminUsersPage />);
    const accounts = await screen.findByRole("region", { name: "Accounts" });
    const row = within(accounts).getByText("member").closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Reset password" }));

    expect(
      String(fetchMock.mock.calls.find((c) => String(c[0]).includes("/reset-password"))?.[0]),
    ).toContain("/users/u-2/reset-password");
    expect(await within(row).findByText(/Temp-1234/)).toBeInTheDocument();
  });

  it("deletes a user after username confirmation", async () => {
    mock();
    render(<AdminUsersPage />);
    const accounts = await screen.findByRole("region", { name: "Accounts" });
    const row = within(accounts).getByText("member").closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Delete" }));
    await userEvent.type(within(row).getByLabelText("Confirm username"), "member");
    await userEvent.click(within(row).getByRole("button", { name: "Confirm delete" }));

    const del = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "DELETE" && /\/users\/u-2$/.test(String(c[0])),
    );
    expect(JSON.parse(String(del?.[1]?.body))).toEqual({ confirm_username: "member" });
    expect(within(accounts).queryByText("member")).not.toBeInTheDocument();
  });

  it("lists, adds, and revokes repository grants", async () => {
    mock();
    render(<AdminUsersPage />);
    const access = await screen.findByRole("region", { name: "Repository access" });
    // The existing grant resolves the user_id to a username (scoped to the grant
    // list so the "Grant to" <option> of the same name doesn't collide).
    const grantList = await within(access).findByRole("list");
    expect(within(grantList).getByText("member")).toBeInTheDocument();

    // Grant access to the admin user.
    await userEvent.selectOptions(within(access).getByLabelText("Grant to"), "u-1");
    await userEvent.click(within(access).getByRole("button", { name: "Grant access" }));
    const granted = fetchMock.mock.calls.find(
      (c) =>
        (c[1]?.method ?? "GET") === "POST" && /\/repositories\/r-1\/grants$/.test(String(c[0])),
    );
    expect(JSON.parse(String(granted?.[1]?.body))).toMatchObject({ user_id: "u-1" });

    // Revoke the member's grant.
    const grantRow = within(grantList).getByText("member").closest("li") as HTMLElement;
    await userEvent.click(within(grantRow).getByRole("button", { name: "Revoke" }));
    const revoked = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "DELETE" && /\/grants\/u-2$/.test(String(c[0])),
    );
    expect(revoked).toBeTruthy();
  });
});
