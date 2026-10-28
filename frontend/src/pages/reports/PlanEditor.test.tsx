import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlanEditor, diffFilled } from "./PlanEditor";
import type { ReportPlan } from "../../api/reports";
import i18n from "../../i18n";

/** The repository's one database, as the plan names it and the editor prints it. */
const DB = "11111111-1111-1111-1111-111111111111";
const DATABASES = [{ id: DB, name: "Apple", description: "Apple filings" }];

function plan(overrides: Partial<ReportPlan> = {}): ReportPlan {
  return {
    title: "Дохід Apple",
    language: "uk",
    sections: [
      {
        title: "Динаміка",
        source: "database",
        databases: [DB],
        columns: [],
        sub_question: "дохід за FY25?",
      },
      {
        title: "Методологія",
        source: "documents",
        databases: [],
        columns: [],
        sub_question: "як рахується рік?",
      },
    ],
    ...overrides,
  };
}

function renderEditor(over: Partial<React.ComponentProps<typeof PlanEditor>> = {}) {
  const onReplan = vi.fn();
  const onApprove = vi.fn();
  const { rerender } = render(
    <PlanEditor
      plan={plan()}
      rounds={1}
      busy={false}
      error={null}
      databases={DATABASES}
      filled={new Set()}
      onReplan={onReplan}
      onApprove={onApprove}
      {...over}
    />,
  );
  /** Re-render with a different plan, everything else held constant — what a poll
   *  or a completed re-plan does to this component. `over` is spread *before*
   *  `plan`, so a caller that set an initial plan does not silently re-render with
   *  it and make the re-seed assertion vacuous. */
  const repoll = (next: ReportPlan) =>
    rerender(
      <PlanEditor
        rounds={1}
        busy={false}
        error={null}
        databases={DATABASES}
        filled={new Set()}
        onReplan={onReplan}
        onApprove={onApprove}
        {...over}
        plan={next}
      />,
    );
  return { onReplan, onApprove, repoll };
}

const sectionTitles = () => screen.getAllByLabelText(/Назва розділу|Section title/);
const removeButtons = () =>
  screen.getAllByRole("button", { name: /Видалити розділ|Remove section/ });

