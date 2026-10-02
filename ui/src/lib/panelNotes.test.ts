import { describe, expect, it } from "vitest";
import type { YContext } from "./api";
import { caveatText, contextNotes, describeShown, panelNotes } from "./panelNotes";

describe("panelNotes", () => {
  it("turns caveat keys into sentences and keeps unknown keys visible", () => {
    expect(caveatText("low_count", 200)).toContain("fewer than 200");
    expect(caveatText("something_new")).toBe("something_new");
  });
  it("adds the y-scale note as info, after the caveats", () => {
    const notes = panelNotes(["gaps"], { yScaledToData: true, nMin: null });
    expect(notes.map((n) => [n.kind, n.key])).toEqual([
      ["caveat", "gaps"],
      ["info", "y_scaled_to_data"],
    ]);
  });
  it("has no notes when there is nothing to warn about", () => {
    expect(panelNotes([], { yScaledToData: false, nMin: null })).toEqual([]);
  });
});

describe("contextNotes (2as.10)", () => {
  const ctx = (o: Partial<YContext> = {}): YContext => ({
    natural_lo: null, natural_hi: null, bounds: null, bounds_origin: null, limit: null, profile: null, notes: [], ...o,
  });
  it("says what the reference range includes", () => {
    const [n] = contextNotes(ctx({ limit: { metric: "size_bytes", dataset: "d2", hi: 100, basis: "bounded_by" }, profile: { lo: 1, hi: 5, label: "normal range (30d)" } }));
    expect(n.key).toBe("y_reference");
    expect(n.text).toMatch(/normal range \(30d\).*physical limit size_bytes/);
  });
  it("reports what could not be applied, in plain words", () => {
    const notes = contextNotes(ctx({ notes: ["profile_pending: still computing", "limit_unavailable: size could not be fetched (boom)", "profile_unavailable: a histogram has no single operating range"] }));
    expect(notes.map((n) => n.key)).toEqual(["profile_pending", "limit_unavailable", "profile_unavailable"]);
    expect(notes[1].kind).toBe("caveat");
    expect(notes[2].text).toMatch(/histogram has no single operating range/);
  });
  it("stays quiet for derived expressions and for no context", () => {
    expect(contextNotes(ctx({ notes: ["natural_bounds_unknown: the expression is not a single metric"] }))).toEqual([]);
    expect(contextNotes(null)).toEqual([]);
  });
  it("replaces the no-reference note once a reference exists (the panel sets yScaledToData off)", () => {
    const notes = panelNotes([], { yScaledToData: false, nMin: null, yContext: ctx({ profile: { lo: 0, hi: 9, label: "normal" } }) });
    expect(notes.map((n) => n.key)).toEqual(["y_reference"]);
  });
});

describe("describeShown", () => {
  it("names the quantile and says it is never aggregated", () => {
    expect(describeShown({ representation: "quantile", quantile: 0.95 }, "1m")).toMatch(/^p95 per 1m window.*never aggregated/);
  });
  it("describes the average-with-envelope default", () => {
    expect(describeShown({ representation: "bucket_agg" }, "30s")).toContain("min–max envelope");
  });
});

describe("distribution notes", () => {
it("explains distribution caveats in plain words", () => {
  const notes = panelNotes(["gaps", "low_count", "estimated_counts", "overflow"], { yScaledToData: false, nMin: 20, representation: "distribution" });
  expect(notes.map((n) => n.key)).toEqual(["gaps", "low_count", "estimated_counts", "overflow"]);
  expect(notes[0].text).toContain("hatched");
  expect(notes[1].text).toContain("fewer than 20 observations");
  expect(notes[2].text).toContain("extrapolat");
  expect(notes[3].text).toContain("largest bucket");
});
it("describes a distribution panel", () => {
  expect(describeShown({ representation: "distribution", quantile: null, scheme: { kind: "classic", edges: [0.1, 1], schema: null, per_decade: null, description: "classic le buckets: 0.1, 1" } }, "1m"))
    .toContain("classic le buckets: 0.1, 1");
});
});

describe("y view notes", () => {
  it("a selected y-view replaces the scaled-to-data note and carries Claude's reason", () => {
    const notes = panelNotes([], {
      yScaledToData: true, nMin: 200,
      yView: { label: "meaningful", reason: "one n=13 bucket squashes the rest", author: "claude", refused: null },
    });
    expect(notes.map((n) => n.key)).toEqual(["y_view"]);
    expect(notes[0].text).toBe('Y view "meaningful" (suggested by Claude): one n=13 bucket squashes the rest');
  });
  it("a refused view is a caveat", () => {
    const notes = panelNotes([], { yScaledToData: true, nMin: null,
      yView: { label: "log", reason: null, author: "user", refused: "log: log needs every value > 0 (min 0)" } });
    expect(notes.map((n) => [n.kind, n.key])).toEqual([["caveat", "y_view_refused"], ["info", "y_scaled_to_data"]]);
  });
});

describe("marginal notes", () => {
  it("an active marginal always states its basis, n and who chose it", () => {
    const notes = panelNotes([], { yScaledToData: false, nMin: null,
      marginal: { what: "per-step values (1m means of scrape samples): scrape samples, not requests",
                  ref: "previous window", n: [122, 7], nMin: 20, author: "claude", reason: "did it shift?" } });
    expect(notes.map((n) => [n.kind, n.key])).toEqual([["info", "marginal"], ["caveat", "marginal_low_n"]]);
    expect(notes[0].text).toBe("Marginal (right): per-step values (1m means of scrape samples): scrape samples, not requests. Filled = now (n=122), dashed = previous window (n=7). Chosen by Claude: did it shift?");
  });
});

describe("quantile estimator provenance", () => {
  it("names the estimator behind a percentile panel", () => {
    expect(describeShown({ representation: "quantile", quantile: 0.95, histogram: { selector: "x", by: [] } }, "1m")).toContain("histogram_quantile, linear interpolation");
    expect(describeShown({ representation: "quantile", quantile: 0.95 }, "1m")).toContain("as computed by the source");
  });
});

describe("auto-charted panels (2as.14)", () => {
  it("say the rate is drawn, why, and how to get the running total", () => {
    const notes = panelNotes([], { yScaledToData: false, nMin: null, auto: { transform: "rate", source_dataset: "d3", reason: "m is a counter (a running total); its per-second rate is drawn" } });
    expect(notes.map((n) => n.key)).toEqual(["auto_rate"]);
    expect(notes[0].text).toMatch(/Shown as a rate: m is a counter/);
    expect(notes[0].text).toMatch(/dataset d3/);
    expect(notes[0].text).toMatch(/raw=true/);
  });
  it("no note for ordinary panels", () => {
    expect(panelNotes([], { yScaledToData: false, nMin: null, auto: null })).toEqual([]);
  });
});
