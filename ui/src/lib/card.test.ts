import { describe, expect, it } from "vitest";
import type { CardField, CardMetric, MetricCard } from "./api";
import { cardSummary, editControl, fmtDuration, fmtValue, isPinned, originLabel, qualityRows } from "./card";

const f = (field: string, value: unknown, o: Partial<CardField> = {}): CardField => ({
  field, editable: true, value, origin: "rule", confidence: 0.7, basis: null, conflict: false, claims: [], ...o,
});
const card = (fields: CardField[], extra: Partial<MetricCard> = {}): MetricCard => ({
  source: "vm", learned: true,
  metrics: [{ metric: "m", present: true, fields, relations: [], bindings: [], gaps: [] } as CardMetric],
  profile: { available: false }, quality: { step_ms: 60_000, resolution_ms: 15_000, scrape_interval_ms: 15_000, scrape_interval_reason: null, series: 2, gap_pct: 0.031, resets: { measured: false, reason: "needs sample statistics" }, cardinality: { in_panel: 2, catalog: null } },
  ...extra,
});

describe("editControl", () => {
  it("uses a select where the catalog accepts a fixed set, text otherwise, nothing when read-only", () => {
    expect(editControl(f("bounds", null))).toEqual({ kind: "select", options: ["≥0", "[0,1]", "[0,100]", "none"] });
    expect(editControl(f("type", null))).toMatchObject({ kind: "select" });
    expect(editControl(f("additivity_series", null))).toMatchObject({ kind: "select", options: ["additive", "intensive", "none"] });
    expect(editControl(f("unit", null))).toEqual({ kind: "text" });
    expect(editControl(f("description", null))).toEqual({ kind: "text" });
    expect(editControl(f("histogram_family", null, { editable: false }))).toBeNull();
  });
});

describe("cardSummary", () => {
  it("gives unit, type and bounds, and counts conflicts", () => {
    const c = card([f("unit", "s"), f("type", "counter"), f("bounds", "≥0", { conflict: true }), f("role", null)]);
    expect(cardSummary(c)).toBe("unit s · counter · ≥0 · 1 conflict");
  });
  it("says so when nothing is claimed or the source is unlearned", () => {
    expect(cardSummary(card([f("unit", null)]))).toBe("nothing claimed yet");
    expect(cardSummary(card([], { metrics: [], learned: false }))).toBe("this source has not been learned");
  });
});

describe("labels and formats", () => {
  it("names origins for people", () => {
    expect(originLabel("user")).toBe("you");
    expect(originLabel("metadata")).toBe("source");
    expect(originLabel("weird")).toBe("weird");
    expect(originLabel(null)).toBe("no claim");
  });
  it("formats values and durations", () => {
    expect(fmtValue(["a", "b"])).toBe("a, b");
    expect(fmtValue(null)).toBe("—");
    expect(fmtDuration(86_400_000 * 30)).toBe("30d");
    expect(fmtDuration(15_000)).toBe("15s");
    expect(fmtDuration(90_000)).toBe("90s");
  });
  it("a field is pinned once the user owns the winning claim", () => {
    expect(isPinned(f("unit", "s", { origin: "user" }))).toBe(true);
    expect(isPinned(f("unit", "s"))).toBe(false);
  });
});

describe("qualityRows", () => {
  it("shows what was measured and admits what was not", () => {
    const rows = Object.fromEntries(qualityRows(card([]).quality).map((r) => [r.label, r]));
    expect(rows["scrape interval"].value).toBe("15s");
    expect(rows["empty buckets"].value).toBe("3.1%");
    expect(rows["counter resets"]).toMatchObject({ value: "not measured" });
    expect(rows["cardinality (catalog)"].value).toBe("not measured");
  });
  it("reports measured resets from a scan", () => {
    const q = { ...card([]).quality, resets: { measured: true, window_ms: 1_800_000, series: 2, samples: 242, resets: 4, small_decreases: 0, negatives: 0, verdict: "counter-like", scanned_ms: 1 } };
    const r = qualityRows(q).find((x) => x.label === "counter resets")!;
    expect(r.value).toBe("4 resets, 0 small decreases");
    expect(r.note).toBe("counter-like over 30m, 2 series");
    const neg = qualityRows({ ...q, resets: { ...q.resets, negatives: 3 } }).find((x) => x.label === "counter resets")!;
    expect(neg.note).toMatch(/3 negative samples/);
  });
  it("explains an unknown scrape interval and flags one coarser than the step", () => {
    const q = card([]).quality;
    const unknown = qualityRows({ ...q, scrape_interval_ms: null, scrape_interval_reason: "fewer than 3 recent samples" }).find((r) => r.label === "scrape interval")!;
    expect(unknown).toMatchObject({ value: "unknown", note: "fewer than 3 recent samples" });
    const coarse = qualityRows({ ...q, scrape_interval_ms: 120_000 }).find((r) => r.label === "scrape interval")!;
    expect(coarse.note).toBe("coarser than the step");
    expect(qualityRows({ ...q, gap_pct: null }).find((r) => r.label === "empty buckets")!.value).toBe("unknown");
  });
});
