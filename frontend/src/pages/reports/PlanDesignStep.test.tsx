import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { PlanDesignStep } from "./PlanDesignStep";
import { ACCENTS } from "./StyleInspector";
import "../../i18n";

vi.mock("../../api/reports", async () => {
  const actual = await vi.importActual<typeof import("../../api/reports")>("../../api/reports");
  return { ...actual, getReportDesign: vi.fn(), setReportDesign: vi.fn() };
});

vi.mock("../../api/reportAssets", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/reportAssets")>("../../api/reportAssets");
  return { ...actual, uploadReportAsset: vi.fn() };
});

import { getReportDesign, setReportDesign } from "../../api/reports";
import { uploadReportAsset } from "../../api/reportAssets";

const TEMPLATES = [
  { key: "kvarta", name: "Кварта" },
  { key: "prezydia", name: "Президія" },
];

function setup(over: Partial<React.ComponentProps<typeof PlanDesignStep>> = {}) {
  const onBack = vi.fn();
  const onGenerate = vi.fn();
  render(
    <PlanDesignStep
      repositoryId="r1"
      reportId="rep1"
      templates={TEMPLATES as never}
      templatesError={null}
      busy={false}
      error={null}
      onBack={onBack}
      onGenerate={onGenerate}
      {...over}
    />,
  );
  return { onBack, onGenerate };
}

