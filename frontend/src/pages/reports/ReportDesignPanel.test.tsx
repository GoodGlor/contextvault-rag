import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ReportDesignPanel } from "./ReportDesignPanel";
import { ApiError } from "../../api/client";
import type { Report } from "../../api/reports";

vi.mock("../../api/reports", async () => {
  const actual = await vi.importActual<typeof import("../../api/reports")>("../../api/reports");
  return {
    ...actual,
    listReportTemplates: vi.fn(),
    getReportDesign: vi.fn(),
    setReportDesign: vi.fn(),
    rerenderReport: vi.fn(),
    downloadReport: vi.fn(),
  };
});

import {
  downloadReport,
  getReportDesign,
  listReportTemplates,
  rerenderReport,
  setReportDesign,
} from "../../api/reports";

function report(over: Partial<Report> = {}): Report {
  return {
    id: "rep-1",
    repository_id: "r-1",
    prompt: "Monthly revenue",
    status: "done",
    error: null,
    created_at: "2026-08-04T10:00:00Z",
    has_pdf: true,
    has_pptx: false,
    schedule_id: null,
    plan: null,
    plan_rounds: 1,
    can_rerender: true,
    template: "kvarta",
    ...over,
  };
}

// The first entry's name is deliberately nothing a production string could be:
// the "no choice recorded" sentence is supposed to prove it read `templates[0]`,
// and against a fixture named "Кварта" it would pass just as well if the panel
// had the default's name hardcoded.
const TEMPLATES = [
  { key: "kvarta", name: "Fixture One", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
  { key: "chasopys", name: "Часопис", paper: "#f7f7f5", ink: "#16181a", accent: "#0a5f56" },
];

describe("ReportDesignPanel", () => {
  const createObjectURL = vi.fn(() => "blob:preview-1");
  const revokeObjectURL = vi.fn();

  beforeEach(() => {
    vi.mocked(listReportTemplates).mockResolvedValue(TEMPLATES);
    vi.mocked(setReportDesign).mockResolvedValue(report({ template: "chasopys" }));
    vi.mocked(rerenderReport).mockResolvedValue(report({ template: "chasopys" }));
    vi.mocked(downloadReport).mockResolvedValue(new Blob(["%PDF-1.7"]));
    vi.mocked(getReportDesign).mockResolvedValue({
      template: "kvarta",
      blocks_version: 1,
      blocks: [{ kind: "cover" }, { kind: "heading", section: 0 }],
      stored: false,
      typography: {},
      page: {},
      brand: {},
      sections: [
        {
          index: 0,
          title: "Виторг",
          kind: "data",
          allowed_chart_types: ["bar", "line"],
          draws_chart: true,
          prose: null,
          sources: null,
        },
      ],
      title: "Річний звіт",
    });
    // Spy on the two statics rather than replacing the global `URL`. A spread of
    // `URL` copies none of its static methods (they are non-enumerable) and
    // throws away the constructor, so any code path that builds a `new URL(...)`
    // — including some the router uses — would break for reasons that have
    // nothing to do with this test.
    vi.spyOn(URL, "createObjectURL").mockImplementation(createObjectURL);
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(revokeObjectURL);
  });
  afterEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
  });

  function renderPanel(
    over: Partial<Report> = {},
    props: { templatesError?: string | null; onApplied?: (r: Report) => void } = {},
  ) {
    const onApplied = props.onApplied ?? vi.fn();
    const view = render(
      <MemoryRouter>
        <ReportDesignPanel
          report={report(over)}
          templates={TEMPLATES}
          templatesError={props.templatesError ?? null}
          onApplied={onApplied}
        />
      </MemoryRouter>,
    );
    return { ...view, onApplied };
  }

  async function openAndChoose(over: Partial<Report> = {}): Promise<void> {
    renderPanel(over);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
  }

  // --- the workbench link (step 6a) -------------------------------------------

  it("links to the workbench for a report that can be rebuilt", () => {
    renderPanel({ can_rerender: true });

    expect(screen.getByRole("link", { name: /Edit design/i })).toHaveAttribute(
      "href",
      "/reports/rep-1/design",
    );
  });

  it("offers no workbench link for a report that cannot be rebuilt", () => {
    renderPanel({ can_rerender: false });

    expect(screen.queryByRole("link", { name: /Edit design/i })).not.toBeInTheDocument();
  });

  it("no longer mounts the block editor — the row is for choosing a template", () => {
    renderPanel({ can_rerender: true });

    expect(screen.queryByRole("list", { name: "Report blocks" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Edit blocks|Close editor/i }),
    ).not.toBeInTheDocument();
  });

  it("stores the choice before it rebuilds, and fetches the artifact after", async () => {
    // The order carries the guarantee. If the rebuild fails, the choice is still
    // on the row and the next build wears it. Reversed, a failed store would
    // leave the row and the stored artifacts disagreeing — the report would look
    // like a template it does not claim on paper.
    await openAndChoose();
    await waitFor(() => expect(downloadReport).toHaveBeenCalled());
    const storedAt = vi.mocked(setReportDesign).mock.invocationCallOrder[0];
    const rebuiltAt = vi.mocked(rerenderReport).mock.invocationCallOrder[0];
    const fetchedAt = vi.mocked(downloadReport).mock.invocationCallOrder[0];
    expect(storedAt).toBeLessThan(rebuiltAt);
    expect(rebuiltAt).toBeLessThan(fetchedAt);
    // Only the template: the endpoint is a partial update, so a block list the
    // reader has stored must survive a template click untouched.
    expect(setReportDesign).toHaveBeenCalledWith("r-1", "rep-1", { template: "chasopys" });
    expect(downloadReport).toHaveBeenCalledWith("r-1", "rep-1", "pdf");
  });

  it("asks for the template list no more than the page already has", async () => {
    // One panel per finished report, each with its own load, meant one identical
    // GET /report-templates per row. The list is a property of the build; the
    // page fetches it once and hands it down.
    renderPanel();
    await screen.findByRole("button", { name: /Design template/i });
    expect(listReportTemplates).not.toHaveBeenCalled();
  });

  it("shows the fetched artifact, not a rendering of its own", async () => {
    await openAndChoose();
    await waitFor(() =>
      expect(screen.getByTitle(/report preview/i)).toHaveAttribute("src", "blob:preview-1"),
    );
  });

  it("releases the previous preview when a new one replaces it", async () => {
    // Unrevoked, every template switch leaks a whole PDF.
    await openAndChoose();
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Fixture One/ }));
    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1"));
  });

  it("releases the preview on unmount", async () => {
    const { unmount } = renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1");
  });

  it("releases a preview that arrives after the panel is gone", async () => {
    // The apply outlives its panel: `show()` would park the URL in a ref whose
    // cleanup has already run, and the PDF would be held for the life of the
    // document with nothing left able to revoke it.
    let deliver!: (blob: Blob) => void;
    vi.mocked(downloadReport).mockReturnValue(
      new Promise<Blob>((resolve) => {
        deliver = resolve;
      }),
    );
    const { unmount } = renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() => expect(downloadReport).toHaveBeenCalled());
    unmount();
    expect(createObjectURL).not.toHaveBeenCalled();

    await act(async () => {
      deliver(new Blob(["%PDF-1.7"]));
      await Promise.resolve();
    });
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1");
  });

  it("still shows the preview after React has re-run its effects", async () => {
    // StrictMode mounts, unmounts and remounts every component in development.
    // A `live` flag that is only ever cleared stays false from that first
    // simulated unmount onward, and the panel then discards the artifact of
    // every apply it makes — the gallery never closes and no preview appears.
    // The e2e caught this; a plain `render()` cannot, because it mounts once.
    render(
      <StrictMode>
        <MemoryRouter>
          <ReportDesignPanel
            report={report()}
            templates={TEMPLATES}
            templatesError={null}
            onApplied={vi.fn()}
          />
        </MemoryRouter>
      </StrictMode>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() =>
      expect(screen.getByTitle(/report preview/i)).toHaveAttribute("src", "blob:preview-1"),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("hands the updated report back to its owner", async () => {
    const { onApplied } = renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() =>
      expect(onApplied).toHaveBeenCalledWith(expect.objectContaining({ template: "chasopys" })),
    );
  });

  it("stores the choice but does not rebuild a report that has no stored data", async () => {
    // The design is an intent and is accepted in any status; only the rebuild is
    // impossible. Refusing the whole interaction would lose a choice the next
    // build could have honoured.
    await openAndChoose({ can_rerender: false });
    await waitFor(() => expect(setReportDesign).toHaveBeenCalled());
    expect(rerenderReport).not.toHaveBeenCalled();
    expect(downloadReport).not.toHaveBeenCalled();
    expect(screen.getByText(/cannot be rebuilt from storage/i)).toBeInTheDocument();
  });

  it("claims a choice is saved only once one has been made", async () => {
    // "Your choice is saved" is a confirmation, not a label. Rendered
    // unconditionally on a `can_rerender: false` row it told every legacy report
    // that a choice nobody had made was already waiting for the next build.
    renderPanel({ can_rerender: false });
    expect(await screen.findByText(/cannot be rebuilt from storage/i)).toBeInTheDocument();
    expect(screen.queryByText(/Your choice is saved/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    expect(await screen.findByText(/Your choice is saved/i)).toBeInTheDocument();
    // …and the row's own limitation is still stated, separately.
    expect(screen.getByText(/cannot be rebuilt from storage/i)).toBeInTheDocument();
  });

  it("never says why a report cannot be rebuilt", async () => {
    // Two causes — generated before `sections_data` existed, or a payload over
    // the 4 MiB budget — are indistinguishable on the wire. Naming either would
    // be a guess presented as a fact.
    await openAndChoose({ can_rerender: false });
    const message = await screen.findByText(/cannot be rebuilt from storage/i);
    expect(message.textContent).not.toMatch(/too large|budget|older|before/i);
  });

  it("survives a 409 from a rebuild it believed was possible", async () => {
    // The race: stored data disappears between the list arriving and the click.
    // By then the PUT has already landed, so the template *was* applied — saying
    // it could not be is false, and dropping the stored row on the floor leaves
    // the page showing the old template until the next poll.
    vi.mocked(rerenderReport).mockRejectedValue(
      new ApiError(409, "This report cannot be rebuilt: no stored section data"),
    );
    const onApplied = vi.fn();
    renderPanel({}, { onApplied });
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));

    expect(await screen.findByText(/Your choice is saved/i)).toBeInTheDocument();
    expect(screen.queryByText(/Could not apply the template/i)).not.toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledWith(expect.objectContaining({ template: "chasopys" }));
    expect(downloadReport).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("still calls a failure a failure when the status is not 409", async () => {
    // Only the 409 means "stored, not rebuilt". A 500 means nothing of the sort.
    vi.mocked(rerenderReport).mockRejectedValue(new ApiError(500, "boom"));
    const onApplied = vi.fn();
    renderPanel({}, { onApplied });
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));

    expect(await screen.findByText(/Could not apply the template/i)).toBeInTheDocument();
    expect(screen.queryByText(/Your choice is saved/i)).not.toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("puts a failed apply inside the dialog it left open", async () => {
    // The gallery is a fixed, full-viewport, opaque overlay. A message rendered
    // in the panel's own page flow while it is up is painted over — the reader
    // clicks a card, the grid goes on sitting there, and nothing explains why.
    vi.mocked(setReportDesign).mockRejectedValue(new Error("boom"));
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/Could not apply the template/i)).toBeInTheDocument();
    // Exactly one copy: the panel yields the message while the gallery holds it.
    expect(screen.getAllByText(/Could not apply the template/i)).toHaveLength(1);
  });

  it("explains an empty grid inside the dialog", async () => {
    // The button opens the gallery whether or not the list arrived, so a failed
    // load otherwise shows an empty grid and nothing else.
    render(
      <MemoryRouter>
        <ReportDesignPanel
          report={report()}
          templates={[]}
          templatesError="Could not load the list of templates."
          onApplied={vi.fn()}
        />
      </MemoryRouter>,
    );
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Could not load the list of templates/i)).toBeInTheDocument();
  });

  it("says a report predating the choice records none, and names what it renders as", async () => {
    renderPanel({ template: null });
    // `templates[0]` is the default by the endpoint's documented ordering, so the
    // sentence names it without the frontend owning any slug.
    expect(await screen.findByText(/No choice recorded/i)).toHaveTextContent("Fixture One");
  });

  it("offers no preview pane for a finished report with no PDF", async () => {
    renderPanel({ has_pdf: false });
    expect(await screen.findByText(/There is no PDF for this report/i)).toBeInTheDocument();
    expect(screen.queryByTitle(/report preview/i)).not.toBeInTheDocument();
  });
});