describe("PlanEditor", () => {
  it("sends the edited plan, not the one it was given", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderEditor();
    const titles = screen.getAllByLabelText(/Назва розділу|Section title/);
    await user.clear(titles[0]);
    await user.type(titles[0], "Мій заголовок");
    await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove.mock.calls[0][0].sections[0].title).toBe("Мій заголовок");
  });

  it("removes a section and does not send it", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderEditor();
    await user.click(screen.getAllByRole("button", { name: /Видалити розділ|Remove section/ })[0]);
    await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
    const sent = onApprove.mock.calls[0][0];
    expect(sent.sections).toHaveLength(1);
    expect(sent.sections[0].title).toBe("Методологія");
  });

  it("shows the chosen source as text, not a control", () => {
    // The harness pins every test to English (see LanguageSwitcher.test.tsx); this
    // one asserts the uk strings specifically, so it switches for itself. The next
    // test's beforeEach pins back to English — nothing to restore here.
    void i18n.changeLanguage("uk");
    renderEditor();
    expect(screen.getByText("БД")).toBeInTheDocument();
    expect(screen.getByText("Документи")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /БД|Database/ })).toBeNull();
    expect(screen.queryByRole("group", { name: /Джерело|Source/ })).toBeNull();
  });

  it("blocks approval while a field is blank but still allows a re-plan", async () => {
    const user = userEvent.setup();
    const { onReplan } = renderEditor({
      plan: plan({
        sections: [
          { title: "Маржа", source: "database", databases: [DB], columns: [], sub_question: "" },
        ],
      }),
    });
    expect(screen.getByRole("button", { name: /Далі|Next/ })).toBeDisabled();
    const replan = screen.getByRole("button", { name: /Перепланувати|Re-plan/ });
    expect(replan).toBeEnabled();
    await user.click(replan);
    expect(onReplan).toHaveBeenCalledTimes(1);
  });

  // addSection also leaves the new section's title and sub-question blank, and
  // `incomplete` already blocks on those — so a section with only its source
  // blank would pass Next regardless of the source check. Filling in the title
  // and sub-question here isolates the source as the one remaining blank field,
  // so this test (and the blank(s.source) mutation-proof) genuinely exercises
  // the source guard rather than riding on the title guard's coattails.
  it("a section the reader adds has no source, and blocks approval until a re-plan fills it", async () => {
    const user = userEvent.setup();
    const { onReplan } = renderEditor();
    await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
    expect(screen.getByLabelText(/Джерело ще не обрано|Source not chosen yet/)).toBeInTheDocument();
    // getByLabelText only proves the attribute is present in the DOM, not that
    // assistive tech would honour it (a bare <span> is role=generic, which ARIA
    // prohibits from having an accessible name) — so also assert the visible "—"
    // placeholder and the unset modifier class, which do not depend on that.
    expect(screen.getByText("—")).toHaveClass("plan-source-unset");
    await user.type(sectionTitles()[2], "Нова секція");
    await user.type(screen.getAllByLabelText(/Підпитання|Sub-question/)[2], "нове питання");
    expect(screen.getByRole("button", { name: /Далі|Next/ })).toBeDisabled();
    const replan = screen.getByRole("button", { name: /Перепланувати|Re-plan/ });
    expect(replan).toBeEnabled();
    await user.click(replan);
    expect(onReplan.mock.calls[0][0].sections[2].source).toBe("");
  });

  it("caps sections at six", async () => {
    const user = userEvent.setup();
    const six = Array.from({ length: 6 }, (_, i) => ({
      title: `S${i}`,
      source: "database" as const,
      databases: [DB],
      columns: [],
      sub_question: "q",
    }));
    renderEditor({ plan: plan({ sections: six }) });
    expect(screen.getByRole("button", { name: /Додати розділ|Add section/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
    expect(screen.getAllByLabelText(/Назва розділу|Section title/)).toHaveLength(6);
  });

  it("marks which fields the model filled and which the user kept", () => {
    renderEditor({ filled: new Set(["s1.sub_question"]) });
    expect(screen.getByText(/дописано LLM|filled in by the LLM/)).toBeInTheDocument();
    expect(screen.getAllByText(/закріплено вами|kept from your edit/).length).toBeGreaterThan(0);
  });

  // Round one is all the model's work, and no round has come back to attribute
  // anything to the reader — so a "kept from your edit" badge would be a lie about
  // who wrote the text. Without this assertion, loosening the guard to `rounds >= 1`
  // passes the whole suite.
  it("attributes nothing to the reader before the first round comes back", () => {
    renderEditor({ rounds: 1, filled: new Set() });
    expect(screen.queryByText(/закріплено вами|kept from your edit/)).toBeNull();
    expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).toBeNull();
  });

  // A round is an LLM call lasting seconds and the plan stays on screen throughout.
  // An editable field is an invitation to type into a draft the landing round will
  // replace wholesale — the user's edits silently discarded, which is the one thing
  // this whole loop exists to prevent.
  it("freezes every control while a round is in flight, not just the buttons", () => {
    renderEditor({ busy: true });
    expect(screen.getByLabelText(/Назва звіту|Report title/)).toBeDisabled();
    expect(screen.getByLabelText(/Мова звіту|Report language/)).toBeDisabled();
    expect(sectionTitles()[0]).toBeDisabled();
    expect(screen.getAllByLabelText(/Підпитання|Sub-question/)[0]).toBeDisabled();
    expect(removeButtons()[0]).toBeDisabled();
    expect(screen.getAllByRole("button", { name: /Нижче|Move down/ })[0]).toBeDisabled();
  });

  it("caps the fields at the lengths the server accepts", () => {
    renderEditor();
    expect(screen.getByLabelText(/Назва звіту|Report title/)).toHaveAttribute("maxlength", "300");
    expect(sectionTitles()[0]).toHaveAttribute("maxlength", "300");
    expect(screen.getAllByLabelText(/Підпитання|Sub-question/)[0]).toHaveAttribute(
      "maxlength",
      "2000",
    );
  });

  it("sends the edited plan to a re-plan too, not the one it was given", async () => {
    const user = userEvent.setup();
    const { onReplan } = renderEditor();
    await user.clear(sectionTitles()[0]);
    await user.type(sectionTitles()[0], "Мій заголовок");
    await user.click(removeButtons()[1]);
    await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
    const sent = onReplan.mock.calls[0][0];
    expect(sent.sections).toHaveLength(1);
    expect(sent.sections[0].title).toBe("Мій заголовок");
  });

  // ReportsPage re-polls listReports every 2s while any report in the repository is
  // generating, handing this component a freshly parsed plan object each time. If
  // the re-seed keys on identity rather than value, the draft is thrown away
  // mid-keystroke and the user's edits vanish while they type.
  it("keeps the user's edits when an equal but newly-built plan arrives", async () => {
    const user = userEvent.setup();
    const { repoll } = renderEditor();
    await user.clear(sectionTitles()[0]);
    await user.type(sectionTitles()[0], "Мій заголовок");
    repoll(plan());
    expect(sectionTitles()[0]).toHaveValue("Мій заголовок");
  });

  it("replaces the draft when a genuinely different plan arrives", async () => {
    const user = userEvent.setup();
    const { repoll } = renderEditor();
    await user.clear(sectionTitles()[0]);
    await user.type(sectionTitles()[0], "Мій заголовок");
    repoll(
      plan({
        sections: [
          {
            title: "Ліквідність",
            source: "database",
            databases: [DB],
            columns: [],
            sub_question: "скільки?",
          },
        ],
      }),
    );
    expect(sectionTitles()).toHaveLength(1);
    expect(sectionTitles()[0]).toHaveValue("Ліквідність");
  });

  it("normalises a language the select cannot represent", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderEditor({ plan: plan({ language: "" }) });
    expect(screen.getByLabelText(/Мова звіту|Report language/)).toHaveValue("uk");
    await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
    expect(onApprove.mock.calls[0][0].language).toBe("uk");
  });

  it("refuses to remove the last section", () => {
    renderEditor({
      plan: plan({
        sections: [
          { title: "Маржа", source: "database", databases: [DB], columns: [], sub_question: "q" },
        ],
      }),
    });
    expect(removeButtons()[0]).toBeDisabled();
  });

  // `filled`'s s{i} keys index the received plan. A reorder keeps every index
  // occupied but changes what occupies it, so the badge stays on screen while
  // pointing at a section the model never touched — attribution must stop, not
  // merely happen to fall off the end of the list.
  it("stops attributing fields to the model once the user restructures the plan", async () => {
    const user = userEvent.setup();
    renderEditor({ filled: new Set(["s0.title"]) });
    const before = screen.getAllByRole("listitem")[0];
    expect(within(before).getByText(/дописано LLM|filled in by the LLM/)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /Нижче|Move down/ })[0]);
    // "Динаміка" is now second; without suppression the badge would sit on
    // "Методологія", crediting the model with text it never wrote.
    expect(sectionTitles()[0]).toHaveValue("Методологія");
    expect(screen.queryByText(/дописано LLM|filled in by the LLM/)).toBeNull();
  });
});

