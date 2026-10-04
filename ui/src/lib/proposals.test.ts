import { describe, expect, it } from "vitest";
import { evidenceRefs, expiresText, nextFocus, parseEdit, scopeText, valueText } from "./proposals";

describe("scopeText", () => {
  it("names source first, then service, family and sorted labels", () => {
    expect(scopeText({ source: "prom" })).toBe("source prom");
    expect(scopeText({ source: "prom", service: "checkout", metric_family: "http_*", labels: { region: "eu", namespace: "prod" } }))
      .toBe("source prom · service checkout · metrics http_* · namespace=prod · region=eu");
  });
});

describe("evidenceRefs", () => {
  it("links evidence in the active workspace and names the workspace otherwise", () => {
    expect(evidenceRefs(["f3", "p7", "f9", "p1"], { f3: "w2", p7: "w2", f9: "w1", p1: null }, "w2")).toEqual([
      { id: "f3", href: "#/finding/f3", note: null },
      { id: "p7", href: "#/panel/p7", note: null },
      { id: "f9", href: null, note: "in w1" },
      { id: "p1", href: null, note: "missing" },
    ]);
  });
});

describe("parseEdit", () => {
  it("keeps text values as trimmed text and reads others as JSON", () => {
    expect(parseEdit("s", " ms ")).toBe("ms");
    expect(parseEdit(["a"], '["a","b"]')).toEqual(["a", "b"]);
    expect(() => parseEdit([{ value: 1 }], "nope")).toThrow(/as JSON/);
  });
  it("valueText shows strings bare and the rest as JSON", () => {
    expect(valueText("s")).toBe("s");
    expect(valueText({ lo: 1 })).toBe('{"lo":1}');
  });
});

describe("expiresText", () => {
  const day = 86_400_000;
  it("counts days and says expired", () => {
    expect(expiresText(10 * day, 0)).toBe("expires in 10 days");
    expect(expiresText(day / 2, 0)).toBe("expires within a day");
    expect(expiresText(0, 5)).toBe("expired");
  });
});

describe("nextFocus", () => {
  it("moves to the next pending item, else the previous, else none", () => {
    expect(nextFocus(["a", "b", "c"], "b")).toBe("c");
    expect(nextFocus(["a", "b", "c"], "c")).toBe("b");
    expect(nextFocus(["a"], "a")).toBeNull();
    expect(nextFocus(["a"], "x")).toBeNull();
  });
});
