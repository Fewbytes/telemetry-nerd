import { describe, expect, it } from "vitest";
import type { CatalogRow } from "./api";
import { PAGE, activeFilters, catalogParams, defaults, pageInfo, rowBadges } from "./catalogQuery";

const row = (o: Partial<CatalogRow> = {}): CatalogRow => ({
  metric: "m", present: true, type: null, unit: null, role: null, bounds: null, origins: {}, confidences: {},
  conflicts: [], findings: [], verdict: null, reviewed: false, ...o,
});

describe("catalogParams", () => {
  it("sends only the source and the page by default", () => {
    expect(catalogParams(defaults("vm")).toString()).toBe(`source=vm&limit=${PAGE}&offset=0`);
  });
  it("carries each filter, trimmed, and the page as an offset", () => {
    const p = catalogParams({ ...defaults("vm"), q: " cpu ", prefix: "node_", origin: "pack", weak: true, conflicts: true, findings: true, reviewed: "no", removed: true, sort: "weakest", page: 3 });
    expect(Object.fromEntries(p)).toEqual({
      source: "vm", limit: "50", offset: "150", q: "cpu", prefix: "node_", origin: "pack", max_confidence: "0.6",
      conflicts: "1", findings: "1", reviewed: "no", removed: "1", sort: "weakest",
    });
  });
  it("leaves blank search out", () => {
    expect(catalogParams({ ...defaults("vm"), q: "   " }).has("q")).toBe(false);
  });
});

describe("activeFilters", () => {
  it("counts what narrows the list, not the sort or page", () => {
    expect(activeFilters(defaults("vm"))).toBe(0);
    expect(activeFilters({ ...defaults("vm"), q: "x", conflicts: true, sort: "weakest", page: 2 })).toBe(2);
  });
});

describe("pageInfo", () => {
  it("reports the visible range and neighbours", () => {
    expect(pageInfo(0, 0)).toEqual({ from: 0, to: 0, pages: 1, prev: false, next: false });
    expect(pageInfo(120, 0)).toMatchObject({ from: 1, to: 50, pages: 3, prev: false, next: true });
    expect(pageInfo(120, 2)).toMatchObject({ from: 101, to: 120, prev: true, next: false });
    expect(pageInfo(50, 0)).toMatchObject({ pages: 1, next: false });
  });
});

describe("rowBadges", () => {
  it("is quiet for an undisputed row", () => {
    expect(rowBadges(row())).toEqual([]);
  });
  it("flags conflicts, findings, removal and a scan verdict", () => {
    const b = rowBadges(row({ present: false, conflicts: ["unit", "type"], findings: [{ kind: "gauge_grows", id: "f1" }], verdict: "counter-like" }));
    expect(b.map((x) => x.key)).toEqual(["removed", "conflicts", "finding-gauge_grows", "verdict"]);
    expect(b[1].text).toBe("conflict: unit, type");
    expect(b[2].text).toBe("gauge grows");
    expect(b[1].tone).toBe("warn");
  });
  it("does not report a scan that learned nothing", () => {
    expect(rowBadges(row({ verdict: "inconclusive" }))).toEqual([]);
    expect(rowBadges(row({ verdict: "no data" }))).toEqual([]);
  });
});
