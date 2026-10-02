import { describe, expect, it } from "vitest";
import { focusRects, hasFocus, notesAt, unknownReasons } from "./focus";

describe("focus", () => {
  it("maps spans to clipped rects", () => {
    const toX = (ms: number) => ms / 1000;
    expect(focusRects({ spans: [[0, 60_000], [100_000, 400_000]] }, toX, 10, 300)).toEqual([{ x: 10, w: 50 }, { x: 100, w: 200 }]);
    expect(focusRects(null, toX, 0, 100)).toEqual([]);
  });
  it("finds notes covering a series and time", () => {
    const notes = [
      { kind: "caveat" as const, key: "missing_data:0", text: "", where: { spans: [[0, 60_000]] as [number, number][], series: ["a"] } },
      { kind: "caveat" as const, key: "untrusted_data:1", text: "", where: { spans: [[0, 60_000]] as [number, number][], series: null } },
      { kind: "caveat" as const, key: "settling", text: "" },
    ];
    expect(notesAt(notes, "a", 30_000)).toEqual(["missing_data:0", "untrusted_data:1"]);
    expect(notesAt(notes, "b", 30_000)).toEqual(["untrusted_data:1"]);
  });
  it("only notes with spans can focus; reasons come from untrusted_data notes covering the bucket", () => {
    const notes = [
      { kind: "caveat" as const, key: "untrusted_data:0", text: "timeout", where: { spans: [[0, 60_000]] as [number, number][], series: null } },
      { kind: "caveat" as const, key: "missing_data:1", text: "no samples", where: { spans: [[0, 60_000]] as [number, number][], series: null } },
      { kind: "caveat" as const, key: "settling", text: "" },
      { kind: "caveat" as const, key: "x:2", text: "", where: { spans: [] as [number, number][] } },
    ];
    expect(notes.map(hasFocus)).toEqual([true, true, false, false]);
    expect(unknownReasons(notes, "a", 30_000)).toEqual(["timeout"]);
    expect(unknownReasons(notes, "a", 90_000)).toEqual([]);
  });
});
