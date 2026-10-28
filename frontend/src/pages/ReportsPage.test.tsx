import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReportsPage, REPORT_POLL_MS } from "./ReportsPage";
import type { Report, Schedule } from "../api/reports";
import { renderWithRepo } from "../test/renderWithRepo";

vi.mock("../api/reports", async () => {
  const actual = await vi.importActual<typeof import("../api/reports")>("../api/reports");
  return {
    ...actual,
    createReport: vi.fn(),
    listReports: vi.fn(),
    downloadReport: vi.fn(),
    deleteReport: vi.fn(),
    listSchedules: vi.fn(),
    patchSchedule: vi.fn(),
    deleteSchedule: vi.fn(),
    replanReport: vi.fn(),
    approveReport: vi.fn(),
    listReportTemplates: vi.fn(),
    listReportDatabases: vi.fn(),
    getReportDesign: vi.fn(),
    setReportDesign: vi.fn(),
  };
});

import {
  approveReport,
  createReport,
  deleteReport,
  deleteSchedule,
  downloadReport,
  getReportDesign,
  listReportDatabases,
  listReports,
  listReportTemplates,
  listSchedules,
  patchSchedule,
  replanReport,
  setReportDesign,
} from "../api/reports";

/** The repository's database, as the plan fixtures below route to it. */
const DB = "11111111-1111-1111-1111-111111111111";

function report(overrides: Partial<Report> = {}): Report {
  return {
    id: "rep-1",
    repository_id: "r-1",
    prompt: "Monthly revenue by region",
    status: "pending",
    error: null,
    created_at: "2026-07-20T10:00:00Z",
    has_pdf: false,
    has_pptx: false,
    schedule_id: null,
    plan: null,
    plan_rounds: 0,
    can_rerender: false,
    template: null,
    ...overrides,
  };
}

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sch-1",
    repository_id: "r-1",
    prompt: "Monthly revenue by region",
    run_at_time: "02:00",
    enabled: true,
    last_run_at: null,
    last_error: null,
    created_at: "2026-07-20T10:00:00Z",
    ...overrides,
  };
}

