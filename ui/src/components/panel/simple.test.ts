import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import SimpleMeta from "./SimpleMeta.svelte";
import SimpleToolbar from "./SimpleToolbar.svelte";
import type { PanelData } from "../../lib/api";
import type { ControlProps } from "./controls";

// Server-side render (no DOM in this vitest environment): enough to pin what each component shows
// for a given payload. Interaction (select/click/Esc) is covered by e2e/simple-toolbar.spec.ts.

const html = (c: unknown, props: object) => render(c as never, { props: props as never }).body.replace(/<!--.*?-->/g, "");

const overlays = (normal: boolean) => ({
  flags: { normal, limit: false, ghost: false },
  normal: { available: true, label: "normal", stale: false },
  limit: { available: false, lines: [] },
  ghost: { available: false },
});

const time = (extra: object = {}) =>
  ({
    kind: "time",
    series: [{ lo: [1] }],
    effective_step_ms: 60_000,
    dataset: {
      start_ms: Date.UTC(2026, 0, 1, 10, 0), end_ms: Date.UTC(2026, 0, 1, 12, 0), source: "s",
      uncertainty: { exact: false, kind: "confidence", level: 0.95, method: "bootstrap" },
    },
    ...extra,
  }) as unknown as PanelData;

describe("SimpleMeta", () => {
  const props = (data: PanelData) => ({ data, heatView: "heatmap", fleetView: "lines" });

  it("shows what is shown, the band legend, then range and step", () => {
    const out = html(SimpleMeta, props(time()));
    expect(out).toContain("data-simple-what");
    expect(out).toContain("band: declared 95% confidence interval (bootstrap)");
    expect(out).toMatch(/data-simple-meta[^>]*>[\s\S]*–[\s\S]*step 1m/);
    expect(out.indexOf("data-simple-what")).toBeLessThan(out.indexOf("data-interval-legend"));
    expect(out.indexOf("data-interval-legend")).toBeLessThan(out.indexOf("data-simple-meta"));
  });

  it("has no band legend without bands or for an exact dataset", () => {
    expect(html(SimpleMeta, props(time({ series: [{}] })))).not.toContain("data-interval-legend");
    const exact = time();
    (exact.dataset as unknown as { uncertainty: object }).uncertainty = { exact: true };
    expect(html(SimpleMeta, props(exact))).not.toContain("data-interval-legend");
  });

  it("states no provenance or query: those are nerd-only", () => {
    const out = html(SimpleMeta, props(time()));
    expect(out).not.toMatch(/query|provenance|dataset/i);
  });
});

describe("SimpleToolbar", () => {
  const base = (data: PanelData | null, over: Partial<ControlProps> = {}): ControlProps => ({
    data, panel: { spec: { y: {} } } as never, yst: null, yctx: null, yIsValues: false, chosen: null, bandPick: false,
    idxBusy: false, overlayBusy: false, rangeBusy: false, rangeError: null, viewport: null, previewRender: null,
    viewNow: null, previewEl: null, toggleOverlay: vi.fn(async () => {}), pick: vi.fn(), pickIndexed: vi.fn(),
    pickView: vi.fn(), pickPreset: vi.fn(), keepThisRange: vi.fn(), ...over,
  });
  const yst = { all: { lo: 1, hi: 5 }, meaningful: null, quantile: false, fadedCount: 0, values: [1, 5] };

  it("renders nothing without data, or for a non-time panel with no y-view offers", () => {
    expect(html(SimpleToolbar, base(null))).not.toContain("data-simple-toolbar");
    expect(html(SimpleToolbar, base({ kind: "heatmap" } as unknown as PanelData))).not.toContain("data-simple-toolbar");
  });

  it("time panel: range presets, disabled while a range request runs", () => {
    const ready = html(SimpleToolbar, base(time()));
    expect(ready).toContain('aria-label="Time range"');
    expect(ready).toContain(">6h<");
    expect(ready).not.toMatch(/<button[^>]*disabled[^>]*>6h/);
    expect(html(SimpleToolbar, base(time(), { rangeBusy: true }))).toMatch(/<button[^>]*disabled[^>]*>6h/);
  });

  it("y-view dropdown: offers, Claude's suggestions in their own group, band hint while picking", () => {
    const panel = { spec: { y: { views: [{ id: "v1", label: "fit the wiggles", mode: "data" }] } } } as never;
    const out = html(SimpleToolbar, base(time(), { yst, yIsValues: true, panel }));
    expect(out).toContain('aria-label="Y-axis view"');
    expect(out).toContain(">from zero<");
    expect(out).toContain('<optgroup label="Claude suggests">');
    expect(out).toContain("fit the wiggles");
    expect(out).not.toContain("Esc cancels");
    const picking = html(SimpleToolbar, base(time(), { yst, yIsValues: true, panel, bandPick: true }));
    expect(picking).toContain("drag vertically on the plot · Esc cancels");
    expect(picking).not.toContain("band… again");
  });

  it("a committed band offers picking again; a pick in progress or another view does not", () => {
    const band = { mode: "band", lo: 1, hi: 2 } as never;
    const withBand = html(SimpleToolbar, base(time(), { yst, yIsValues: true, chosen: band }));
    expect(withBand).toContain("band… again");
    expect(html(SimpleToolbar, base(time(), { yst, yIsValues: true, chosen: { mode: "data" } as never }))).not.toContain("band… again");
  });

  it("layers: one disclosure counting the layers that are on", () => {
    const off = html(SimpleToolbar, base(time({ overlays: overlays(false) })));
    expect(off).toContain("Layers</summary>");
    const on = html(SimpleToolbar, base(time({ overlays: overlays(true) })));
    expect(on).toContain("Layers (1)");
    expect(on).toMatch(/<input type="checkbox"[^>]*checked/);
    expect(html(SimpleToolbar, base(time()))).not.toContain("Reference layers");
  });

  it("data filter: a Data view dropdown marking Claude's pick, with the filter-edge caveat", () => {
    const filter = { offered: ["overlay", "raw"], default: "overlay", filter: "lowpass 15m", reason: "the trend" };
    const out = html(SimpleToolbar, base(time({ filter }), { viewNow: "raw" }));
    expect(out).toContain('aria-label="Data view"');
    expect(out).toContain("filtered over raw · Claude");
    expect(out).toContain("lowpass 15m; dashed: filter edge (unreliable)");
  });
});
