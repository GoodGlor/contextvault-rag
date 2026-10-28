import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ReportCanvas } from "./ReportCanvas";
import type { ReportCanvasProps } from "./ReportCanvas";

/** The canvas with its scale controlled, the way the page controls it (#172).
 *
 *  `zoom` moved out of this component so it could outlive the view switch that
 *  unmounts it. The tests below still assert on the readout, so the harness has
 *  to hold the value — rendering `ReportCanvas` with a fixed `zoom` and a bare
 *  spy would make every one of them pass without the buttons doing anything.
 *  The caller's `onZoom` is still called, so a test may assert on it too. */
function Controlled({ zoom: initial, onZoom, ...rest }: ReportCanvasProps): ReactNode {
  const [zoom, setZoom] = useState(initial);
  return (
    <ReportCanvas
      {...rest}
      zoom={zoom}
      onZoom={(next) => {
        setZoom(next);
        onZoom(next);
      }}
    />
  );
}

/** The canvas.
 *
 *  What jsdom can see here is the DOM: that the document lands inside a shadow
 *  root, that a click selects the block under it, and that the toolbar reaches
 *  the block list. **What it cannot see is layout** — jsdom performs none, so
 *  every rect is zero. The hover tag's and the toolbar's *positions* are
 *  therefore asserted in e2e and checked by screenshot, not here, and nothing
 *  below pretends otherwise.
 */

const DOC = `
<style>p { color: red }</style>
<div class="cv-sheet">
  <h2 data-block-index="0">Виторг</h2>
  <p data-block-index="1" data-block-text="1" contenteditable="true">Текст</p>
  <table data-block-index="2"><tbody><tr><td>Київ</td></tr></tbody></table>
</div>`;

function setup(over: Partial<ReportCanvasProps> = {}): ReportCanvasProps {
  const props: ReportCanvasProps = {
    html: DOC,
    selected: null,
    onSelect: vi.fn(),
    label: (index) => `блок ${index}`,
    onMove: vi.fn(),
    onToggleHidden: vi.fn(),
    onDelete: vi.fn(),
    onEditText: vi.fn(),
    blockCount: 3,
    zoom: 1,
    onZoom: vi.fn(),
    autoFit: false,
    ...over,
  };
  render(<Controlled {...props} />);
  return props;
}

/** Same props, but keeping the handle a redraw test needs. */
function mount(over: Partial<ReportCanvasProps> = {}) {
  const props: ReportCanvasProps = {
    html: DOC,
    selected: null,
    onSelect: vi.fn(),
    label: (index) => `блок ${index}`,
    onMove: vi.fn(),
    onToggleHidden: vi.fn(),
    onDelete: vi.fn(),
    onEditText: vi.fn(),
    blockCount: 3,
    zoom: 1,
    onZoom: vi.fn(),
    autoFit: false,
    ...over,
  };
  return { props, ...render(<Controlled {...props} />) };
}

/** Every event the canvas reads crosses the shadow boundary, so every one of
 *  them is dispatched `composed` — an event without it never reaches the host
 *  and the assertion would be about a listener that was never called. */
function fire(target: Element, type: string): void {
  target.dispatchEvent(new Event(type, { bubbles: true, composed: true }));
}