describe("ReportsPage", () => {
  beforeEach(() => {
    vi.mocked(listSchedules).mockResolvedValue([]);
    vi.mocked(listReportTemplates).mockResolvedValue([]);
    vi.mocked(listReportDatabases).mockResolvedValue([{ id: DB, name: "Apple", description: "" }]);
    // PlanDesignStep's useDesignDraft fetches this on mount. Left unmocked, it
    // falls through to the real `getReportDesign`/`setReportDesign` — a real
    // `fetch` under jsdom whose rejection is swallowed by the hook's own
    // `.catch`, so the test still passes but the resulting state update lands
    // outside `act`, after assertions have already run.
    vi.mocked(getReportDesign).mockResolvedValue({
      template: null,
      blocks_version: 1,
      blocks: [],
      stored: false,
      sections: [],
      title: null,
      typography: {},
      page: {},
      brand: {},
    } as never);
    vi.mocked(setReportDesign).mockResolvedValue({} as never);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("submits a prompt, calls createReport, and shows the new row as generating", async () => {
    vi.mocked(listReports).mockResolvedValue([]);
    vi.mocked(createReport).mockResolvedValue(report({ status: "pending" }));
    renderWithRepo(<ReportsPage />);
    await screen.findByLabelText("Prompt");

    await userEvent.type(screen.getByLabelText("Prompt"), "Monthly revenue by region");
    await userEvent.click(screen.getByRole("button", { name: "Plan report" }));

    expect(createReport).toHaveBeenCalledWith("r-1", "Monthly revenue by region");
    expect(await screen.findByText("Monthly revenue by region")).toBeInTheDocument();
    expect(screen.getByText("Generating…")).toBeInTheDocument();
  });

  it("polls while a report is generating and stops once it reaches done", async () => {
    vi.useFakeTimers();
    try {
      let call = 0;
      vi.mocked(listReports).mockImplementation(() => {
        call += 1;
        return Promise.resolve([
          report({ status: call === 1 ? "processing" : "done", has_pdf: true }),
        ]);
      });
      renderWithRepo(<ReportsPage />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.getByText("Generating…")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(REPORT_POLL_MS);
      });
      expect(screen.getByRole("button", { name: "Download PDF" })).toBeInTheDocument();

      const callsAfterDone = vi.mocked(listReports).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(REPORT_POLL_MS * 3);
      });
      // No further polling once every report is terminal.
      expect(vi.mocked(listReports).mock.calls.length).toBe(callsAfterDone);
    } finally {
      vi.useRealTimers();
    }
  });

  it("downloads a done report's PDF and revokes the object URL", async () => {
    vi.mocked(listReports).mockResolvedValue([report({ status: "done", has_pdf: true })]);
    const blob = new Blob(["%PDF"], { type: "application/pdf" });
    vi.mocked(downloadReport).mockResolvedValue(blob);

    const createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });

    renderWithRepo(<ReportsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Download PDF" }));

    expect(downloadReport).toHaveBeenCalledWith("r-1", "rep-1", "pdf");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

    vi.unstubAllGlobals();
  });

  it("offers a PPTX download only when that artifact exists", async () => {
    // Reports generated before PPTX existed have no deck; offering the button
    // would hand the reader a 404.
    vi.mocked(listReports).mockResolvedValue([
      report({ status: "done", has_pdf: true, has_pptx: false }),
    ]);
    const { unmount } = renderWithRepo(<ReportsPage />);
    expect(await screen.findByRole("button", { name: "Download PDF" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download PPTX" })).not.toBeInTheDocument();
    unmount();

    vi.mocked(listReports).mockResolvedValue([
      report({ status: "done", has_pdf: true, has_pptx: true }),
    ]);
    const blob = new Blob(["PK"], { type: "application/vnd.ms-powerpoint" });
    vi.mocked(downloadReport).mockResolvedValue(blob);
    const createObjectURL = vi.fn().mockReturnValue("blob:mock-url");
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL: vi.fn() });

    renderWithRepo(<ReportsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Download PPTX" }));
    expect(downloadReport).toHaveBeenCalledWith("r-1", "rep-1", "pptx");

    vi.unstubAllGlobals();
  });

  it("shows when a report was created", async () => {
    vi.mocked(listReports).mockResolvedValue([
      report({ status: "done", created_at: "2026-08-01T14:30:00Z" }),
    ]);
    renderWithRepo(<ReportsPage />);
    const stamp = await screen.findByText(new Date("2026-08-01T14:30:00Z").toLocaleString());
    // The machine-readable value stays exact regardless of how it is displayed.
    expect(stamp).toHaveAttribute("datetime", "2026-08-01T14:30:00Z");
  });

  it("deletes a report after confirming and drops it from the list", async () => {
    vi.mocked(listReports).mockResolvedValue([report({ status: "done", has_pdf: true })]);
    vi.mocked(deleteReport).mockResolvedValue(undefined);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));

    renderWithRepo(<ReportsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(deleteReport).toHaveBeenCalledWith("r-1", "rep-1");
    await waitFor(() =>
      expect(screen.queryByText("Monthly revenue by region")).not.toBeInTheDocument(),
    );
    vi.unstubAllGlobals();
  });

  it("does not delete when the confirmation is declined", async () => {
    vi.mocked(listReports).mockResolvedValue([report({ status: "done", has_pdf: true })]);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(false));

    renderWithRepo(<ReportsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(deleteReport).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("shows the error text for a failed report", async () => {
    vi.mocked(listReports).mockResolvedValue([
      report({ status: "failed", error: "SQL guardrail rejected the query" }),
    ]);
    renderWithRepo(<ReportsPage />);
    expect(await screen.findByText("SQL guardrail rejected the query")).toBeInTheDocument();
  });

  it("offers no nightly-repeat action on a done report (scheduling is deferred)", async () => {
    vi.mocked(listReports).mockResolvedValue([report({ status: "done", has_pdf: true })]);
    renderWithRepo(<ReportsPage />);

    expect(await screen.findByRole("button", { name: "Download PDF" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Repeat nightly…" })).not.toBeInTheDocument();
  });

  it("explains that reports draw on documents and the connected database", async () => {
    vi.mocked(listReports).mockResolvedValue([]);
    renderWithRepo(<ReportsPage />);

    expect(
      await screen.findByText(
        "The report draws on this repository's documents and, if connected, its reporting database.",
      ),
    ).toBeInTheDocument();
  });

  it("lists schedules and toggles enabled via patchSchedule", async () => {
    vi.mocked(listReports).mockResolvedValue([]);
    vi.mocked(listSchedules).mockResolvedValue([schedule({ enabled: true })]);
    vi.mocked(patchSchedule).mockResolvedValue(schedule({ enabled: false }));

    renderWithRepo(<ReportsPage />);
    const row = (await screen.findByText("Monthly revenue by region")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("checkbox"));

    expect(patchSchedule).toHaveBeenCalledWith("sch-1", { enabled: false });
  });

  it("deletes a schedule", async () => {
    vi.mocked(listReports).mockResolvedValue([]);
    vi.mocked(listSchedules).mockResolvedValue([schedule()]);
    vi.mocked(deleteSchedule).mockResolvedValue(undefined);

    renderWithRepo(<ReportsPage />);
    const row = (await screen.findByText("Monthly revenue by region")).closest("li") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Delete" }));

    expect(deleteSchedule).toHaveBeenCalledWith("sch-1");
  });

  it("offers the design template control on a finished report only", async () => {
    vi.mocked(listReports).mockResolvedValue([
      report({ id: "done-1", status: "done", has_pdf: true, can_rerender: true }),
      report({ id: "busy-1", status: "processing", has_pdf: false }),
    ]);
    vi.mocked(listSchedules).mockResolvedValue([]);
    renderWithRepo(<ReportsPage />);
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /Design template/i })).toHaveLength(1),
    );
  });

  it("fetches the list of templates once for the whole page", async () => {
    // The templates are a property of the build, not of a report. A panel per
    // finished report, each loading its own copy, meant one identical
    // GET /report-templates per row — twenty rows, twenty requests.
    vi.mocked(listReports).mockResolvedValue([
      report({ id: "done-1", status: "done", has_pdf: true, can_rerender: true }),
      report({ id: "done-2", status: "done", has_pdf: true, can_rerender: true }),
      report({ id: "done-3", status: "done", has_pdf: true, can_rerender: true }),
    ]);
    renderWithRepo(<ReportsPage />);
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /Design template/i })).toHaveLength(3),
    );
    expect(listReportTemplates).toHaveBeenCalledTimes(1);
  });

  it("hands the fetched templates down to each design panel", async () => {
    vi.mocked(listReportTemplates).mockResolvedValue([
      { key: "kvarta", name: "Fixture One", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
    ]);
    vi.mocked(listReports).mockResolvedValue([
      report({ id: "done-1", status: "done", has_pdf: true, can_rerender: true, template: null }),
    ]);
    renderWithRepo(<ReportsPage />);
    // Only the page's fetch can supply this name, so the sentence proves the
    // panel is rendering the list it was given.
    expect(await screen.findByText(/No choice recorded/i)).toHaveTextContent("Fixture One");
  });

  it("states a failed template load once, not once per finished report", async () => {
    vi.mocked(listReportTemplates).mockRejectedValue(new Error("boom"));
    vi.mocked(listReports).mockResolvedValue([
      report({ id: "done-1", status: "done", has_pdf: true, can_rerender: true }),
      report({ id: "done-2", status: "done", has_pdf: true, can_rerender: true }),
    ]);
    renderWithRepo(<ReportsPage />);
    // `findByText` is itself the assertion: it throws on more than one match.
    expect(await screen.findByText(/Could not load the list of templates/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Could not load the list of templates/i)).toHaveLength(1);
  });

  it("says nothing about the templates when the load worked", async () => {
    vi.mocked(listReports).mockResolvedValue([
      report({ id: "done-1", status: "done", has_pdf: true, can_rerender: true }),
    ]);
    renderWithRepo(<ReportsPage />);
    await screen.findByRole("button", { name: /Design template/i });
    expect(screen.queryByText(/Could not load the list of templates/i)).not.toBeInTheDocument();
  });

  it("keeps quiet about the templates when no report could use one", async () => {
    // Nothing is finished, so there is no design control on the page and nothing
    // the missing list would have served.
    vi.mocked(listReportTemplates).mockRejectedValue(new Error("boom"));
    vi.mocked(listReports).mockResolvedValue([report({ status: "processing" })]);
    renderWithRepo(<ReportsPage />);
    await screen.findByText("Generating…");
    expect(screen.queryByText(/Could not load the list of templates/i)).not.toBeInTheDocument();
  });

  // The plan review loop drives the poll timer, and userEvent's `advanceTimers`
  // needs a mocked clock, so this whole group runs on fake timers.
  describe("plan review", () => {
    beforeEach(() => {
      vi.useFakeTimers();
      // Testing Library only advances a fake clock it recognises, and it looks for
      // a global `jest`. Without this shim every findBy*/waitFor sits on a frozen
      // clock: React's scheduler runs on setImmediate, which useFakeTimers() also
      // freezes, so no state update ever renders and the query times out.
      vi.stubGlobal("jest", { advanceTimersByTime: (ms: number) => vi.advanceTimersByTime(ms) });
    });

    afterEach(() => {
      vi.unstubAllGlobals();
      vi.useRealTimers();
    });

    const PLAN = {
      title: "Дохід Apple",
      language: "uk",
      sections: [
        {
          title: "Динаміка",
          source: "database" as const,
          databases: [DB],
          columns: [],
          sub_question: "дохід за FY25?",
        },
      ],
    };

    /** What the next round came back with: the section the reader added blank,
     *  filled in by the model. */
    const FILLED = {
      ...PLAN,
      sections: [
        PLAN.sections[0],
        {
          title: "Ризики",
          source: "database" as const,
          databases: [DB],
          columns: [],
          sub_question: "які ризики?",
        },
      ],
    };

    it("attributes a round to the model only once that round has come back", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      // /replan is asynchronous: the POST returns with the row already flipped to
      // `planning` and the *pre-replan* plan still attached. The re-planned one
      // only lands on a later poll, so the response body is no baseline at all.
      vi.mocked(replanReport).mockResolvedValue(
        report({ status: "planning", plan: PLAN, plan_rounds: 1 }),
      );
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await waitFor(() => expect(replanReport).toHaveBeenCalledTimes(1));
      // The model has not answered yet, so nothing may be attributed to it.
      expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).not.toBeInTheDocument();
      // …and the plan stays on screen for the length of the call.
      expect(screen.getByDisplayValue("Динаміка")).toBeInTheDocument();

      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: FILLED, plan_rounds: 2 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });

      // Exactly the two fields the reader left blank, and nothing else: the three
      // they wrote themselves stay attributed to them.
      expect(await screen.findAllByText(/дописано LLM|filled in by the LLM/)).toHaveLength(2);
      expect(screen.getAllByText(/закріплено вами|kept from your edit/)).toHaveLength(3);

      // A round that never happened attributes nothing: last round's badges would
      // otherwise stand against edits they were never computed from.
      vi.mocked(replanReport).mockRejectedValue(new Error("boom"));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await screen.findByText(/Не вдалося перепланувати|Could not re-plan/);
      expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).not.toBeInTheDocument();
    });

    it("attributes nothing when the backend rolls the round back", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(replanReport).mockResolvedValue(
        report({ status: "planning", plan: PLAN, plan_rounds: 1 }),
      );
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      // Blank a field the *previous* plan has filled in — the shape that makes a
      // diff against the wrong baseline visible.
      await user.clear(screen.getByLabelText(/Підпитання|Sub-question/));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await waitFor(() => expect(replanReport).toHaveBeenCalledTimes(1));

      // A round that failed inside the worker comes back as `awaiting_approval`
      // with the PREVIOUS plan and an error — the recovery path that keeps an
      // approvable plan alive through a provider hiccup. The model never ran, so
      // it filled nothing, and `plan_rounds` is no help: the backend skips the
      // increment for a draft with no blanks.
      vi.mocked(listReports).mockResolvedValue([
        report({
          status: "awaiting_approval",
          plan: PLAN,
          plan_rounds: 1,
          error: "ReportPlanError: provider timeout",
        }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });

      expect(await screen.findByText(/provider timeout/)).toBeInTheDocument();
      expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).not.toBeInTheDocument();
      // …and the reader can try again.
      expect(screen.getByRole("button", { name: /Перепланувати|Re-plan/ })).toBeEnabled();
    });

    it("keeps the editor on the report being reviewed when another plan lands", async () => {
      const other = report({
        id: "rep-2",
        status: "awaiting_approval",
        plan: { ...PLAN, sections: [{ ...PLAN.sections[0], title: "Конкуренти" }] },
        plan_rounds: 1,
      });
      const reviewed = report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 });
      // The second report is still being planned, which is also what keeps the poll
      // armed; it has no plan yet, so it cannot be the one under review.
      vi.mocked(listReports).mockResolvedValue([
        reviewed,
        report({ id: "rep-2", status: "planning", plan: null }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      // Newest first, so the second report finishing its plan arrives *above* the
      // one being reviewed. Re-deriving the editor's subject from the list would
      // swap it out mid-edit and PlanEditor would re-seed over the reader's edits.
      vi.mocked(listReports).mockResolvedValue([other, reviewed]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });
      await waitFor(() => expect(vi.mocked(listReports).mock.calls.length).toBeGreaterThan(1));
      expect(screen.getByDisplayValue("Динаміка")).toBeInTheDocument();
      expect(screen.queryByDisplayValue("Конкуренти")).not.toBeInTheDocument();
    });

    it("shows the plan editor when a report is awaiting approval", async () => {
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      expect(await screen.findByDisplayValue("Динаміка")).toBeInTheDocument();
    });

    it("keeps polling while planning and stops once it is awaiting approval", async () => {
      vi.mocked(listReports).mockResolvedValue([report({ status: "planning", plan: null })]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      await screen.findByText(/Планування|Planning/);
      const before = vi.mocked(listReports).mock.calls.length;
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });
      await waitFor(() => expect(vi.mocked(listReports).mock.calls.length).toBeGreaterThan(before));
      await screen.findByDisplayValue("Динаміка");
      const settled = vi.mocked(listReports).mock.calls.length;
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS * 3);
      });
      expect(vi.mocked(listReports).mock.calls.length).toBe(settled);
    });

    it("approving posts the on-screen plan and moves the report to pending", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(approveReport).mockResolvedValue(report({ status: "pending", plan: PLAN }));
      renderWithRepo(<ReportsPage />);
      const title = await screen.findByDisplayValue("Динаміка");
      await user.clear(title);
      await user.type(title, "Змінено");
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await user.click(await screen.findByRole("button", { name: /Згенерувати|Generate report/ }));
      await waitFor(() => expect(approveReport).toHaveBeenCalledTimes(1));
      expect(vi.mocked(approveReport).mock.calls[0][2].sections[0].title).toBe("Змінено");
    });

    it("advancing from the plan makes no network call", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      expect(approveReport).not.toHaveBeenCalled();
      expect(
        await screen.findByRole("button", { name: /Згенерувати|Generate report/ }),
      ).toBeInTheDocument();
    });

    it("Back returns to the plan with the edit still present", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      const title = await screen.findByDisplayValue("Динаміка");
      await user.clear(title);
      await user.type(title, "Змінено");
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await user.click(await screen.findByRole("button", { name: /Назад|Back/ }));
      expect(await screen.findByDisplayValue("Змінено")).toBeInTheDocument();
    });

    it("shows a landed re-plan after Next, Back, and Re-plan", async () => {
      // The only route back to Re-plan once a draft has been pinned by Next: edit,
      // advance, come back, ask the model to redo it. A draft that survives this
      // round would hide the model's rewrite behind stale text forever — step 1's
      // `plan` prop would never change again, so PlanEditor would never re-seed.
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(replanReport).mockResolvedValue(
        report({ status: "planning", plan: PLAN, plan_rounds: 1 }),
      );
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      // Edit the title before advancing. This is what tells the right fix apart
      // from the forbidden one: `replanReport`'s mocked response carries the
      // *pre-replan* plan (title "Дохід Apple", per the real endpoint's contract —
      // see the comment above `sent`), so clearing the draft as soon as that POST
      // resolves would snap the screen back to the server's stale title mid-flight,
      // discarding the edit while the round is still running. Only PLAN's own
      // title is edited here; no assertion in this test would move if the clear
      // never ran at all.
      const title = screen.getByLabelText(/Назва звіту|Report title/);
      await user.clear(title);
      await user.type(title, "Змінено");

      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await user.click(await screen.findByRole("button", { name: /Назад|Back/ }));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await waitFor(() => expect(replanReport).toHaveBeenCalledTimes(1));

      // Flush any pending passive effects — PlanEditor's own re-seed effect among
      // them — so the check below reflects settled state, not a mid-flush snapshot
      // that happens to still show the pre-click value regardless of where the
      // clear landed.
      await act(async () => {});
      // The round has not landed yet — only answered that it started — so the
      // edit must still be on screen. A clear placed in `onReplan`'s success
      // branch instead of the landing effect would have discarded it right here.
      expect(screen.getByDisplayValue("Змінено")).toBeInTheDocument();

      // The round lands with a genuinely different plan.
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: FILLED, plan_rounds: 2 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });

      // Only FILLED has this section — a draft still pinned to the pre-replan plan
      // would never show it.
      expect(await screen.findByDisplayValue("Ризики")).toBeInTheDocument();
    });

    it("drops provenance across a Next/Back trip so a reorder cannot re-attribute a badge", async () => {
      // `filled`'s `s{i}` keys index the plan they were computed against.
      // PlanEditor's own `restructured` flag already suppresses a stale badge once
      // the reader reorders — but that flag is component-local state, and step 2
      // unmounts the editor. A later Back trip remounts it fresh, `restructured`
      // reset to false, while `review.draft` faithfully carries the *reordered*
      // plan across the trip and `filled` does not know to follow: without a fix,
      // the badge reappears, now attached to whatever section the reorder put at
      // that index — attributing text to the model it never wrote.
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(replanReport).mockResolvedValue(
        report({ status: "planning", plan: PLAN, plan_rounds: 1 }),
      );
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      // Get a round back, so there is a real badge to lose track of.
      await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await waitFor(() => expect(replanReport).toHaveBeenCalledTimes(1));
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: FILLED, plan_rounds: 2 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });
      expect(await screen.findAllByText(/дописано LLM|filled in by the LLM/)).toHaveLength(2);

      // Reorder: the badges correctly disappear at once — this much already works.
      await user.click(screen.getAllByRole("button", { name: /Нижче|Move down/ })[0]);
      expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).not.toBeInTheDocument();

      // Advance and come back. The reorder survives the trip; the badges must not.
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await user.click(await screen.findByRole("button", { name: /Назад|Back/ }));

      expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).not.toBeInTheDocument();
    });

    it("keeps provenance across a Next/Back trip when nothing was reordered", async () => {
      // The overwhelmingly common trip: look at the design step, come straight
      // back, having touched nothing on the plan. Clearing provenance on every
      // advance — not only a restructuring one — would cost the reader the record
      // of what the model wrote versus what they kept on every ordinary round
      // trip, not just the rare one that actually needs protecting.
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(replanReport).mockResolvedValue(
        report({ status: "planning", plan: PLAN, plan_rounds: 1 }),
      );
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");

      // Get a round back, so there is a real badge that must survive.
      await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      await waitFor(() => expect(replanReport).toHaveBeenCalledTimes(1));
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: FILLED, plan_rounds: 2 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });
      expect(await screen.findAllByText(/дописано LLM|filled in by the LLM/)).toHaveLength(2);

      // No reorder this time — straight to the design step and back.
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await user.click(await screen.findByRole("button", { name: /Назад|Back/ }));

      // Both badges survive, still attached to the section the model actually
      // wrote — "Ризики" — and not to "Динаміка", which the reader wrote.
      const risky = screen.getByDisplayValue("Ризики").closest("li") as HTMLElement;
      expect(within(risky).getAllByText(/дописано LLM|filled in by the LLM/)).toHaveLength(2);
      const dynamics = screen.getByDisplayValue("Динаміка").closest("li") as HTMLElement;
      expect(
        within(dynamics).queryByText(/дописано LLM|filled in by the LLM/),
      ).not.toBeInTheDocument();
    });

    it("drops a stale step and draft when the pinned report changes under it", async () => {
      // Reader is mid-review of A, on step 2, with an edit pinned by Next. A poll
      // then retires A (it leaves the review loop) and frees the pin to B. B must
      // come up on step 1 with its own plan — never A's leftover step or draft,
      // which would otherwise let a stray Generate post A's plan onto B, or fire a
      // design fetch for a report the reader never opened.
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const planA = PLAN;
      const planB = {
        title: "Дохід Google",
        language: "uk",
        sections: [
          {
            title: "Маржа",
            source: "database" as const,
            databases: [DB],
            columns: [],
            sub_question: "яка маржа?",
          },
        ],
      };
      // B starts out `planning` — not yet reviewable, so it cannot steal the pin —
      // which is also what keeps the poll armed: `awaiting_approval` alone is
      // excluded from polling on purpose (nothing running would change it).
      vi.mocked(listReports).mockResolvedValue([
        report({ id: "rep-a", status: "awaiting_approval", plan: planA, plan_rounds: 1 }),
        report({ id: "rep-b", status: "planning", plan: null }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");
      await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
      await screen.findByRole("button", { name: /Назад|Back/ }); // on step 2

      // A leaves the review loop (e.g. approved out from under the poll); B is now
      // the only reviewable report.
      vi.mocked(listReports).mockResolvedValue([
        report({ id: "rep-a", status: "pending", plan: planA, plan_rounds: 1 }),
        report({ id: "rep-b", status: "awaiting_approval", plan: planB, plan_rounds: 1 }),
      ]);
      await act(async () => {
        vi.advanceTimersByTime(REPORT_POLL_MS + 10);
      });

      // B's plan, on step 1 — not a stray step-2 card left over from A.
      expect(await screen.findByDisplayValue("Маржа")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Назад|Back/ })).not.toBeInTheDocument();
      // The harm a transient stale render actually leaves behind: a real GET for a
      // report the reader never opened. The DOM above is the same whether or not
      // that fetch fired — it is flushed away by the time these assertions run —
      // so only the call log can catch it.
      expect(getReportDesign).not.toHaveBeenCalledWith(expect.anything(), "rep-b");
    });

    it("a finished report's row is untouched by the step flow", async () => {
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "done", has_pdf: true, can_rerender: true }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(listReportTemplates).mockResolvedValue([
        { key: "kvarta", name: "Кварта" },
      ] as never);
      renderWithRepo(<ReportsPage />);
      expect(await screen.findByRole("button", { name: "Download PDF" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Далі|Next/ })).not.toBeInTheDocument();
    });

    it("surfaces a replan failure without losing the plan", async () => {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      vi.mocked(listReports).mockResolvedValue([
        report({ status: "awaiting_approval", plan: PLAN, plan_rounds: 1 }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      vi.mocked(replanReport).mockRejectedValue(new Error("boom"));
      renderWithRepo(<ReportsPage />);
      await screen.findByDisplayValue("Динаміка");
      await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
      expect(
        await screen.findByText(/Не вдалося перепланувати|Could not re-plan/),
      ).toBeInTheDocument();
      expect(screen.getByDisplayValue("Динаміка")).toBeInTheDocument();
    });

    it("shows a failed report's error even when a plan survives", async () => {
      vi.mocked(listReports).mockResolvedValue([
        report({
          status: "awaiting_approval",
          plan: PLAN,
          plan_rounds: 2,
          error: "ReportGenerationError: boom",
        }),
      ]);
      vi.mocked(listSchedules).mockResolvedValue([]);
      renderWithRepo(<ReportsPage />);
      expect(await screen.findByText(/boom/)).toBeInTheDocument();
    });
  });
});
