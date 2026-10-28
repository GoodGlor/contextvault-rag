import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { TemplateGallery } from "./TemplateGallery";
import type { TemplateChoice } from "../../api/reports";

const TEMPLATES: TemplateChoice[] = [
  { key: "kvarta", name: "Кварта", paper: "#f7f8fa", ink: "#10151e", accent: "#1a4a75" },
  { key: "prezydia", name: "Президія", paper: "#14161d", ink: "#f2f5fa", accent: "#4f97db" },
  { key: "chasopys", name: "Часопис", paper: "#f7f7f5", ink: "#16181a", accent: "#0a5f56" },
];

function renderGallery(over: Partial<ComponentProps<typeof TemplateGallery>> = {}) {
  const onChoose = vi.fn();
  const onClose = vi.fn();
  render(
    <TemplateGallery
      templates={TEMPLATES}
      selected="kvarta"
      lede="Fixture lede"
      busy={false}
      onChoose={onChoose}
      onClose={onClose}
      {...over}
    />,
  );
  return { onChoose, onClose };
}

describe("TemplateGallery", () => {
  it("shows the lede its host supplies rather than one of its own", () => {
    // The gallery is mounted by two surfaces whose choices mean different
    // things: applying a template to one finished report, and setting the
    // default for reports not yet generated. Owning the sentence itself meant
    // the repository field told admins their choice would be "applied to this
    // report and rebuilt from stored data" — describing a rebuild that the
    // field, by design, never performs.
    renderGallery({ lede: "Whatever the host wants said" });
    expect(screen.getByText("Whatever the host wants said")).toBeInTheDocument();
  });

  it("offers one card per shipped template", () => {
    renderGallery();
    expect(screen.getAllByRole("button", { name: /Кварта|Президія|Часопис/ })).toHaveLength(3);
  });

  it("marks the current template and only that one", () => {
    renderGallery();
    expect(screen.getByRole("button", { name: /Кварта/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Президія/ })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("marks nothing when the row records no choice", () => {
    // Null is not "Кварта was chosen" — it is a report generated before the
    // choice existed. Marking the default here would tell the reader a decision
    // was made that never was.
    renderGallery({ selected: null });
    screen
      .getAllByRole("button", { name: /Кварта|Президія|Часопис/ })
      .forEach((card) => expect(card).toHaveAttribute("aria-pressed", "false"));
  });

  it("marks nothing when the row names a template this build no longer ships", () => {
    // The backend rejects unknown slugs on write and tolerates them on read, so
    // that deleting a template can never make an old report unrenderable. The
    // gallery inherits that tolerance instead of crashing or inventing one.
    renderGallery({ selected: "retired-template" });
    screen
      .getAllByRole("button", { name: /Кварта|Президія|Часопис/ })
      .forEach((card) => expect(card).toHaveAttribute("aria-pressed", "false"));
  });

  it("reports the chosen key", async () => {
    const { onChoose } = renderGallery();
    await userEvent.click(screen.getByRole("button", { name: /Часопис/ }));
    expect(onChoose).toHaveBeenCalledWith("chasopys");
  });

  it("accepts no choice while one is being applied", async () => {
    // Two overlapping rebuilds race, and the artifact that lands last need not
    // be the template clicked last.
    const { onChoose } = renderGallery({ busy: true });
    await userEvent.click(screen.getByRole("button", { name: /Часопис/ }));
    expect(onChoose).not.toHaveBeenCalled();
  });

  it("says an apply is in flight, inside the dialog", () => {
    // The backdrop is fixed, full-viewport and opaque with nothing above it, so
    // anything the host renders in page flow while the gallery is up is painted
    // over. The gallery is the only place the reader can still be told.
    renderGallery({ busy: true });
    expect(within(screen.getByRole("dialog")).getByRole("status")).toHaveTextContent(/Applying/i);
  });

  it("carries the host's message inside the dialog", () => {
    // Worst case for the alternative: the list failed to load, the button opens
    // the gallery anyway, and the reader gets an empty grid with the explanation
    // hidden behind it.
    renderGallery({ templates: [], error: "Could not load the list of templates." });
    expect(
      within(screen.getByRole("dialog")).getByText("Could not load the list of templates."),
    ).toBeInTheDocument();
  });

  it("shows neither when there is nothing to say", () => {
    renderGallery();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog").querySelector(".error")).toBeNull();
  });
});
