import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AdminRepositoriesPage } from "./AdminRepositoriesPage";
import type { AdminRepository } from "../api/repositories";

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const REPOS: AdminRepository[] = [
  { id: "r-1", name: "Handbook", description: "the company handbook", configured: true },
  { id: "r-2", name: "Runbook", description: null, configured: false },
];

describe("AdminRepositoriesPage — rename & delete", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function mock() {
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.endsWith("/admin/repositories")) return Promise.resolve(json(REPOS));
      const m = /\/repositories\/([^/]+)$/.exec(url);
      if (m && method === "PATCH") {
        const body = JSON.parse(String(init?.body)) as {
          name?: string;
          description?: string | null;
        };
        return Promise.resolve(
          json({
            id: m[1],
            name: body.name ?? "Handbook",
            description: body.description ?? null,
            configured: true,
          }),
        );
      }
      if (m && method === "DELETE") return Promise.resolve(json(null, 204));
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("renames a repository", async () => {
    mock();
    render(<AdminRepositoriesPage />);
    const row = (await screen.findByText("Runbook")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const nameInput = within(row).getByLabelText("Name");
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Runbook v2");
    await userEvent.type(within(row).getByLabelText("Description"), "ops runbook");
    await userEvent.click(within(row).getByRole("button", { name: "Save" }));

    const patch = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "PATCH" && /\/repositories\/r-2$/.test(String(c[0])),
    );
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      name: "Runbook v2",
      description: "ops runbook",
    });
    // The list reflects the new name.
    expect(await screen.findByText("Runbook v2")).toBeInTheDocument();
  });

  it("deletes a repository after name confirmation", async () => {
    mock();
    render(<AdminRepositoriesPage />);
    const row = (await screen.findByText("Handbook")).closest("li") as HTMLElement;

    await userEvent.click(within(row).getByRole("button", { name: "Delete" }));
    await userEvent.type(within(row).getByLabelText("Confirm name"), "Handbook");
    await userEvent.click(within(row).getByRole("button", { name: "Confirm delete" }));

    const del = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "DELETE" && /\/repositories\/r-1$/.test(String(c[0])),
    );
    expect(JSON.parse(String(del?.[1]?.body))).toEqual({ confirm_name: "Handbook" });
    expect(screen.queryByText("Handbook")).not.toBeInTheDocument();
  });
});
