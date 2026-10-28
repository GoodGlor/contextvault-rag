import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReportPreview } from "./useReportPreview";

vi.mock("../../api/reports", () => ({
  setReportDesign: vi.fn(),
  rerenderReport: vi.fn(),
  downloadReport: vi.fn(),
  getReportPreviewHtml: vi.fn(),
}));

const { setReportDesign, rerenderReport, downloadReport, getReportPreviewHtml } =
  await import("../../api/reports");

const mocked = {
  design: vi.mocked(setReportDesign),
  rerender: vi.mocked(rerenderReport),
  download: vi.mocked(downloadReport),
  preview: vi.mocked(getReportPreviewHtml),
};

/** Resolves only when `release()` is called, so a render can be held in flight. */
function deferred<T>(): { promise: Promise<T>; release: (value: T) => void } {
  let release!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

/** Advance the debounce and let the render's promise chain settle.
 *
 *  `waitFor` cannot be used here: it schedules its own timers, and under fake
 *  timers those never fire, so every assertion waited out its five seconds.
 */
async function tick(ms = 0): Promise<void> {
  await act(async () => {
    if (ms > 0) vi.advanceTimersByTime(ms);
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });
}

let created: string[] = [];
let revoked: string[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  created = [];
  revoked = [];
  let n = 0;
  vi.stubGlobal("URL", {
    createObjectURL: () => {
      const url = `blob:${++n}`;
      created.push(url);
      return url;
    },
    revokeObjectURL: (url: string) => {
      revoked.push(url);
    },
  });
  mocked.design.mockReset().mockResolvedValue({} as never);
  mocked.rerender.mockReset().mockResolvedValue({} as never);
  mocked.download.mockReset().mockResolvedValue(new Blob(["pdf"]));
  mocked.preview.mockReset().mockResolvedValue('<div class="cv-sheet">…</div>');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function setup(delayMs = 300) {
  return renderHook(() => useReportPreview({ repositoryId: "r-1", reportId: "rep-1", delayMs }));
}

/** The hook opened on the PDF, for the tests that are about the blob's life
 *  rather than about scheduling. **The switch itself renders one PDF**, so the
 *  call counters are cleared but the blob arrays are not — a test about blob
 *  lifetimes must see every blob that was ever made. */
async function setupOnPdf(delayMs = 300) {
  const rendered = setup(delayMs);
  // The mount draws the sheet; let it finish before switching, so the switch is
  // not racing a render it did not start.
  await tick();
  act(() => rendered.result.current.setView("pdf"));
  await tick();
  mocked.design.mockClear();
  mocked.rerender.mockClear();
  mocked.download.mockClear();
  return rendered;
}

describe("useReportPreview", () => {
  it("debounces: a burst of edits renders once, with the last value", async () => {
    const { result } = setup();

    act(() => {
      result.current.request({ template: "kvarta" });
      result.current.request({ template: "prezydia" });
      result.current.request({ template: "chasopys" });
    });
    // Nothing has been asked of the server yet — the reader is still typing.
    expect(mocked.design).not.toHaveBeenCalled();

    await tick(300);

    expect(mocked.design).toHaveBeenCalledTimes(1);
    expect(mocked.design).toHaveBeenCalledWith("r-1", "rep-1", { template: "chasopys" });
  });

  it("restarts the idle wait on every edit rather than firing at the first deadline", async () => {
    // The burst test above cannot prove this: coalescing collapses a burst even
    // with no debounce at all, so it passes either way. Here the edits are spaced
    // so that a timer which is never reset would elapse mid-typing and render a
    // half-finished state.
    const { result } = setup();
    // Let the mount's own draw finish first: an edit arriving *during* a render
    // in flight is picked up on its completion by design (that is coalescing),
    // which would fire before the debounce and mask what this test is about. In
    // a real session the open completes long before anyone types.
    await tick();

    act(() => result.current.request({ template: "k" }));
    await tick(200);
    act(() => result.current.request({ template: "kv" }));
    await tick(200);
    // 400 ms have passed, but never 300 consecutive idle ones.
    expect(mocked.design).not.toHaveBeenCalled();

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    expect(mocked.design).toHaveBeenCalledTimes(1);
    expect(mocked.design).toHaveBeenCalledWith("r-1", "rep-1", { template: "kvarta" });
  });

  it("coalesces: edits during an in-flight render collapse into ONE follow-up", async () => {
    const held = deferred<Blob>();
    const { result } = await setupOnPdf();
    mocked.download.mockReturnValueOnce(held.promise);

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    expect(mocked.design).toHaveBeenCalledTimes(1);

    // Three more edits arrive while the first render is still in flight. A queue
    // would run three more renders; coalescing runs one, for the newest state.
    act(() => {
      result.current.request({ template: "a" });
      result.current.request({ template: "b" });
      result.current.request({ template: "c" });
    });
    await tick(300);
    expect(mocked.design).toHaveBeenCalledTimes(1);

    held.release(new Blob(["pdf"]));
    await tick();

    expect(mocked.design).toHaveBeenCalledTimes(2);
    expect(mocked.design).toHaveBeenLastCalledWith("r-1", "rep-1", { template: "c" });
    expect(mocked.design).toHaveBeenCalledTimes(2);
  });

  it("reports pending across the whole debounce-and-render cycle", async () => {
    const held = deferred<Blob>();
    const { result } = await setupOnPdf();
    mocked.download.mockReturnValueOnce(held.promise);

    expect(result.current.pending).toBe(false);
    act(() => result.current.request({ template: "kvarta" }));
    // Pending from the keystroke, not from the request: the preview on screen is
    // already stale, which is exactly what the indicator must say.
    expect(result.current.pending).toBe(true);

    await tick(300);
    expect(result.current.pending).toBe(true);

    held.release(new Blob(["pdf"]));
    await tick();
    expect(result.current.pending).toBe(false);
  });

  it("revokes the superseded blob rather than leaking it", async () => {
    const { result } = await setupOnPdf();
    // Opening the PDF view rendered one, so `created[0]` is that first blob.
    expect(created).toHaveLength(1);

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    expect(result.current.url).toBe(created[1]);
    expect(revoked).toEqual([created[0]]);

    act(() => result.current.request({ template: "prezydia" }));
    await tick(300);
    expect(result.current.url).toBe(created[2]);
    expect(revoked).toEqual([created[0], created[1]]);
  });

  it("drops a stale render that resolves after a newer one", async () => {
    const slow = deferred<Blob>();
    const { result } = await setupOnPdf();
    mocked.download.mockReturnValueOnce(slow.promise);

    act(() => result.current.request({ template: "old" }));
    await tick(300);
    expect(mocked.design).toHaveBeenCalledTimes(1);

    act(() => result.current.request({ template: "new" }));
    await tick(300);

    // The newer render completes first...
    slow.release(new Blob(["stale"]));
    await tick();
    expect(mocked.design).toHaveBeenCalledTimes(2);
    const fresh = result.current.url;

    // ...and the stale one, landing late, must not replace it.
    await tick();
    expect(result.current.url).toBe(fresh);
  });

  it("revokes on unmount so a preview cannot outlive its panel", async () => {
    const { result, unmount } = await setupOnPdf();

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    expect(result.current.url).not.toBeNull();

    unmount();
    expect(revoked).toContain(created[0]);
  });

  it("carries a block list, not only a template", async () => {
    // 5d edits structure, not just looks. The hook forwards whatever design it is
    // given, so the editor's reorder and the gallery's template choice go through
    // one debounced, coalesced path rather than two competing ones.
    const { result } = setup();
    const blocks = [{ kind: "cover" }, { kind: "heading", section: 0 }];

    act(() => result.current.request({ blocks }));
    await tick(300);

    expect(mocked.design).toHaveBeenCalledWith("r-1", "rep-1", { blocks });
  });

  it("surfaces a failed render and clears pending", async () => {
    // The sheet path never calls `rerenderReport`; a failure here is the preview
    // fetch refusing (a 409 for a report with no stored payload, say). Armed
    // after the mount's draw, so the rejection belongs to the edit under test.
    const { result } = setup();
    await tick();
    mocked.preview.mockRejectedValueOnce(new Error("boom"));

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);

    expect(result.current.error).not.toBeNull();
    expect(result.current.pending).toBe(false);
  });

  // --- the two views ---------------------------------------------------------

  it("edits on the sheet without printing anything", async () => {
    // The whole saving of 6b: no rerender, no download, no artifact written.
    const { result } = setup();

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);

    expect(mocked.design).toHaveBeenCalledTimes(1);
    expect(mocked.preview).toHaveBeenCalledWith("r-1", "rep-1");
    expect(mocked.rerender).not.toHaveBeenCalled();
    expect(mocked.download).not.toHaveBeenCalled();
    expect(result.current.html).toContain("cv-sheet");
  });

  it("opens on the sheet, so a reader who never asks for a PDF never prints one", () => {
    const { result } = setup();

    expect(result.current.view).toBe("sheet");
  });

  it("draws the sheet as soon as the editor opens, before any edit", async () => {
    // e2e found this: the canvas was blank until you changed something. Waiting
    // was right when a preview cost a print; the sheet costs none, and an editor
    // that opens empty is this card's whole point missed.
    const { result } = setup();
    await tick();

    expect(mocked.preview).toHaveBeenCalledTimes(1);
    expect(mocked.design).not.toHaveBeenCalled();
    expect(result.current.html).toContain("cv-sheet");
    expect(result.current.pending).toBe(false);
  });

  it("does not print on mount — only the sheet is free", async () => {
    const { result } = setup();
    await tick();

    expect(mocked.rerender).not.toHaveBeenCalled();
    expect(result.current.url).toBeNull();
  });

  it("prints only when the PDF is actually asked for", async () => {
    const { result } = setup();

    act(() => result.current.setView("pdf"));
    await tick();

    expect(mocked.rerender).toHaveBeenCalledTimes(1);
    expect(result.current.url).not.toBeNull();
    // The stored design is already what the reader wants — a switch is not an
    // edit, and re-PUTting it would be a write with nothing to say.
    expect(mocked.design).not.toHaveBeenCalled();
  });

  it("switching back to a still-current sheet costs nothing", async () => {
    const { result } = setup();
    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    act(() => result.current.setView("pdf"));
    await tick();
    mocked.preview.mockClear();

    act(() => result.current.setView("sheet"));
    await tick();

    expect(mocked.preview).not.toHaveBeenCalled();
    expect(result.current.view).toBe("sheet");
  });

  it("an edit made on the sheet invalidates the PDF, so the switch cannot show a stale one", async () => {
    // Only the shown view is rebuilt. Without this the stored PDF describes a
    // design nobody chose, and the switch would present it as the artifact of
    // record.
    const { result } = setup();
    act(() => result.current.setView("pdf"));
    await tick();
    act(() => result.current.setView("sheet"));
    await tick();
    mocked.rerender.mockClear();

    act(() => result.current.request({ blocks: [{ kind: "cover" }] }));
    await tick(300);
    act(() => result.current.setView("pdf"));
    await tick();

    expect(mocked.rerender).toHaveBeenCalledTimes(1);
  });

  it("an edit made on the PDF invalidates the sheet", async () => {
    const { result } = setup();
    act(() => result.current.setView("pdf"));
    await tick();
    mocked.preview.mockClear();

    act(() => result.current.request({ template: "chasopys" }));
    await tick(300);
    act(() => result.current.setView("sheet"));
    await tick();

    expect(mocked.preview).toHaveBeenCalledTimes(1);
  });

  it("switching to the view already shown does nothing at all", async () => {
    const { result } = setup();
    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    mocked.preview.mockClear();

    act(() => result.current.setView("sheet"));
    await tick();

    expect(mocked.preview).not.toHaveBeenCalled();
  });

  it("a switch during a render publishes the view the reader ended on", async () => {
    // `render` reads the view through a ref for this reason: a closure would
    // publish the artifact the reader had just navigated away from.
    const held = deferred<string>();
    mocked.preview.mockReturnValueOnce(held.promise);
    const { result } = setup();

    act(() => result.current.request({ template: "kvarta" }));
    await tick(300);
    act(() => result.current.setView("pdf"));
    held.release('<div class="cv-sheet">stale</div>');
    await tick();

    expect(result.current.view).toBe("pdf");
    expect(result.current.url).not.toBeNull();
  });
});
