import { describe, expect, it } from "vitest";
import { seriesLabels, shortNamer } from "./seriesNames";

describe("seriesLabels", () => {
  it("drops constant labels and shows the lone varying value", () => {
    expect(seriesLabels([{ job: "x", i: "a" }, { job: "x", i: "b" }])).toEqual(["a", "b"]);
  });
  it("drops a shared __name__ but keeps a varying one", () => {
    expect(seriesLabels([{ __name__: "m", i: "a" }, { __name__: "m", i: "b" }])).toEqual(["a", "b"]);
    expect(seriesLabels([{ __name__: "m1" }, { __name__: "m2" }])).toEqual(["m1", "m2"]);
  });
  it("joins several varying keys sorted by key", () => {
    expect(seriesLabels([{ z: "1", a: "x" }, { z: "2", a: "y" }])).toEqual(["a=x, z=1", "a=y, z=2"]);
  });
  it("single series: name, then full list, then total", () => {
    expect(seriesLabels([{ __name__: "m", i: "a" }])).toEqual(["m"]);
    expect(seriesLabels([{ i: "a", j: "b" }])).toEqual(["i=a, j=b"]);
    expect(seriesLabels([{}])).toEqual(["total"]);
  });
  it("disambiguates identical short names", () => {
    expect(seriesLabels([{ i: "a" }, { i: "a" }])).toEqual(["i=a", "i=a #2"]);
    expect(seriesLabels([{ i: "a", j: "1" }, { i: "b", j: "1" }, { i: "b", j: "1" }])).toEqual(["a", "b", "b #3"]);
  });
  it("renders a missing key as an em dash", () => {
    expect(seriesLabels([{ i: "a" }, {}])).toEqual(["a", "—"]);
    expect(seriesLabels([{ i: "a", j: "1" }, { j: "2" }])).toEqual(["i=a, j=1", "i=—, j=2"]);
  });
  it("is deterministic", () => {
    const all = [{ b: "1", a: "2" }, { b: "2", a: "2" }];
    expect(seriesLabels(all)).toEqual(seriesLabels(all));
  });
});

describe("shortNamer", () => {
  it("looks up by labels and falls back to the full selector", () => {
    const n = shortNamer([{ i: "a" }, { i: "b" }]);
    expect(n({ i: "b" })).toBe("b");
    expect(n({ i: "zzz" })).toBe('{i="zzz"}');
  });
});
