import { describe, expect, it } from "vitest";
import { parseAnswer } from "./parseAnswer";

describe("parseAnswer", () => {
  it("splits text around known citation markers", () => {
    const segments = parseAnswer("Retention is 30 days [1] by policy.", new Set([1]));
    expect(segments).toEqual([
      { type: "text", value: "Retention is 30 days " },
      { type: "cite", number: 1 },
      { type: "text", value: " by policy." },
    ]);
  });

  it("leaves unknown markers as literal text", () => {
    const segments = parseAnswer("See [7] for details.", new Set([1]));
    expect(segments).toEqual([{ type: "text", value: "See [7] for details." }]);
  });

  it("handles adjacent markers with no text between", () => {
    const segments = parseAnswer("Facts [1][2].", new Set([1, 2]));
    expect(segments).toEqual([
      { type: "text", value: "Facts " },
      { type: "cite", number: 1 },
      { type: "cite", number: 2 },
      { type: "text", value: "." },
    ]);
  });

  it("returns a single text segment when there are no citations", () => {
    const segments = parseAnswer("Nothing to cite.", new Set());
    expect(segments).toEqual([{ type: "text", value: "Nothing to cite." }]);
  });
});