/** Let the deferred flush run.
 *
 *  The canvas draws a held document a turn *after* `focusout`, because focus
 *  moving between two editable runs fires `focusout` before `focusin`. Inside
 *  `act` so the state that redraw sets is flushed before the assertion — a bare
 *  `waitFor` sees the DOM but leaves React complaining, which is a warning the
 *  suite would then carry forever. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function root(): ShadowRoot {
  const host = screen.getByTestId("canvas-host");
  expect(host.shadowRoot).not.toBeNull();
  return host.shadowRoot as ShadowRoot;
}

describe("ReportCanvas", () => {
  it("mounts the report inside a shadow root, so its stylesheet cannot reach the app", () => {
    // `stylesheet()` styles bare `p`, `table`, `h2`, `body`. In the light DOM
    // that is not a preview, it is a restyle of every page the router can reach.
    const { container } = render(
      <ReportCanvas
        html={DOC}
        selected={null}
        onSelect={vi.fn()}
        label={(i) => `блок ${i}`}
        onMove={vi.fn()}
        onToggleHidden={vi.fn()}
        onDelete={vi.fn()}
        onEditText={vi.fn()}
        blockCount={3}
        zoom={1}
        onZoom={vi.fn()}
        autoFit={false}
      />,
    );

    expect(root().querySelector("style")).not.toBeNull();
    expect(container.querySelector("style")).toBeNull();
    expect(container.querySelector("[data-block-index]")).toBeNull();
  });

  it("selects the block under the click, by its list index", async () => {
    const props = setup();

    root()
      .querySelector('[data-block-index="2"]')!
      .dispatchEvent(new MouseEvent("click", { bubbles: true, composed: true }));

    expect(props.onSelect).toHaveBeenCalledWith(2);
  });

  it("clicking the sheet outside every block clears the selection", () => {
    const props = setup({ selected: 1 });

    root()
      .querySelector(".cv-sheet")!
      .dispatchEvent(new MouseEvent("click", { bubbles: true, composed: true }));

    expect(props.onSelect).toHaveBeenCalledWith(null);
  });

  it("finds the block from a click on something inside it", () => {
    // A reader clicks a table cell, not the table. `closest` is what makes the
    // whole block the target rather than whatever leaf was under the pointer.
    const props = setup();

    root()
      .querySelector("td")!
      .dispatchEvent(new MouseEvent("click", { bubbles: true, composed: true }));

    expect(props.onSelect).toHaveBeenCalledWith(2);
  });

  it("rings the selected block, and rings only it", () => {
    setup({ selected: 1 });

    expect(root().querySelectorAll(".cv-selected")).toHaveLength(1);
    expect(root().querySelector('[data-block-index="1"]')!.className).toContain("cv-selected");
  });

  it("moves the ring when the selection moves, leaving none behind", () => {
    const { rerender } = render(
      <ReportCanvas
        html={DOC}
        selected={1}
        onSelect={vi.fn()}
        label={(i) => `блок ${i}`}
        onMove={vi.fn()}
        onToggleHidden={vi.fn()}
        onDelete={vi.fn()}
        onEditText={vi.fn()}
        blockCount={3}
        zoom={1}
        onZoom={vi.fn()}
        autoFit={false}
      />,
    );

    rerender(
      <ReportCanvas
        html={DOC}
        selected={2}
        onSelect={vi.fn()}
        label={(i) => `блок ${i}`}
        onMove={vi.fn()}
        onToggleHidden={vi.fn()}
        onDelete={vi.fn()}
        onEditText={vi.fn()}
        blockCount={3}
        zoom={1}
        onZoom={vi.fn()}
        autoFit={false}
      />,
    );

    const rings = root().querySelectorAll(".cv-selected");
    expect(rings).toHaveLength(1);
    expect(rings[0].getAttribute("data-block-index")).toBe("2");
  });

  it("a new document replaces the old one rather than stacking on it", () => {
    // Every preview publishes a *whole* document. Appending would grow the
    // canvas by one more report per keystroke's worth of debounce.
    const { rerender } = render(
      <ReportCanvas
        html={DOC}
        selected={null}
        onSelect={vi.fn()}
        label={(i) => `блок ${i}`}
        onMove={vi.fn()}
        onToggleHidden={vi.fn()}
        onDelete={vi.fn()}
        onEditText={vi.fn()}
        blockCount={3}
        zoom={1}
        onZoom={vi.fn()}
        autoFit={false}
      />,
    );

    rerender(
      <ReportCanvas
        html={'<div class="cv-sheet"><h2 data-block-index="0">Інше</h2></div>'}
        selected={null}
        onSelect={vi.fn()}
        label={(i) => `блок ${i}`}
        onMove={vi.fn()}
        onToggleHidden={vi.fn()}
        onDelete={vi.fn()}
        onEditText={vi.fn()}
        blockCount={1}
        zoom={1}
        onZoom={vi.fn()}
        autoFit={false}
      />,
    );

    expect(root().querySelectorAll(".cv-sheet")).toHaveLength(1);
    expect(root().querySelectorAll("[data-block-index]")).toHaveLength(1);
  });

  it("names the block under the pointer", async () => {
    setup();

    root()
      .querySelector('[data-block-index="1"]')!
      .dispatchEvent(new MouseEvent("mouseover", { bubbles: true, composed: true }));

    expect(await screen.findByTestId("canvas-tag")).toHaveTextContent("блок 1");
  });

  it("shows no toolbar when nothing is selected", () => {
    setup();

    expect(screen.queryByTestId("canvas-toolbar")).not.toBeInTheDocument();
  });

  it("moves, hides and deletes the selected block", async () => {
    const props = setup({ selected: 1 });

    await userEvent.click(screen.getByTitle(/up|вгору/i));
    await userEvent.click(screen.getByTitle(/down|вниз/i));
    await userEvent.click(screen.getByTitle(/hide|сховати/i));
    await userEvent.click(screen.getByTitle(/delete|видалити/i));

    expect(props.onMove).toHaveBeenCalledWith(1, 0);
    expect(props.onMove).toHaveBeenCalledWith(1, 2);
    expect(props.onToggleHidden).toHaveBeenCalledWith(1);
    expect(props.onDelete).toHaveBeenCalledWith(1);
  });

  it("refuses to move the first block up or the last one down", () => {
    setup({ selected: 0 });

    expect(screen.getByTitle(/up|вгору/i)).toBeDisabled();
    expect(screen.getByTitle(/down|вниз/i)).toBeEnabled();
  });

  it("a toolbar click does not also select whatever is beneath it", async () => {
    const props = setup({ selected: 1 });

    await userEvent.click(screen.getByTitle(/hide|сховати/i));

    expect(props.onSelect).not.toHaveBeenCalled();
  });

  it("selecting a hidden block shows no toolbar rather than throwing", () => {
    // A hidden block renders nothing, so there is no element to ring or to hang
    // a toolbar on. The tree still shows it selected, which is the point.
    setup({ selected: 9 });

    expect(screen.queryByTestId("canvas-toolbar")).not.toBeInTheDocument();
  });

  it("shows the hint until the first document arrives", () => {
    setup({ html: null });

    expect(screen.getByText(/sheet redraws|аркуш/i)).toBeInTheDocument();
  });
});

describe("ReportCanvas — editing on the sheet", () => {
  it("reads a typed edit back as that block's text override", () => {
    const props = setup();
    const paragraph = root().querySelector('[data-block-text="1"]')!;

    paragraph.textContent = "Переписано";
    fire(paragraph, "input");

    expect(props.onEditText).toHaveBeenCalledWith(1, "Переписано");
  });

  it("takes the index from the text mark, not from the block it sits in", () => {
    // They agree today. They need not: a cover's editable run is its `<h1>`,
    // nested inside the block that carries `data-block-index`. Reading the
    // block's own attribute would work there by luck and break the moment a
    // block nests another.
    const props = setup({
      html: `<div class="cv-sheet"><div data-block-index="0">
               <p class="eyebrow">Звіт</p>
               <h1 data-block-text="0" contenteditable="true">Назва</h1>
             </div></div>`,
    });
    const heading = root().querySelector("h1")!;

    heading.textContent = "Інша назва";
    fire(heading, "input");

    expect(props.onEditText).toHaveBeenCalledWith(0, "Інша назва");
  });

  it("an emptied run is still an override, not a return to the generated words", () => {
    // Re-homed from the retired textarea. An empty override is a decision —
    // "print nothing here" — and is not the same as never having overridden.
    // Publishing `undefined` for an empty run would silently restore the model's
    // words the moment a reader cleared a paragraph.
    const props = setup();
    const paragraph = root().querySelector('[data-block-text="1"]')!;

    paragraph.textContent = "";
    fire(paragraph, "input");

    expect(props.onEditText).toHaveBeenCalledWith(1, "");
  });

  it("does not read an edit out of a block that has no editable run", () => {
    // A table prints values derived from the stored result. The document does
    // not mark it, and the canvas must not invent a mark for it.
    const props = setup();

    fire(root().querySelector('[data-block-index="2"]')!, "input");

    expect(props.onEditText).not.toHaveBeenCalled();
  });

  it("holds a redraw while the caret is in the document", () => {
    // The whole answer to the card's open question. A preview round-trip is
    // ~300 ms and the document is replaced wholesale, so applying it under a
    // caret destroys the element the reader is typing into.
    const { rerender, props } = mount();
    fire(root().querySelector('[data-block-text="1"]')!, "focusin");

    rerender(<Controlled {...props} html={DOC.replace("Текст", "Свіжий текст")} />);

    expect(root().textContent).toContain("Текст");
    expect(root().textContent).not.toContain("Свіжий текст");
  });

  it("draws the held document once the reader looks away", async () => {
    const { rerender, props } = mount();
    const paragraph = root().querySelector('[data-block-text="1"]')!;
    fire(paragraph, "focusin");
    rerender(<Controlled {...props} html={DOC.replace("Текст", "Свіжий текст")} />);

    fire(paragraph, "focusout");
    await settle();

    expect(root().textContent).toContain("Свіжий текст");
  });

  it("draws a new document at once when nothing is being edited", () => {
    // The hold must be exactly as wide as the caret. A canvas that waited for a
    // blur that never comes would simply stop updating.
    const { rerender, props } = mount();

    rerender(<Controlled {...props} html={DOC.replace("Текст", "Свіжий текст")} />);

    expect(root().textContent).toContain("Свіжий текст");
  });

  it("rings the selected block again after a held document is drawn", async () => {
    // The ring is written onto the element, and a redraw replaces every element
    // in the document. Without this the selection survives in the tree and
    // vanishes from the sheet.
    const { rerender, props } = mount({ selected: 0 });
    const paragraph = root().querySelector('[data-block-text="1"]')!;
    fire(paragraph, "focusin");
    rerender(<Controlled {...props} selected={0} html={DOC.replace("Виторг", "Дохід")} />);
    fire(paragraph, "focusout");
    await settle();

    expect(root().querySelector('[data-block-index="0"]')!.className).toContain("cv-selected");
  });

  /** The zoom (#168).
   *
   *  The scale is a custom property on the host, and `_preview_css` turns it into
   *  `zoom` on the sheet inside the shadow root. jsdom can see the property being
   *  written and that the document is not rebuilt; it cannot see a scaled page,
   *  so *that* is asserted in e2e and by screenshot.
   */
  describe("the zoom", () => {
    function zoomOf(): string {
      return screen.getByTestId("canvas-host").style.getPropertyValue("--cv-zoom");
    }

    it("shows the sheet life-size until it is asked for anything else", () => {
      setup();
      expect(zoomOf()).toBe("1");
      expect(screen.getByTestId("canvas-zoom")).toHaveTextContent("100%");
    });

    it("scales the sheet without rebuilding the document", async () => {
      // The whole point of the control: a rebuild is ~300 ms of chart redraw and
      // a request, and a reader changing the scale asked for neither. Element
      // identity is the evidence — a redraw replaces every node in the root.
      setup();
      const before = root().querySelector('[data-block-index="0"]');

      await userEvent.click(screen.getByRole("button", { name: "Zoom in" }));

      expect(zoomOf()).toBe("1.25");
      expect(root().querySelector('[data-block-index="0"]')).toBe(before);
    });

    it("stops at 150% and at 50%", async () => {
      setup();
      const bigger = screen.getByRole("button", { name: "Zoom in" });
      const smaller = screen.getByRole("button", { name: "Zoom out" });

      for (let i = 0; i < 6; i += 1) await userEvent.click(bigger);
      expect(zoomOf()).toBe("1.5");
      expect(bigger).toBeDisabled();

      for (let i = 0; i < 8; i += 1) await userEvent.click(smaller);
      expect(zoomOf()).toBe("0.5");
      expect(smaller).toBeDisabled();
    });

    it("fits to width by measuring the canvas, not by guessing at it", async () => {
      // #167's lesson, applied: the one number that decides this comes out of the
      // element rather than off paper. 794px is A4 at 96dpi.
      setup();
      const host = screen.getByTestId("canvas-host");
      Object.defineProperty(host, "clientWidth", { value: 596, configurable: true });

      await userEvent.click(screen.getByRole("button", { name: "Fit to width" }));

      expect(Number(zoomOf())).toBeCloseTo(596 / 794, 3);
      expect(screen.getByTestId("canvas-zoom")).toHaveTextContent("75%");
    });

    it("refuses a fit that would fall outside the offered range", async () => {
      // A narrow window would otherwise ask for an illegible 30% sheet, and a
      // very wide one for a 300% sheet nobody chose.
      setup();
      const host = screen.getByTestId("canvas-host");
      Object.defineProperty(host, "clientWidth", { value: 200, configurable: true });

      await userEvent.click(screen.getByRole("button", { name: "Fit to width" }));

      expect(zoomOf()).toBe("0.5");
    });
  });
});