describe("diffFilled", () => {
  it("flags only fields that were blank on the way out and non-blank on the way back", () => {
    const sent = plan({
      title: "",
      sections: [
        {
          title: "Динаміка",
          source: "database",
          databases: [DB],
          columns: [],
          sub_question: "дохід за FY25?",
        },
        { title: "Маржа", source: "database", databases: [DB], columns: [], sub_question: "" },
      ],
    });
    const received = plan({
      title: "Фінансовий огляд",
      sections: [
        {
          title: "Динаміка",
          source: "database",
          databases: [DB],
          columns: [],
          sub_question: "дохід за FY25?",
        },
        {
          title: "Маржа",
          source: "database",
          databases: [DB],
          columns: [],
          sub_question: "Яка маржа?",
        },
      ],
    });
    expect(diffFilled(sent, received)).toEqual(new Set(["title", "s1.sub_question"]));
  });

  it("flags nothing when the user left no blanks", () => {
    expect(diffFilled(plan(), plan())).toEqual(new Set());
  });
});

// #203: a section names WHICH database it reads, not just that it reads one.
// A repository owns several since #201, so the source chip alone no longer tells
// the reader what a section will actually run against.
describe("PlanEditor section routing", () => {
  it("prints the name of the database a section reads", () => {
    renderEditor();
    expect(screen.getByLabelText(/(База даних|Database): Apple/)).toHaveTextContent("Apple");
  });

  it("shows no database chip on a documents section", () => {
    renderEditor({
      plan: plan({
        sections: [
          {
            title: "Методологія",
            source: "documents",
            databases: [],
            columns: [],
            sub_question: "q",
          },
        ],
      }),
    });
    expect(screen.queryByLabelText(/База даних|Database:/)).not.toBeInTheDocument();
  });

  it("stays quiet rather than printing a raw id it cannot name", () => {
    // While the list is still loading, and for a connection deleted after the
    // plan was made. A UUID in a chip tells the reader less than no chip at all.
    renderEditor({ databases: [] });
    expect(screen.queryByLabelText(/База даних|Database:/)).not.toBeInTheDocument();
  });

  it("blocks Next on a database section with no database", () => {
    // Mirrors the server: `finalize_draft` refuses an unrouted section, so the
    // reader must not be able to press Generate and be told no.
    renderEditor({
      plan: plan({
        sections: [
          { title: "Маржа", source: "database", databases: [], columns: [], sub_question: "q" },
        ],
      }),
    });
    expect(screen.getByRole("button", { name: /Далі|Next/ })).toBeDisabled();
  });

  it("leaves Re-plan enabled on an unrouted section, because that is what fills it", () => {
    renderEditor({
      plan: plan({
        sections: [
          { title: "Маржа", source: "database", databases: [], columns: [], sub_question: "q" },
        ],
      }),
    });
    expect(screen.getByRole("button", { name: /Переплан|Re-plan/ })).toBeEnabled();
  });

  it("sends the routing back untouched — the plan chooses it, the reader reads it", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderEditor();
    await user.click(screen.getByRole("button", { name: /Далі|Next/ }));
    expect(onApprove.mock.calls[0][0].sections[0].databases).toEqual([DB]);
    expect(onApprove.mock.calls[0][0].sections[1].databases).toEqual([]);
  });

  it("adds a new section unrouted, for the next round to fill", async () => {
    const user = userEvent.setup();
    const { onReplan } = renderEditor();
    await user.click(screen.getByRole("button", { name: /Додати розділ|Add section/ }));
    await user.click(screen.getByRole("button", { name: /Переплан|Re-plan/ }));
    expect(onReplan.mock.calls[0][0].sections[2].databases).toEqual([]);
  });
});

