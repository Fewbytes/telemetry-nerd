import { describe, expect, it } from "vitest";
import type { ContextLine, LineData, OverlaysPayload, YContext } from "../lib/api";
import { panelNotes } from "../lib/panelNotes";
import { limitLabel, lineTitle, overlayChips, overlayDraw } from "./overlays";
import { LIMIT_COLOR, THRESHOLD_COLORS, REFERENCE_COLOR, lineStyle, toUplot } from "./toUplot";
import { badgeText, refExtent, resolveY, yStats } from "./yview";

const ts = [1000, 2000, 3000];
const series = (v: number) => [{ id: "x", labels: { __name__: "m" }, ts, avg: [v, v, v], min: [v, v, v], max: [v, v, v], count: [1, 1, 1] }];
const limit: LineData = { kind: "limit", metric: "mem_total", label: "memory limit", hi: 100, origin: "pack", confidence: 0.85, basis: "pack node_exporter@1: docs", series: series(100) };
const warn: LineData = { kind: "threshold", metric: "crit", label: "sensor max", hi: 80, tone: "warn", origin: "pack", confidence: 0.85, basis: "pack node_exporter@1: docs", series: series(80) };
const slo: LineData = { kind: "threshold", metric: "SLO p99", label: "SLO p99", hi: 0.25, value: 0.25, tone: "bad", origin: "user", confidence: 1, basis: "user claim" };
const ref: LineData = { kind: "reference", metric: "client_latency", hi: 0.3, origin: "claude", confidence: 0.7, basis: "same quantity, client side", series: series(0.3) };

const payload = (lines: LineData[]): OverlaysPayload => ({
  flags: { normal: false, limit: true, ghost: false },
  normal: { available: false, reason: "none" },
  limit: { available: true, label: "limit mem_total", metric: "mem_total", hi: 100, lines },
  ghost: { available: true, loaded: false },
});

describe("context line chips and provenance", () => {
  it("a lone limit keeps the familiar name; a mix says what it holds", () => {
    expect(limitLabel([limit])).toBe("limit line");
    expect(limitLabel([limit, warn, ref])).toBe("limit + threshold + reference (3)");
    expect(limitLabel(undefined)).toBe("limit line");
  });
  it("every line's title says who, how sure, and why", () => {
    expect(lineTitle(limit)).toBe("memory limit (limit): origin: pack (confidence 0.85); pack node_exporter@1: docs");
    expect(lineTitle(slo)).toBe("SLO p99 (threshold): origin: user (confidence 1.00); user claim");
    expect(lineTitle({ ...ref, origin: null, confidence: null })).toMatch(/origin: unknown/);
  });
  it("the chip title lists all lines", () => {
    const chip = overlayChips(payload([limit, warn])).find((c) => c.key === "limit")!;
    expect(chip.title.split("\n")).toHaveLength(2);
    expect(chip.title).toMatch(/sensor max \(threshold\): origin: pack/);
  });
});

describe("drawing context lines", () => {
  it("limits, thresholds and references are styled apart, never as series colours", () => {
    expect(lineStyle({ kind: "limit" }).stroke).toBe(LIMIT_COLOR);
    expect(lineStyle({ kind: "threshold", tone: "bad" }).stroke).toBe(THRESHOLD_COLORS.bad);
    expect(lineStyle({ kind: "threshold", tone: "warn" }).stroke).toBe(THRESHOLD_COLORS.warn);
    expect(lineStyle({ kind: "threshold" }).stroke).toBe(THRESHOLD_COLORS.info);
    expect(lineStyle({ kind: "reference" })).toMatchObject({ stroke: REFERENCE_COLOR, width: 1 });
    expect(new Set([LIMIT_COLOR, ...Object.values(THRESHOLD_COLORS), REFERENCE_COLOR]).size).toBe(5);
  });
  it("overlayDraw hands over lines when present, the legacy limit series otherwise", () => {
    expect(overlayDraw(payload([limit]))?.lines).toHaveLength(1);
    const old = payload([]);
    old.limit.lines = undefined;
    old.limit.series = series(9);
    const draw = overlayDraw(old)!;
    expect(draw.lines).toBeUndefined();
    expect(draw.limit).toHaveLength(1);
  });
  it("toUplot adds a dashed column per line; a constant fills the whole grid", () => {
    const m = toUplot([{ id: "a", labels: { i: "a" }, ts, avg: [1, 2, 3], min: [1, 2, 3], max: [1, 2, 3], count: [4, 4, 4] }], undefined, {
      overlays: { lines: [limit, slo] },
    });
    const labels = m.series.map((s) => s.label);
    expect(labels).toContain("memory limit");
    expect(labels).toContain("SLO p99");
    const i = labels.indexOf("SLO p99");
    expect(m.data[i]).toEqual([0.25, 0.25, 0.25]);
    expect(m.series[i].dash).toBeDefined();
    expect(m.legendHidden).toContain(i);
  });
});

const ctx = (lines: ContextLine[], limitLine: ContextLine | null = lines.find((l) => l.kind === "limit") ?? null): YContext => ({
  natural_lo: 0, natural_hi: null, bounds: "≥0", bounds_origin: "pack", limit: limitLine, lines, profile: null, notes: [],
});

describe("y range and notes with context lines", () => {
  it("every hard limit joins the reference range; a threshold never stretches it", () => {
    const c = ctx([limit, { ...limit, metric: "other", hi: 150 }, { ...warn, hi: 1e6 }]);
    expect(refExtent({ lo: 0, hi: 10 }, c).hi).toBe(150);
    expect(refExtent({ lo: 0, hi: 10 }, ctx([warn], null)).hi).toBe(10);
  });
  it("the badge names each limit it includes", () => {
    const c = ctx([limit, { ...limit, metric: "other", hi: 150 }]);
    const st = yStats([{ id: "a", labels: {}, ts, avg: [1, 2, 3], min: [1, 2, 3], max: [1, 2, 3], count: [4, 4, 4] }], { quantile: false, nMin: null });
    const r = resolveY({ mode: "reference", label: "reference" }, st, c);
    const text = badgeText({ mode: "reference", label: "reference" }, r, null, undefined, c);
    expect(text).toMatch(/limit mem_total 100/);
    expect(text).toMatch(/limit other 150/);
  });
  it("a note per line carries its provenance, empirical ones included", () => {
    const notes = panelNotes([], { yContext: ctx([limit, warn, slo]) } as never).filter((n) => n.key.startsWith("context_line"));
    expect(notes.map((n) => n.text)).toEqual([
      "memory limit (limit) at 100: origin: pack (confidence 0.85); pack node_exporter@1: docs.",
      "sensor max (threshold) at 80: origin: pack (confidence 0.85); pack node_exporter@1: docs.",
      "SLO p99 (threshold) at 0.25: origin: user (confidence 1.00); user claim.",
    ]);
  });
  it("a reframed panel carries a caveat saying so", () => {
    const n = panelNotes([], { auto: { transform: "reframe", source_dataset: "d1", reason: "reframed from p1: show available memory instead" } } as never);
    expect(n.find((x) => x.key === "auto_reframe")).toMatchObject({ kind: "caveat" });
    expect(n.find((x) => x.key === "auto_reframe")!.text).toMatch(/not the metric as asked.*d1/);
  });
});
