import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DatabasePanel } from "./DatabasePanel";
import { renderWithRepo, repoValue } from "../../test/renderWithRepo";
import type { DatabaseConnection, ExposedTable } from "../../api/database";

function json(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function connection(overrides: Partial<DatabaseConnection> = {}): DatabaseConnection {
  return {
    id: "conn-1",
    name: "Reporting",
    description: "",
    description_stale: false,
    db_type: "postgres",
    host: "db.internal",
    port: 5432,
    database: "reporting",
    username: "readonly",
    exposed_schema: [],
    ...overrides,
  };
}

const SCHEMA: ExposedTable[] = [
  {
    table: "orders",
    description: "",
    columns: [
      { name: "id", description: "" },
      { name: "total", description: "" },
    ],
  },
  {
    table: "customers",
    description: "",
    columns: [{ name: "email", description: "" }],
  },
];

describe("DatabasePanel", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  /** `conns` is the repository's connection list — `[]` for a repository with
   *  none, which is what shows the setup form. */
  function mock(opts: {
    conns?: DatabaseConnection[];
    createResult?: DatabaseConnection;
    createStatus?: number;
    describeResult?: DatabaseConnection;
    describeStatus?: number;
    getResults?: DatabaseConnection[];
    introspectSchema?: ExposedTable[];
    patchResult?: DatabaseConnection;
  }) {
    const conns = opts.conns ?? [];
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (/\/databases\/[^/]+\/introspect$/.test(url) && method === "POST") {
        return Promise.resolve(json({ schema: opts.introspectSchema ?? [] }));
      }
      // `?language=` since #215, so the pattern must not anchor at the path end.
      if (/\/databases\/[^/]+\/describe(\?.*)?$/.test(url) && method === "POST") {
        return opts.describeStatus === 400
          ? Promise.resolve(json({ detail: "quota exhausted" }, 400))
          : Promise.resolve(
              json(opts.describeResult ?? connection({ description: "Customer order totals." })),
            );
      }
      if (/\/databases\/[^/]+\/description$/.test(url) && method === "PATCH") {
        return Promise.resolve(json(opts.patchResult ?? connection()));
      }
      if (/\/databases\/[^/]+\/schema$/.test(url) && method === "PATCH") {
        return Promise.resolve(json(opts.patchResult ?? connection()));
      }
      if (/\/databases\/[^/]+$/.test(url)) {
        if (method === "DELETE") return Promise.resolve(json(null, 204));
        if (method === "GET") {
          // #212 polls this one after saving an allow-list, so a test can hand
          // back a sequence: the description is not there, then it is.
          const next = opts.getResults?.shift();
          return Promise.resolve(json(next ?? conns[0] ?? null));
        }
      }
      if (/\/repositories\/[^/]+\/databases$/.test(url)) {
        if (method === "GET") return Promise.resolve(json(conns));
        if (method === "POST") {
          const status = opts.createStatus ?? 201;
          return status === 201
            ? Promise.resolve(json(opts.createResult ?? connection(), 201))
            : Promise.resolve(json({ detail: "duplicate" }, status));
        }
      }
      throw new Error(`unexpected fetch ${method} ${url}`);
    });
  }

  it("renders the connection setup form when no connection exists", async () => {
    mock({ conns: [] });
    renderWithRepo(<DatabasePanel />);
    expect(await screen.findByLabelText("Name")).toBeInTheDocument();
    expect(screen.getByLabelText("Host")).toBeInTheDocument();
    expect(screen.getByLabelText("Port")).toBeInTheDocument();
    expect(screen.getByLabelText("Database")).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("submits the setup form and creates a connection with typed values", async () => {
    mock({ conns: [], createResult: connection() });
    renderWithRepo(<DatabasePanel />);
    await screen.findByLabelText("Host");

    await userEvent.type(screen.getByLabelText("Name"), "Apple financials");
    await userEvent.selectOptions(screen.getByLabelText("Database type"), "postgres");
    await userEvent.type(screen.getByLabelText("Host"), "db.internal");
    const portInput = screen.getByLabelText("Port");
    await userEvent.clear(portInput);
    await userEvent.type(portInput, "5432");
    await userEvent.type(screen.getByLabelText("Database"), "reporting");
    await userEvent.type(screen.getByLabelText("Username"), "readonly");
    await userEvent.type(screen.getByLabelText("Password"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    const post = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "POST" && String(c[0]).endsWith("/databases"),
    );
    expect(String(post?.[0])).toContain("/repositories/r-1/databases");
    const body = JSON.parse(String(post?.[1]?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      name: "Apple financials",
      db_type: "postgres",
      host: "db.internal",
      port: 5432,
      database: "reporting",
      username: "readonly",
      password: "s3cret",
    });
    expect(typeof body.port).toBe("number");

    // After a successful connect, the connected summary shows.
    expect(await screen.findByRole("button", { name: "Introspect" })).toBeInTheDocument();
  });

  it("shows a masked connected summary — never the password", async () => {
    mock({ conns: [connection()] });
    renderWithRepo(<DatabasePanel />);
    // Exact summary lines, not a loose /db\.internal/ — the connection list now
    // also prints host/database in its row, and a substring match would find
    // both and stop distinguishing the summary from the picker.
    expect(await screen.findByText("Host: db.internal")).toBeInTheDocument();
    expect(screen.getByText("Database: reporting")).toBeInTheDocument();
    expect(screen.getByText("Username: readonly")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Introspect" })).toBeInTheDocument();
    expect(screen.queryByText(/s3cret/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });

  it("renders tables with description inputs and checkboxes after introspecting", async () => {
    mock({ conns: [connection()], introspectSchema: SCHEMA });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Introspect" }));

    expect(await screen.findByText("orders")).toBeInTheDocument();
    expect(screen.getByText("customers")).toBeInTheDocument();
    expect(screen.getByLabelText("Include table orders")).toBeInTheDocument();
    expect(screen.getByLabelText("Include column id")).toBeInTheDocument();
    expect(screen.getByLabelText("Include column total")).toBeInTheDocument();
    expect(screen.getByLabelText("Include column email")).toBeInTheDocument();
    expect(screen.getAllByPlaceholderText("Table description")).toHaveLength(2);
    expect(screen.getAllByPlaceholderText("Column description")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Save allow-list" })).toBeInTheDocument();
  });

  it("collapses every table after introspecting, allow-listed ones included", async () => {
    // "orders" is already in the saved allow-list, "customers" is not — neither
    // may open on its own, or a dozen exposed tables bury the save button.
    mock({ conns: [connection({ exposed_schema: [SCHEMA[0]] })], introspectSchema: SCHEMA });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Introspect" }));
    await screen.findByText("orders");

    expect(screen.getByLabelText("Include table orders")).toBeChecked();
    expect(screen.getByLabelText("Include column total")).not.toBeVisible();
    expect(screen.getByLabelText("Include column email")).not.toBeVisible();

    // ...but including a table reveals its columns, so it can be curated.
    await userEvent.click(screen.getByLabelText("Include table customers"));
    expect(screen.getByLabelText("Include column email")).toBeVisible();
  });

  it("saves the allow-list with only checked tables/columns", async () => {
    mock({ conns: [connection()], introspectSchema: SCHEMA });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Introspect" }));
    await screen.findByText("orders");

    // Include "orders" and only its "total" column with a description; leave
    // "customers" unchecked entirely.
    await userEvent.click(screen.getByLabelText("Include table orders"));
    await userEvent.click(screen.getByLabelText("Include column total"));
    const ordersRow = screen.getByText("orders").closest("li") as HTMLElement;
    await userEvent.type(
      within(ordersRow).getByPlaceholderText("Table description"),
      "Customer orders",
    );
    const totalColumn = screen.getByLabelText("Include column total").closest("li") as HTMLElement;
    await userEvent.type(
      within(totalColumn).getByPlaceholderText("Column description"),
      "Order total",
    );

    await userEvent.click(screen.getByRole("button", { name: "Save allow-list" }));

    const patch = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "PATCH");
    const body = JSON.parse(String(patch?.[1]?.body)) as { exposed_schema: ExposedTable[] };
    expect(body.exposed_schema).toEqual([
      {
        table: "orders",
        description: "Customer orders",
        columns: [{ name: "total", description: "Order total" }],
      },
    ]);
  });

  it("deletes the connection after confirming", async () => {
    mock({ conns: [connection()] });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete connection" }));

    expect(window.confirm).toHaveBeenCalled();
    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "DELETE");
    expect(String(del?.[0])).toContain("/repositories/r-1/databases/conn-1");
    // Back to the setup form once the last one is deleted.
    expect(await screen.findByRole("button", { name: "Connect" })).toBeInTheDocument();
  });

  it("does not delete when the confirmation is declined", async () => {
    mock({ conns: [connection()] });
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete connection" }));

    const del = fetchMock.mock.calls.find((c) => (c[1]?.method ?? "GET") === "DELETE");
    expect(del).toBeUndefined();
  });

  it("lists every connection and switches the curation panel between them", async () => {
    mock({
      conns: [
        connection({ id: "conn-apple", name: "Apple financials", database: "apple" }),
        connection({ id: "conn-msft", name: "Microsoft financials", database: "microsoft" }),
      ],
    });
    renderWithRepo(<DatabasePanel />);

    expect(await screen.findByRole("button", { name: /Apple financials/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Microsoft financials/ })).toBeInTheDocument();

    // With several, none is auto-selected — picking is the admin's move.
    expect(screen.queryByRole("button", { name: "Introspect" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Apple financials/ }));
    expect(await screen.findByText("Database: apple")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Microsoft financials/ }));
    expect(await screen.findByText("Database: microsoft")).toBeInTheDocument();
    expect(screen.queryByText("Database: apple")).not.toBeInTheDocument();
  });

  it("selects the only connection automatically", async () => {
    // A repository with one is the common case and used to open straight onto
    // the curation UI; making it a list of one to click would be a regression.
    mock({ conns: [connection()] });
    renderWithRepo(<DatabasePanel />);
    expect(await screen.findByRole("button", { name: "Introspect" })).toBeInTheDocument();
  });

  it("reports a duplicate name as a naming problem, not a connection failure", async () => {
    // A 409 through the generic handler would say "could not connect" and send
    // an admin to check the host, which is reachable and not the problem.
    mock({ conns: [connection()], createStatus: 409 });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Add a database" }));

    await userEvent.type(screen.getByLabelText("Name"), "Reporting");
    await userEvent.type(screen.getByLabelText("Host"), "db.internal");
    await userEvent.type(screen.getByLabelText("Port"), "5432");
    await userEvent.type(screen.getByLabelText("Database"), "reporting");
    await userEvent.type(screen.getByLabelText("Username"), "readonly");
    await userEvent.type(screen.getByLabelText("Password"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(
      await screen.findByText("A database with this name already exists in this repository."),
    ).toBeInTheDocument();
  });

  it("generates a description and puts it in the editable field", async () => {
    mock({ conns: [connection()] });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Describe" }));

    expect(await screen.findByLabelText("Description")).toHaveValue("Customer order totals.");
  });

  it("warns when the description is older than the allow-list", async () => {
    // #203 routes on this text, so a description outliving the allow-list it
    // describes has to be visible rather than silently wrong.
    mock({ conns: [connection({ description: "Old summary.", description_stale: true })] });
    renderWithRepo(<DatabasePanel />);
    expect(
      await screen.findByText("Out of date — the allow-list changed since this was written."),
    ).toBeInTheDocument();
  });

  it("does not warn about a connection nobody has described", async () => {
    // Absent is not out-of-date; a warning on every new connection is a warning
    // an admin learns to ignore.
    mock({ conns: [connection()] });
    renderWithRepo(<DatabasePanel />);
    await screen.findByLabelText("Description");
    expect(
      screen.queryByText("Out of date — the allow-list changed since this was written."),
    ).not.toBeInTheDocument();
  });

  it("saves a hand-corrected description", async () => {
    mock({ conns: [connection({ description: "Wrong." })] });
    renderWithRepo(<DatabasePanel />);
    const field = await screen.findByLabelText("Description");
    await userEvent.clear(field);
    await userEvent.type(field, "Apple financials.");
    await userEvent.click(screen.getByRole("button", { name: "Save description" }));

    const patch = fetchMock.mock.calls.find(
      (c) => (c[1]?.method ?? "GET") === "PATCH" && String(c[0]).endsWith("/description"),
    );
    const body = JSON.parse(String(patch?.[1]?.body)) as { description: string };
    expect(body.description).toBe("Apple financials.");
  });

  it("reports a failed generation without losing the connection", async () => {
    // Ruling 3: failure never blocks. The panel keeps working.
    mock({ conns: [connection()], describeStatus: 400 });
    renderWithRepo(<DatabasePanel />);
    await userEvent.click(await screen.findByRole("button", { name: "Describe" }));

    expect(await screen.findByText(/quota exhausted/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Introspect" })).toBeInTheDocument();
  });

  describe("the panel's controls are grouped (#211)", () => {
    /** jsdom performs no layout — every rect is zero — so these assert the
     *  STRUCTURE the fix's CSS hangs off, not the geometry. The non-overlap
     *  itself is only observable in a real browser; `e2e/database-panel.spec.ts`
     *  is the guard that can see it. */

    it("puts the description's two buttons in one action row", async () => {
      mock({ conns: [connection()] });
      renderWithRepo(<DatabasePanel />, repoValue());

      const describe = await screen.findByRole("button", { name: "Describe" });
      const row = describe.closest(".database-actions");
      expect(row).not.toBeNull();
      expect(
        within(row as HTMLElement).getByRole("button", { name: "Save description" }),
      ).toBeInTheDocument();
    });

    it("marks disconnecting as destructive, not just as the last button", async () => {
      mock({ conns: [connection()] });
      renderWithRepo(<DatabasePanel />, repoValue());

      const remove = await screen.findByRole("button", { name: "Delete connection" });
      expect(remove).toHaveClass("danger");
    });

    it("keeps the destructive action out of the description's row", async () => {
      /** They sit in separate groups on purpose: one row is about the text
       *  above it, the other is about the connection. */
      mock({ conns: [connection()] });
      renderWithRepo(<DatabasePanel />, repoValue());

      const remove = await screen.findByRole("button", { name: "Delete connection" });
      const row = remove.closest(".database-actions") as HTMLElement;
      expect(within(row).queryByRole("button", { name: "Describe" })).toBeNull();
    });
  });

  describe("the description writes itself (#212)", () => {
    it("waits for the background description after an allow-list save", async () => {
      /** The save's own answer predates the description — the task is queued as
       *  that response is composed — so the panel has to look again. */
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const written = connection({ description: "Customer order totals." });
      mock({
        conns: [connection()],
        introspectSchema: SCHEMA,
        // A saved allow-list is what makes a description possible at all.
        patchResult: connection({
          description: "",
          description_stale: false,
          exposed_schema: SCHEMA,
        }),
        getResults: [written],
      });
      renderWithRepo(<DatabasePanel />, repoValue());
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

      await user.click(await screen.findByRole("button", { name: "Introspect" }));
      await user.click(await screen.findByRole("button", { name: "Save allow-list" }));

      expect(await screen.findByText("The description is being written…")).toBeInTheDocument();
      await vi.advanceTimersByTimeAsync(1600);
      await screen.findByDisplayValue("Customer order totals.");
      vi.useRealTimers();
    });

    it("does not wait when the save already carries a current description", async () => {
      /** Re-saving curation that changed nothing queues no task server-side, so
       *  claiming one is coming would be a lie the panel tells on its own. */
      mock({
        conns: [connection()],
        introspectSchema: SCHEMA,
        patchResult: connection({
          description: "Already written.",
          description_stale: false,
          exposed_schema: SCHEMA,
        }),
      });
      renderWithRepo(<DatabasePanel />, repoValue());
      const user = userEvent.setup();

      await user.click(await screen.findByRole("button", { name: "Introspect" }));
      await user.click(await screen.findByRole("button", { name: "Save allow-list" }));

      expect(await screen.findByText("Allow-list saved.")).toBeInTheDocument();
      expect(screen.queryByText("The description is being written…")).toBeNull();
    });

    it("gives up quietly rather than claiming a description that never came", async () => {
      /** #212 stores no "it failed" state deliberately, so the panel must stop
       *  saying anything and leave Describe as the retry that shows why. */
      vi.useFakeTimers({ shouldAdvanceTime: true });
      mock({
        conns: [connection()],
        introspectSchema: SCHEMA,
        // A saved allow-list is what makes a description possible at all.
        patchResult: connection({
          description: "",
          description_stale: false,
          exposed_schema: SCHEMA,
        }),
        getResults: [],
      });
      renderWithRepo(<DatabasePanel />, repoValue());
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

      await user.click(await screen.findByRole("button", { name: "Introspect" }));
      await user.click(await screen.findByRole("button", { name: "Save allow-list" }));
      expect(await screen.findByText("The description is being written…")).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(1500 * 9);
      await waitFor(() =>
        expect(screen.queryByText("The description is being written…")).toBeNull(),
      );
      expect(screen.getByRole("button", { name: "Describe" })).toBeEnabled();
      vi.useRealTimers();
    });
  });

  describe("the description's language (#215)", () => {
    it("tells the server which interface language to write in", async () => {
      /** The description is read by a person, in the interface they chose; the
       *  server has no other way to know which that is. */
      mock({ conns: [connection()], introspectSchema: SCHEMA });
      renderWithRepo(<DatabasePanel />, repoValue());
      const user = userEvent.setup();

      await user.click(await screen.findByRole("button", { name: "Introspect" }));
      await user.click(await screen.findByRole("button", { name: "Save allow-list" }));

      const call = fetchMock.mock.calls.find(
        ([url, init]) =>
          /\/schema$/.test(url as string) && (init as RequestInit)?.method === "PATCH",
      );
      expect(call).toBeDefined();
      const body = JSON.parse((call?.[1] as RequestInit).body as string);
      expect(body.language).toBeTruthy();
    });

    it("asks an explicit re-describe in the same language", async () => {
      mock({ conns: [connection()] });
      renderWithRepo(<DatabasePanel />, repoValue());
      const user = userEvent.setup();

      await user.click(await screen.findByRole("button", { name: "Describe" }));

      const call = fetchMock.mock.calls.find(([url]) => /\/describe/.test(url as string));
      expect(call?.[0]).toContain("language=");
    });
  });

  it("shows the no-repos message when there is no current repository", async () => {
    renderWithRepo(<DatabasePanel />, repoValue({ repos: [], currentRepoId: "" }));
    expect(
      await screen.findByText("No repositories yet. Create one under Repositories first."),
    ).toBeInTheDocument();
  });
});