describe("PlanDesignStep", () => {
  beforeEach(() => {
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
    vi.mocked(uploadReportAsset).mockResolvedValue({
      id: "asset1",
      filename: "logo.png",
      mime: "image/png",
      width: 10,
      height: 10,
      byte_size: 10,
    } as never);
  });
  afterEach(() => vi.clearAllMocks());

  it("stores a chosen template and never sends a block list", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(
      await screen.findByRole("button", { name: /Шаблон оформлення|Design template/ }),
    );
    await user.click(await screen.findByRole("button", { name: /Президія/ }));
    await waitFor(() => expect(setReportDesign).toHaveBeenCalled());
    const patch = vi.mocked(setReportDesign).mock.calls.at(-1)?.[2];
    expect(patch?.template).toBe("prezydia");
    expect(patch && "blocks" in patch).toBe(false);
  });

  it("says which template is chosen, and says so when none is", async () => {
    const user = userEvent.setup();
    setup();
    expect(await screen.findByText(/Шаблон не обрано|No template chosen/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Шаблон оформлення|Design template/ }));
    await user.click(await screen.findByRole("button", { name: /Кварта/ }));
    expect(await screen.findByText(/(Обрано|Chosen): Кварта/)).toBeInTheDocument();
  });

  it("hands Back and Generate straight to their callbacks", async () => {
    const user = userEvent.setup();
    const { onBack, onGenerate } = setup();
    await user.click(await screen.findByRole("button", { name: /Назад|Back/ }));
    await user.click(screen.getByRole("button", { name: /Згенерувати|Generate report/ }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(onGenerate).toHaveBeenCalledTimes(1);
  });

  it("disables Generate while the approve round is in flight", async () => {
    setup({ busy: true });
    expect(
      await screen.findByRole("button", { name: /Згенерувати|Generate report/ }),
    ).toBeDisabled();
  });

  it("mounts the inspector, and an inspector edit is saved", async () => {
    const user = userEvent.setup();
    setup();
    const chip = (await screen.findAllByTestId("pairing-chip"))[1];
    expect(chip).toBeDefined();
    await user.click(chip!);
    await waitFor(() => {
      const patch = vi.mocked(setReportDesign).mock.calls.at(-1)?.[2];
      expect(patch?.typography).toBeDefined();
    });
  });

  it("keeps an accent chosen while the logo upload is still in flight", async () => {
    // The defect: `onLogoFile` read `draft.style.brand` when the upload STARTED
    // and wrote `{ ...that, logo_asset_id }` when it resolved. Design patches
    // merge by whole key, so the accent the reader picked in between was
    // replaced by the stale snapshot — on screen and in the PUT that followed.
    const user = userEvent.setup();
    let land: (asset: unknown) => void = () => {};
    vi.mocked(uploadReportAsset).mockReturnValue(
      new Promise((resolve) => {
        land = resolve;
      }) as never,
    );
    setup();

    const input = await screen.findByTestId("logo-file");
    await user.upload(input, new File(["x"], "logo.png", { type: "image/png" }));

    // …and while it is in flight, the reader picks an accent. Synchronous, so it
    // lands first; the upload's write is the one that must not undo it.
    const swatch = screen
      .getAllByTestId("accent-swatch")
      .find((el) => el.getAttribute("data-accent") === ACCENTS[1]);
    await user.click(swatch!);

    land({
      id: "asset1",
      filename: "logo.png",
      mime: "image/png",
      width: 10,
      height: 10,
      byte_size: 10,
    });

    await waitFor(() => expect(setReportDesign).toHaveBeenCalled());
    await waitFor(() => {
      const brands = vi
        .mocked(setReportDesign)
        .mock.calls.map(([, , patch]) => (patch as { brand?: Record<string, unknown> }).brand)
        .filter((brand): brand is Record<string, unknown> => brand !== undefined);
      const last = brands[brands.length - 1];
      // Both, in the same patch: whichever wrote last must have carried the other.
      expect(last).toMatchObject({ accent: ACCENTS[1], logo_asset_id: "asset1" });
    });
  });

  it("surfaces an error when the logo upload fails", async () => {
    const user = userEvent.setup();
    vi.mocked(uploadReportAsset).mockRejectedValueOnce(new Error("nope"));
    setup();
    const input = await screen.findByTestId("logo-file");
    const file = new File(["x"], "logo.png", { type: "image/png" });
    await user.upload(input, file);
    expect(
      await screen.findByText(/Не вдалося завантажити логотип|Could not upload the logo/),
    ).toBeInTheDocument();
  });

  it("shows an in-flight status while busy", async () => {
    setup({ busy: true });
    // Looked up by test id, not `role: "status"` alone: the inspector's
    // <output> percentage readouts already carry that implicit ARIA role, and
    // `status` gives no name-from-content, so a name filter can't disambiguate.
    const status = await screen.findByTestId("generation-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent(/Генерація|Generating/);
  });

  it("shows no in-flight status when not busy", async () => {
    setup();
    await screen.findByText(/Шаблон не обрано|No template chosen/);
    expect(screen.queryByTestId("generation-status")).not.toBeInTheDocument();
  });

  it("stores the template before it hands generation on — not alongside it", async () => {
    // The branch's headline promise, and the one thing no unit test covered:
    // replacing `flush().then(onGenerate)` with a bare `onGenerate()` passed the
    // entire suite, leaving a wall-clock race in an e2e spec as the only guard.
    // Recording both sides in one list is what makes the order assertable.
    const order: string[] = [];
    vi.mocked(setReportDesign).mockImplementation(async () => {
      order.push("put");
      return {} as never;
    });
    const user = userEvent.setup();
    const onGenerate = vi.fn(() => order.push("approve"));
    setup({ onGenerate });

    await user.click(
      await screen.findByRole("button", { name: /Шаблон оформлення|Design template/ }),
    );
    await user.click(await screen.findByRole("button", { name: /Президія/ }));
    // Straight to Generate, with the debounce still ticking — the reader who
    // knows what they want and does not linger.
    await user.click(screen.getByRole("button", { name: /Згенерувати|Generate report/ }));

    await waitFor(() => expect(onGenerate).toHaveBeenCalled());
    expect(order).toEqual(["put", "approve"]);
  });

  it("Back stores a change made inside the debounce window rather than dropping it", async () => {
    // Back unmounts the step in the page — the ternary in ReportsPage swaps it
    // for PlanEditor — and the unmount cleanup clears the debounce timer. So the
    // unmount is the whole point of this test: without it the timer survives and
    // fires on its own, and the assertion holds whether or not Back flushes.
    // (It did exactly that on the first attempt, which is how this comment
    // came to exist.)
    const user = userEvent.setup();
    let unmount = (): void => {};
    const onBack = vi.fn(() => unmount());
    const view = render(
      <PlanDesignStep
        repositoryId="r1"
        reportId="rep1"
        templates={TEMPLATES as never}
        templatesError={null}
        busy={false}
        error={null}
        onBack={onBack}
        onGenerate={vi.fn()}
      />,
    );
    unmount = view.unmount;

    await user.click(
      await screen.findByRole("button", { name: /Шаблон оформлення|Design template/ }),
    );
    await user.click(await screen.findByRole("button", { name: /Кварта/ }));
    await user.click(screen.getByRole("button", { name: /Назад|Back/ }));

    await waitFor(() => expect(onBack).toHaveBeenCalled());
    expect(setReportDesign).toHaveBeenCalledTimes(1);
    expect(vi.mocked(setReportDesign).mock.calls.at(-1)?.[2]).toEqual({ template: "kvarta" });
  });

  it("starts generation even when storing the design failed", async () => {
    // Deliberate: the reader pressed Generate, and refusing to start because a
    // style token did not save would be a worse answer than a report in the
    // previous design. Recorded as a decision rather than left incidental.
    vi.mocked(setReportDesign).mockRejectedValue(new Error("no"));
    const user = userEvent.setup();
    const onGenerate = vi.fn();
    setup({ onGenerate });

    await user.click(
      await screen.findByRole("button", { name: /Шаблон оформлення|Design template/ }),
    );
    await user.click(await screen.findByRole("button", { name: /Президія/ }));
    await user.click(screen.getByRole("button", { name: /Згенерувати|Generate report/ }));

    await waitFor(() => expect(onGenerate).toHaveBeenCalledTimes(1));
  });
});