describe("opening fitted", () => {
  // jsdom performs no layout, so `clientWidth` is 0 on every element. That is
  // exactly the unmeasured case, which makes the guard unit-testable — and the
  // measured case needs the prototype stubbed, because nothing else in jsdom
  // will ever report a width.
  function withWidth(px: number, run: () => void): void {
    const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth");
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get: () => px,
    });
    try {
      run();
    } finally {
      if (original === undefined) {
        delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
      } else {
        Object.defineProperty(HTMLElement.prototype, "clientWidth", original);
      }
    }
  }

  it("fits to the measured width when it opens", () => {
    const onZoom = vi.fn();
    withWidth(900, () => {
      setup({ autoFit: true, onZoom });
    });
    // 900 / 794 = 1.1335…, tidied to the nearest thousandth.
    expect(onZoom).toHaveBeenCalledWith(1.134);
  });

  it("leaves the zoom alone when the host has not been measured", () => {
    // The recorded trap: `fit()` clamps 0 up to MIN, so an unguarded effect
    // would open every report at 50 % instead of fitted.
    const onZoom = vi.fn();
    setup({ autoFit: true, onZoom });
    expect(onZoom).not.toHaveBeenCalled();
  });

  it("does not fit when the reader already chose a zoom", () => {
    const onZoom = vi.fn();
    withWidth(900, () => {
      setup({ autoFit: false, onZoom });
    });
    expect(onZoom).not.toHaveBeenCalled();
  });
});