describe("a section that merges several databases (#204)", () => {
  const DB2 = "22222222-2222-2222-2222-222222222222";
  const BOTH = [...DATABASES, { id: DB2, name: "Microsoft", description: "" }];

  it("names every database the section reads", () => {
    renderEditor({
      databases: BOTH,
      plan: plan({
        sections: [
          {
            title: "Порівняння",
            source: "database",
            databases: [DB, DB2],
            columns: ["company", "revenue"],
            sub_question: "дохід обох",
          },
        ],
      }),
    });
    expect(screen.getByText("Apple, Microsoft")).toBeInTheDocument();
  });

  it("carries the output contract through an edit untouched", async () => {
    /** The editor has no control for it — the plan chooses (#186) — so the only
     *  way it can be lost is by being dropped on the way back out, which would
     *  leave the section's queries with no agreed shape. */
    const user = userEvent.setup();
    const { onReplan } = renderEditor({
      databases: BOTH,
      plan: plan({
        sections: [
          {
            title: "Порівняння",
            source: "database",
            databases: [DB, DB2],
            columns: ["company", "revenue"],
            sub_question: "дохід обох",
          },
        ],
      }),
    });
    await user.type(screen.getByDisplayValue("Порівняння"), "!");
    await user.click(screen.getByRole("button", { name: /Перепланувати|Re-plan/ }));
    expect(onReplan).toHaveBeenCalledWith(
      expect.objectContaining({
        sections: [expect.objectContaining({ columns: ["company", "revenue"] })],
      }),
    );
  });
});
