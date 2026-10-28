import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RepositoryTemplateField } from "./RepositoryTemplateField";

vi.mock("../../api/reports", async () => {
  const actual = await vi.importActual<typeof import("../../api/reports")>("../../api/reports");
  return { ...actual, listReportTemplates: vi.fn(), rerenderReport: vi.fn() };
});
vi.mock("../../api/repositories", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/repositories")>("../../api/repositories");
  return { ...actual, getReportTemplate: vi.fn(), setReportTemplate: vi.fn() };
});

import { listReportTemplates, rerenderReport } from "../../api/reports";
import { getReportTemplate, setReportTemplate } from "../../api/repositories";

const TEMPLATES = [
  { key: "kvarta", name: "Fixture One", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
  { key: "chasopys", name: "Часопис", paper: "#f7f7f5", ink: "#16181a", accent: "#0a5f56" },
];

describe("RepositoryTemplateField", () => {
  beforeEach(() => {
    vi.mocked(listReportTemplates).mockResolvedValue(TEMPLATES);
    vi.mocked(getReportTemplate).mockResolvedValue({ template: null });
    vi.mocked(setReportTemplate).mockResolvedValue({ template: "chasopys" });
  });
  afterEach(() => vi.clearAllMocks());

  it("never tells the admin that choosing a default rebuilds a report", async () => {
    // The counterpart to "never rebuilds anything" above: that one proves the
    // field does not rebuild, this one proves it does not *claim* to. The
    // gallery is shared with the per-report panel, whose lede promises the
    // choice "is applied to this report and rebuilt from stored data" — false
    // here in both halves, and it is the only sentence the admin is given.
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    const gallery = await screen.findByRole("dialog");
    expect(within(gallery).queryByText(/applied to this report/i)).toBeNull();
    expect(within(gallery).getByText(/new reports/i)).toBeInTheDocument();
  });

  it("saves the chosen default", async () => {
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() => expect(setReportTemplate).toHaveBeenCalledWith("r-1", "chasopys"));
  });

  it("never rebuilds anything", async () => {
    // A default is not a report. Existing reports each snapshotted their own
    // template at generation, and changing the default must not reach back and
    // restyle them — that is the whole binding rule from step 3b.
    //
    // `rerenderReport` is spied through the SAME module mock the component
    // imports. Spying on a fresh object literal that merely holds the function
    // would assert nothing at all: the component would call the real export
    // while the spy sat untouched, and the test would pass no matter what the
    // component did.
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() => expect(setReportTemplate).toHaveBeenCalled());
    expect(rerenderReport).not.toHaveBeenCalled();
  });

  it("shows no preview, because a default has no report behind it", async () => {
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    await waitFor(() => expect(setReportTemplate).toHaveBeenCalled());
    expect(screen.queryByTitle(/report preview/i)).not.toBeInTheDocument();
  });

  it("names the configured default without making the admin open the gallery", async () => {
    vi.mocked(getReportTemplate).mockResolvedValue({ template: "chasopys" });
    render(<RepositoryTemplateField repositoryId="r-1" />);
    expect(await screen.findByText(/Current template/i)).toHaveTextContent("Часопис");
    // …and once a default is set, the "no default chosen" sentence is gone.
    expect(screen.queryByText(/No default chosen/i)).not.toBeInTheDocument();
  });

  it("names nothing when the stored default is a slug this build no longer ships", async () => {
    // Step 3b rejects unknown templates on write and tolerates them on read, so
    // that removing a template can never break an existing repository. Inventing
    // a name here, or crashing, would both break that tolerance.
    vi.mocked(getReportTemplate).mockResolvedValue({ template: "retired-template" });
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await screen.findByRole("button", { name: /Design template/i });
    await waitFor(() => expect(getReportTemplate).toHaveBeenCalled());
    expect(screen.queryByText(/Current template/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/No default chosen/i)).not.toBeInTheDocument();
  });

  it("names the new default straight after saving it", async () => {
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    expect(await screen.findByText(/Current template/i)).toHaveTextContent("Часопис");
  });

  it("says an apply is in flight", async () => {
    // The sibling per-report panel has always said so; without it the admin
    // clicks a card and the gallery simply sits there.
    vi.mocked(setReportTemplate).mockReturnValue(new Promise(() => undefined));
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    expect(await screen.findByRole("status")).toHaveTextContent(/Applying/i);
  });

  it("puts a failed apply inside the dialog it left open", async () => {
    // The gallery is a fixed, full-viewport, opaque overlay: a message left in
    // the field's own page flow while it is up is painted over.
    vi.mocked(setReportTemplate).mockRejectedValue(new Error("boom"));
    render(<RepositoryTemplateField repositoryId="r-1" />);
    await userEvent.click(await screen.findByRole("button", { name: /Design template/i }));
    await userEvent.click(await screen.findByRole("button", { name: /Часопис/ }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/Could not apply the template/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Could not apply the template/i)).toHaveLength(1);
  });

  it("surfaces a failed template load instead of showing an empty gallery", async () => {
    // Swallowed, this failure is indistinguishable from a build that ships no
    // templates at all — nothing tells the user anything went wrong.
    vi.mocked(listReportTemplates).mockRejectedValue(new Error("boom"));
    render(<RepositoryTemplateField repositoryId="r-1" />);
    expect(await screen.findByText(/Could not load the list of templates/i)).toBeInTheDocument();
  });

  it("says nothing about a load that worked", async () => {
    render(<RepositoryTemplateField repositoryId="r-1" />);
    expect(await screen.findByText(/No default chosen/i)).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/i)).not.toBeInTheDocument();
  });

  it("drops the load error once a later load succeeds", async () => {
    // The effect re-runs on a repository switch and on a locale change, so a
    // transient failure would otherwise leave a populated gallery sitting under
    // a stale error for the rest of the session.
    vi.mocked(listReportTemplates).mockRejectedValueOnce(new Error("boom"));
    const { rerender } = render(<RepositoryTemplateField repositoryId="r-1" />);
    expect(await screen.findByText(/Could not load the list of templates/i)).toBeInTheDocument();

    rerender(<RepositoryTemplateField repositoryId="r-2" />);
    await waitFor(() =>
      expect(screen.queryByText(/Could not load the list of templates/i)).not.toBeInTheDocument(),
    );
  });

  it("blames the request that actually failed", async () => {
    // Joined under one `Promise.all`, a failing read of the current default
    // reported "could not load the list of templates" — about a list that had
    // arrived intact, and which the gallery can still show.
    vi.mocked(getReportTemplate).mockRejectedValue(new Error("boom"));
    render(<RepositoryTemplateField repositoryId="r-1" />);
    expect(
      await screen.findByText(/Could not load the current default template/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Could not load the list of templates/i)).not.toBeInTheDocument();
    // …and, not knowing the default, the field must not claim there is none.
    expect(screen.queryByText(/No default chosen/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Design template/i }));
    expect(await screen.findByRole("button", { name: /Часопис/ })).toBeInTheDocument();
  });
});
