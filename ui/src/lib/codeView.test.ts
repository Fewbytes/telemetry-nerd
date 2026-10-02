import { describe, expect, it } from "vitest";
import type { CodeBrief } from "./api";
import { boundText, durationText, highlightPython, inputLinks, newestFirst, runSummary, statusView } from "./codeView";

const brief = (o: Partial<CodeBrief> = {}): CodeBrief => ({
  id: "c1", status: "ok", exec_status: "ok", inputs: ["d1"], outputs: ["d2"], author: "claude",
  created_at_ms: 0, finished_at_ms: 1, duration_s: 1.234, rerun_of: null, error: null, restarted: false, ...o,
});

describe("statusView", () => {
  it("pairs every status with a glyph and words", () => {
    expect(statusView(brief())).toEqual({ glyph: "✓", text: "ok", tone: "ok" });
    expect(statusView(brief({ status: "running", exec_status: null })).text).toBe("running");
    expect(statusView(brief({ status: "failed", exec_status: "timeout" }))).toMatchObject({ glyph: "✗", text: "failed: timed out", tone: "failed" });
    expect(statusView(brief({ status: "failed", exec_status: "weird" })).text).toBe("failed: weird");
    expect(statusView(brief({ status: "failed", exec_status: null })).text).toBe("failed");
  });
});

describe("durationText", () => {
  it("scales", () => {
    expect(durationText(null)).toBe("");
    expect(durationText(0.0456)).toBe("46 ms");
    expect(durationText(1.234)).toBe("1.23 s");
    expect(durationText(125)).toBe("2 min 5 s");
  });
});

describe("newestFirst / runSummary", () => {
  it("orders ids numerically", () => {
    expect(newestFirst([brief({ id: "c2" }), brief({ id: "c10" }), brief({ id: "c1" })]).map((c) => c.id)).toEqual(["c10", "c2", "c1"]);
  });
  it("summarises a run in one line", () => {
    expect(runSummary(brief())).toBe("✓ ok · 1.23 s · d1 → d2");
    expect(runSummary(brief({ status: "failed", exec_status: "error", inputs: [], outputs: [], duration_s: null }))).toBe("✗ failed: raised an exception");
  });
});

describe("boundText", () => {
  it("keeps the tail and says what was hidden", () => {
    const s = Array.from({ length: 10 }, (_, i) => `l${i}`).join("\n");
    expect(boundText(s, 3)).toEqual({ text: "l7\nl8\nl9", hiddenLines: 7 });
    expect(boundText("a\nb\n", 5)).toEqual({ text: "a\nb", hiddenLines: 0 });
  });
  it("bounds characters too", () => {
    const r = boundText("x".repeat(50) + "\n" + "y".repeat(50), 10, 60);
    expect(r.text).toBe("y".repeat(50));
    expect(r.hiddenLines).toBe(1);
  });
});

describe("inputLinks", () => {
  it("links an input to the open panel drawing it", () => {
    const panels = [
      { id: "p1", dataset_ids: ["d1"], closed: true },
      { id: "p2", dataset_ids: ["d1"], closed: false },
    ];
    expect(inputLinks({ inputs: ["d1", "d9"] }, panels)).toEqual([{ dataset: "d1", panel: "p2" }, { dataset: "d9", panel: null }]);
  });
});

describe("highlightPython", () => {
  const kinds = (src: string) => highlightPython(src).filter((t) => t.kind !== "plain").map((t) => [t.kind, t.text]);
  it("round-trips the source exactly", () => {
    const src = 'import numpy as np\n@deco.x\ndef f(x):\n    """doc # not comment"""\n    return x + 0x1F  # c\n\ns = f"a{1}" \'b\' "unterminated\n';
    expect(highlightPython(src).map((t) => t.text).join("")).toBe(src);
  });
  it("classifies keywords, builtins, strings, numbers, comments", () => {
    expect(kinds("for i in range(3): print('x') # hi")).toEqual([
      ["kw", "for"], ["kw", "in"], ["builtin", "range"], ["num", "3"], ["builtin", "print"], ["str", "'x'"], ["com", "# hi"],
    ]);
  });
  it("does not colour keywords inside strings or identifiers", () => {
    expect(kinds('x = "for if"\nformat = 1')).toEqual([["str", '"for if"'], ["num", "1"]]);
  });
  it("handles triple-quoted strings and decorators", () => {
    expect(kinds('@cache\n"""a\nb"""')).toEqual([["deco", "@cache"], ["str", '"""a\nb"""']]);
  });
});
