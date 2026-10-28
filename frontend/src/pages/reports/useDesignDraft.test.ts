import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDesignDraft } from "./useDesignDraft";

vi.mock("../../api/reports", async () => {
  const actual = await vi.importActual<typeof import("../../api/reports")>("../../api/reports");
  return { ...actual, getReportDesign: vi.fn(), setReportDesign: vi.fn() };
});

import { getReportDesign, setReportDesign } from "../../api/reports";

const SEED = {
  template: "kvarta",
  blocks_version: 1,
  blocks: [],
  stored: false,
  sections: [],
  title: null,
  typography: { scale: 1.1 },
  page: {},
  brand: {},
};

describe("useDesignDraft", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getReportDesign).mockResolvedValue(SEED as never);
    vi.mocked(setReportDesign).mockResolvedValue({} as never);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  const render = () =>
    renderHook(() => useDesignDraft({ repositoryId: "r1", reportId: "rep1", errorText: "boom" }));

  it("seeds template and style groups from the stored design", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());
    expect(result.current.template).toBe("kvarta");
    expect(result.current.style).toEqual({ typography: { scale: 1.1 }, page: {}, brand: {} });
  });

  it("saves a template choice without a blocks key", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());
    act(() => result.current.choose("prezydia"));
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    const patch = vi.mocked(setReportDesign).mock.calls.at(-1)?.[2];
    expect(patch).toEqual({ template: "prezydia" });
    expect(patch && "blocks" in patch).toBe(false);
    expect(result.current.template).toBe("prezydia");
  });

  it("collapses a burst of style edits into one save carrying the last value", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());
    act(() => result.current.restyle({ page: { margin_mm: 12 } }));
    act(() => result.current.restyle({ page: { margin_mm: 20 } }));
    // A later edit to a *different* group must not drop the earlier one: the
    // coalesced patch merges across groups, it does not replace wholesale.
    act(() => result.current.restyle({ brand: { accent: "amber" } }));
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(vi.mocked(setReportDesign)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(setReportDesign).mock.calls[0]?.[2]).toEqual({
      page: { margin_mm: 20 },
      brand: { accent: "amber" },
    });
  });

  it("reports a failed save without discarding what the reader chose", async () => {
    vi.mocked(setReportDesign).mockRejectedValue(new Error("no"));
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());
    act(() => result.current.choose("minimum"));
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.template).toBe("minimum");
  });

  it("never saves the seed it just loaded", async () => {
    render();
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(vi.mocked(setReportDesign)).not.toHaveBeenCalled();
  });

  it("ignores a stale response when two saves overlap and resolve out of order", async () => {
    // Each call gets its own deferred promise, so the test — not the mock's
    // default resolution order — controls which save answers first.
    const deferred: { resolve: () => void; reject: (err: Error) => void }[] = [];
    vi.mocked(setReportDesign).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          deferred.push({ resolve: () => resolve({} as never), reject });
        }),
    );

    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());

    // First save: debounce fires, the PUT goes out and is left hanging.
    act(() => result.current.choose("first"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Second save: a fresh debounce window fires while the first is still in
    // flight, so a second PUT goes out concurrently.
    act(() => result.current.choose("second"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(deferred).toHaveLength(2);

    // Resolve out of order: the newer save succeeds first, the stale older one
    // then rejects. A correct hook lets only the newer response speak.
    await act(async () => {
      deferred[1]?.resolve();
    });
    await act(async () => {
      deferred[0]?.reject(new Error("stale"));
    });

    expect(result.current.error).toBeNull();
  });

  it("keeps a newer failure on screen when an older, slower save then succeeds", async () => {
    // Mirror image of the test above, and the case the original finding named
    // as dangerous: the *newer* save is the one that fails, and the *older*,
    // slower save is the one that settles after it, with success. A stale
    // success must not clear the error the newer, real failure just set —
    // that would tell the reader their design saved when it did not.
    const deferred: { resolve: () => void; reject: (err: Error) => void }[] = [];
    vi.mocked(setReportDesign).mockImplementation(
      () =>
        new Promise((resolve, reject) => {
          deferred.push({ resolve: () => resolve({} as never), reject });
        }),
    );

    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());

    // First save: debounce fires, the PUT goes out and is left hanging.
    act(() => result.current.choose("first"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    // Second save: a fresh debounce window fires while the first is still in
    // flight, so a second PUT goes out concurrently.
    act(() => result.current.choose("second"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });

    expect(deferred).toHaveLength(2);

    // Settle out of order: the newer save (deferred[1]) fails first and sets
    // the error; the older, stale save (deferred[0]) then succeeds. Only the
    // newer response may speak — the stale success must be ignored.
    await act(async () => {
      deferred[1]?.reject(new Error("no"));
    });
    await act(async () => {
      deferred[0]?.resolve();
    });

    expect(result.current.error).toBe("boom");
  });

  it("flush sends a still-debounced choice immediately, before the timer would have", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());

    act(() => result.current.choose("prezydia"));
    // Deliberately no timer advance: this is the state a reader is in when they
    // pick a template and press Generate straight away. The debounce is still
    // ticking, and the component unmounts on approve — which clears the timer,
    // so without a flush the choice is not merely late, it is never sent.
    expect(setReportDesign).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.flush();
    });

    expect(setReportDesign).toHaveBeenCalledTimes(1);
    expect(vi.mocked(setReportDesign).mock.calls[0]?.[2]).toEqual({ template: "prezydia" });

    // And the timer it pre-empted must not fire a second, duplicate write.
    await act(async () => {
      vi.advanceTimersByTime(400);
    });
    expect(setReportDesign).toHaveBeenCalledTimes(1);
  });

  it("flush with nothing pending resolves without touching the network", async () => {
    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());

    await act(async () => {
      await result.current.flush();
    });

    expect(setReportDesign).not.toHaveBeenCalled();
  });

  it("flush waits for a save already on the wire, not only for a pending one", async () => {
    // The ordinary path, and the one a debounce-only flush leaves racing: the
    // reader picks a template, pauses longer than 300ms — so the timer has
    // already fired and nothing is pending — then presses Generate while that
    // PUT is still travelling. Resolving straight away would post /approve
    // alongside it, and the first artifact could be built before the design
    // reached the row.
    const deferred: { resolve: () => void }[] = [];
    vi.mocked(setReportDesign).mockImplementation(
      () =>
        new Promise((resolve) => {
          deferred.push({ resolve: () => resolve({} as never) });
        }) as never,
    );

    const { result } = render();
    await waitFor(() => expect(result.current.style).not.toBeNull());

    act(() => result.current.choose("prezydia"));
    await act(async () => {
      vi.advanceTimersByTime(300);
    });
    expect(deferred).toHaveLength(1);

    let settled = false;
    await act(async () => {
      void result.current.flush().then(() => {
        settled = true;
      });
    });
    expect(settled).toBe(false);

    await act(async () => {
      deferred[0]?.resolve();
    });
    expect(settled).toBe(true);
  });
});
