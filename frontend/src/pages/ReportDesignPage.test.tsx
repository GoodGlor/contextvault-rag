import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReportDesignPage } from "./ReportDesignPage";
import * as reportsApi from "../api/reports";
import * as assetsApi from "../api/reportAssets";
import { ACCENTS } from "./reports/StyleInspector";
import { ApiError } from "../api/client";
import { RepositoryContext } from "../repository/RepositoryContext";
import type { Repository } from "../api/repositories";
import type { Report, ReportDesign } from "../api/reports";

const REPORT: Report = {
  id: "r1",
  repository_id: "repo1",
  prompt: "Виторг за 5 років",
  status: "done",
  error: null,
  created_at: "2026-08-02T10:00:00Z",
  has_pdf: true,
  has_pptx: true,
  schedule_id: null,
  plan: { title: "Звіт про виторг", language: "uk", sections: [] },
  plan_rounds: 1,
  can_rerender: true,
  template: "kvarta",
};

const DESIGN: ReportDesign = {
  template: "kvarta",
  blocks_version: 1,
  blocks: [{ kind: "cover" }, { kind: "heading", section: 0 }],
  stored: true,
  sections: [
    {
      index: 0,
      title: "Виторг",
      kind: "data",
      allowed_chart_types: ["bar"],
      draws_chart: true,
      prose: null,
      sources: null,
    },
  ],
  title: "Звіт про виторг",
  typography: {},
  page: {},
  brand: {},
};

function renderPage(currentRepoId = "repo1"): void {
  render(
    <RepositoryContext.Provider
      value={{
        repos: [{ id: "repo1", name: "Бухгалтерія" } as Repository],
        currentRepoId,
        setCurrentRepoId: vi.fn(),
        loading: false,
        error: null,
      }}
    >
      <MemoryRouter initialEntries={["/reports/r1/design"]}>
        <Routes>
          <Route path="/reports/:reportId/design" element={<ReportDesignPage />} />
        </Routes>
      </MemoryRouter>
    </RepositoryContext.Provider>,
  );
}

describe("ReportDesignPage", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(reportsApi, "listReportTemplates").mockResolvedValue([]);
  });

  it("renders the three columns once the report and design arrive", async () => {
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);

    renderPage();

    expect(await screen.findByRole("list", { name: "Report blocks" })).toBeInTheDocument();
    expect(screen.getByTestId("group-typo")).toBeInTheDocument();
    expect(screen.getByTestId("meta-blocks")).toHaveTextContent("2");
  });

  it("keeps an accent chosen while the logo upload is still in flight", async () => {
    // The workbench's own copy of the defect (#188). `onLogoFile` read
    // `style.brand` when the upload started and wrote it back when it landed, so
    // an accent picked in between was replaced by the stale snapshot. This path
    // is *not* the step's: it writes through `useReportPreview.request`, whose
    // waiting patch is replaced wholesale rather than merged — so the accent's
    // own patch is dropped too, and the logo's must carry it.
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);
    vi.spyOn(reportsApi, "getReportPreviewHtml").mockResolvedValue("<html></html>");
    const wrote = vi.spyOn(reportsApi, "setReportDesign").mockResolvedValue(REPORT);
    let land: (asset: unknown) => void = () => {};
    vi.spyOn(assetsApi, "uploadReportAsset").mockReturnValue(
      new Promise((resolve) => {
        land = resolve;
      }) as never,
    );

    renderPage();

    const input = await screen.findByTestId("logo-file");
    await userEvent.upload(input, new File(["x"], "logo.png", { type: "image/png" }));

    const swatch = screen
      .getAllByTestId("accent-swatch")
      .find((el) => el.getAttribute("data-accent") === ACCENTS[1]);
    await userEvent.click(swatch!);

    land({
      id: "asset1",
      filename: "logo.png",
      mime: "image/png",
      width: 10,
      height: 10,
      byte_size: 10,
    });

    await waitFor(() => expect(wrote).toHaveBeenCalled());
    await waitFor(() => {
      const brands = wrote.mock.calls
        .map(([, , patch]) => (patch as { brand?: Record<string, unknown> }).brand)
        .filter((brand): brand is Record<string, unknown> => brand !== undefined);
      expect(brands[brands.length - 1]).toMatchObject({
        accent: ACCENTS[1],
        logo_asset_id: "asset1",
      });
    });
  });

  it("names the repository and the document in the topbar", async () => {
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);

    renderPage();

    expect(await screen.findByText(/Бухгалтерія/)).toBeInTheDocument();
    expect(screen.getByText("Звіт про виторг")).toBeInTheDocument();
  });

  it("the tree and the inspector agree about the selection", async () => {
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);

    renderPage();
    await screen.findByTestId("block-node-1");
    await userEvent.click(screen.getByTestId("block-node-0"));
    // "Move up" is disabled at the top of the list and nowhere else, which makes
    // it the inspector's one index-*sensitive* control. Before 6c this assertion
    // read the textarea's value; that box is gone, and a witness that merely
    // showed "some block is selected" would be a weaker test wearing the same
    // name.
    expect(screen.getByTestId("block-up")).toBeDisabled();

    await userEvent.click(screen.getByTestId("block-node-1"));

    expect(screen.getByTestId("block-node-1")).toHaveAttribute("aria-current", "true");
    // The inspector is the other column reading the same state — this assertion
    // is the whole reason the block list lives in a hook.
    expect(screen.getByTestId("block-up")).toBeEnabled();
  });

  it("says the report is not in this repository rather than showing an empty editor", async () => {
    vi.spyOn(reportsApi, "getReport").mockRejectedValue(new ApiError(404, "not found"));

    renderPage("other-repo");

    expect(await screen.findByText(/not in the selected repository/i)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Report blocks" })).not.toBeInTheDocument();
  });

  it("does not ask for the design until the report has arrived", async () => {
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    const design = vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);

    renderPage();

    await waitFor(() => expect(design).toHaveBeenCalledWith("repo1", "r1"));
  });

  it("keeps the sheet's scale across a trip through the PDF view", async () => {
    // #172. The scale lived in `ReportCanvas`, which the centre column renders
    // behind a ternary on the view — so switching to `pdf` unmounted it and the
    // state was discarded. Coming back re-initialised at 100 %, silently
    // undoing whatever the reader had set.
    //
    // jsdom performs no layout, so *fit-to-width* cannot be tested here — but
    // the reset has nothing to do with measurement, and this reproduces it
    // without a browser.
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(DESIGN);
    vi.spyOn(reportsApi, "getReportPreviewHtml").mockResolvedValue("<main></main>");

    renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Zoom in" }));
    expect(screen.getByTestId("canvas-zoom")).toHaveTextContent("125%");

    await userEvent.click(screen.getByRole("button", { name: "PDF" }));
    await userEvent.click(screen.getByRole("button", { name: "Sheet" }));

    expect(screen.getByTestId("canvas-zoom")).toHaveTextContent("125%");
  });

  it("survives a design response carrying no style groups", async () => {
    // The exact 5f defect: a cached SPA meeting an older response threw inside
    // the inspector and React unmounted the whole editor, block tree included.
    vi.spyOn(reportsApi, "getReport").mockResolvedValue(REPORT);
    const withoutGroups = { ...DESIGN } as Partial<ReportDesign>;
    delete withoutGroups.typography;
    delete withoutGroups.page;
    delete withoutGroups.brand;
    vi.spyOn(reportsApi, "getReportDesign").mockResolvedValue(withoutGroups as ReportDesign);

    renderPage();

    expect(await screen.findByRole("list", { name: "Report blocks" })).toBeInTheDocument();
  });
});
