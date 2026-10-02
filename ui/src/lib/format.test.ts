import { describe, expect, it } from "vitest";
import type { EvidenceRef, Scope, TimeSpan } from "./api";
import { flagLabel, fmtRange, fmtSI, fmtTime, refLabel, scopeLine, statLine, type StatisticRef } from "./format";

const span = (startMs: number, endMs: number): TimeSpan => ({ start_ms: startMs, end_ms: endMs });
const minutes = (h: number, m: number) => Date.UTC(2026, 8, 30, h, m);

const baseScope: Scope = {
  source: "prometheus",
  selector: "checkout, pod=~checkout-.*",
  time_range: span(minutes(14, 0), minutes(14, 30)),
  step: "30s",
  aggregation: "p99",
  baseline_range: null,
};

const statRef = (over: Partial<StatisticRef>): StatisticRef => ({
  kind: "statistic",
  dataset: "d1",
  name: "p99",
  value: 2.5,
  interval: [2.1, 3.2],
  exact: false,
  method: "bootstrap",
  params: {},
  ...over,
});

describe("fmtTime", () => {
  it("formats epoch ms as HH:MM UTC", () => {
    expect(fmtTime(minutes(14, 5))).toBe("14:05");
    expect(fmtTime(Date.UTC(2026, 8, 30, 23, 59))).toBe("23:59");
  });
});

describe("fmtRange", () => {
  it("omits the date within one UTC day", () => {
    expect(fmtRange(minutes(14, 0), minutes(14, 30))).toBe("14:00–14:30");
  });
  it("prefixes both ends with their date when crossing midnight", () => {
    expect(fmtRange(Date.UTC(2026, 8, 30, 23, 30), Date.UTC(2026, 9, 1, 0, 30)))
      .toBe("09-30 23:30–10-01 00:30");
  });
});

describe("scopeLine", () => {
  it("renders selector, UTC range, step and aggregation", () => {
    expect(scopeLine(baseScope)).toBe(
      "checkout, pod=~checkout-.* · 14:00–14:30 UTC · 30s step · p99",
    );
  });

  it("appends the baseline range when present", () => {
    const scope: Scope = {
      ...baseScope,
      baseline_range: span(minutes(13, 0), minutes(13, 30)),
    };
    expect(scopeLine(scope)).toBe(
      "checkout, pod=~checkout-.* · 14:00–14:30 UTC · 30s step · p99 · vs 13:00–13:30",
    );
  });
});

describe("statLine", () => {
  it("renders value with interval as [lo, hi] (method)", () => {
    expect(statLine(statRef({}))).toBe("p99 = 2.5 [2.1, 3.2] (bootstrap)");
  });

  it("renders exact statistics without an interval", () => {
    expect(
      statLine(statRef({ name: "n", value: 42, interval: null, exact: true, method: "count" })),
    ).toBe("n = 42 (exact, count)");
  });

  it("never renders a value without an interval as exact (spec §5.3)", () => {
    expect(statLine(statRef({ interval: null, exact: false, uncertainty_unknown: true }))).toBe(
      "p99 = 2.5 (uncertainty unknown, bootstrap)",
    );
  });
});

describe("flagLabel", () => {
  it("names the evidence uncertainty flags in words", () => {
    expect(flagLabel("uncertainty_unknown")).toBe("uncertainty unknown");
    expect(flagLabel("input_uncertainty_unknown")).toBe("input uncertainty unknown");
    expect(flagLabel("something_else")).toBe("something else");
  });
});

describe("fmtSI", () => {
  it("scales a big rate into the matching SI prefix (telemetry-nerd-klt)", () => {
    // sum(rate(node_network_transmit_bytes_total[5m])) at B/s: this is the panel p22 case
    expect(fmtSI(1_400_000_000, "B/s")).toBe("1.4 GB/s");
  });

  it("scales B, bit, Hz, W, J and count with k/M/G/T prefixes", () => {
    expect(fmtSI(2_500, "B")).toBe("2.5 kB");
    expect(fmtSI(3_200_000, "bit")).toBe("3.2 Mbit");
    expect(fmtSI(5_000_000_000_000, "Hz")).toBe("5 THz");
    expect(fmtSI(750, "W")).toBe("750 W");
  });

  it("leaves small values and unit-less numbers alone", () => {
    expect(fmtSI(42, "B/s")).toBe("42 B/s");
    expect(fmtSI(42, null)).toBe("42");
  });

  it("converts sub-second durations to ms, like the existing per-value formatter", () => {
    expect(fmtSI(0.25, "s")).toBe("250 ms");
    expect(fmtSI(90, "s")).toBe("90 s");
  });

  it("never scales percent, ratio or temperature", () => {
    expect(fmtSI(97, "%")).toBe("97%");
    expect(fmtSI(1_500_000, "ratio")).toBe("1500000 ratio");
    expect(fmtSI(5_000, "°C")).toBe("5000 °C");
  });
});

describe("refLabel", () => {
  it("labels panel and annotation refs by kind and id", () => {
    const panel: EvidenceRef = { kind: "panel", panel: "p3" };
    const annotation: EvidenceRef = { kind: "annotation", annotation: "a2" };
    expect(refLabel(panel)).toBe("panel p3");
    expect(refLabel(annotation)).toBe("annotation a2");
  });

  it("labels claim refs by the metric, the field and who disagrees", () => {
    const claim: EvidenceRef = { kind: "claim", source: "vm", metric: "queue_wait_seconds", field: "unit", origins: ["context", "metadata"] };
    expect(refLabel(claim)).toBe("queue_wait_seconds unit: context vs metadata disagree");
  });

  it("renders statistic refs through statLine", () => {
    expect(refLabel(statRef({}))).toBe("p99 = 2.5 [2.1, 3.2] (bootstrap)");
  });
});
