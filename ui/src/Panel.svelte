<script lang="ts">
  import { untrack } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    closePanel, fetchPanelData, refreshYContext, reframePanel, splitOutcome, reportRender, selectYView, setMarginal, selectDataView, setOverlays,
    type Annotation, type Panel, type PanelData, type Thread, type Where, type YView,
  } from "./lib/api";
  import { rgba, seriesName, toUplot } from "./chart/toUplot";
  import { drawRug, facetTop, hitRug, rugAxisExtra, rugCells, rugHeight, rugTop, rugHint, rugMoreLabel, type RugCell } from "./chart/rug";
  import { describeShown, intervalLegend, panelNotes, provenanceParts, provenanceText } from "./lib/panelNotes";
  import { getContext } from "svelte";
  import { windowBadge } from "./lib/coverage";
  import { focusRects, notesAt } from "./chart/focus";
  import { fmtSI, fmtStep } from "./lib/format";
  import { axisGutterSize } from "./chart/plotKit";
  import { setupCanvas } from "./chart/canvas";
  import { drawnFor, VIEW_LABELS, type DataViewName } from "./chart/dataview";
  import SpectrumPlot from "./components/SpectrumPlot.svelte";
  import SpectrogramPlot from "./components/SpectrogramPlot.svelte";
  import SpcPlot from "./components/SpcPlot.svelte";
  import LittlesPlot from "./components/LittlesPlot.svelte";
  import SeasonalPlot from "./components/SeasonalPlot.svelte";
  import FleetPlot from "./components/FleetPlot.svelte";
  import { fleetStats, fleetY } from "./chart/fleet";
  import { fmtRatio, indexSeries, ratioTicks } from "./chart/indexed";
  import { drawMarginal, marginalHeader } from "./chart/marginal";
  import { overlayChips, overlayDraw } from "./chart/overlays";
  import { badgeText, contextStrip, nonZeroOrigin, offeredViews, resolveY, stripExtent, yStats } from "./chart/yview";
  import { measureFirstDraw } from "./chart/measureDraw";
  import { drawAnnotations, drawOps, readAnnotationColors } from "./chart/annotations";
  import { plotColors, theme } from "./lib/theme.svelte";
  import SelectionMenu from "./components/SelectionMenu.svelte";
  import PanelThread from "./components/PanelThread.svelte";
  import PinButton from "./components/PinButton.svelte";
  import MetricCard from "./components/MetricCard.svelte";
  import HeatmapPlot from "./components/HeatmapPlot.svelte";
  import PercentilePlot from "./components/PercentilePlot.svelte";
  import CountStrip from "./components/CountStrip.svelte";
  import { colormap } from "./chart/colormap";
  import { colorMaxOf } from "./chart/heatmap";
  import { DEFAULT_QUANTILES, QUANTILE_CHOICES, MAX_QUANTILES, overlay, qLabel, toggleQuantile } from "./chart/percentiles";
  import DistributionPlot from "./components/DistributionPlot.svelte";

  let { panel, annotations = [], threads = [] }: {
    panel: Panel; annotations?: Annotation[]; threads?: Thread[];
  } = $props();

  // opens the read-only code view of the node that produced this panel's data (provided by App)
  const openCode = getContext<((id: string) => void) | undefined>("openCode");
  const panelAnns = $derived(
    annotations.filter((a) => !a.deleted && (a.panel === null || a.panel === panel.id)),
  );
  // content key: the plot effect rebuilds only when this changes — a new
  // annotations ARRAY from an unrelated panel/event must not tear down the
  // plot (it would clear an in-progress brush selection and detach the menu)
  const annKey = $derived(panelAnns.map((a) => `${a.id}:${a.deleted ? "d" : "v"}`).join("|"));
  const panelThreads = $derived(threads.filter((t) => t.anchor === panel.id));

  // Brush selection: x range in seconds (from uPlot scales) + px position over the plot.
  interface Selection { x0: number; x1: number; left: number; top: number; width: number }
  let selection = $state<Selection | null>(null);
  let annCount = $state(0);
  const RESIZE_MIN_PX = 8;
  let plot: uPlot | null = null;

  const cancelSelection = () => {
    selection = null;
    plot?.setSelect({ left: 0, top: 0, width: 0, height: 0 });
  };

  let plotEl = $state<HTMLDivElement | null>(null);
  let data = $state.raw<PanelData | null>(null);
  let fetchWidth = $state(0);
  let error = $state<string | null>(null);
  let render = $state<{ ms: number; exceeded: boolean } | null>(null);

  const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");

  let requestId = 0;
  const load = (width: number) => {
    const id = ++requestId;
    fetchWidth = width;
    fetchPanelData(panel.id, width)
      .then((d) => { if (id === requestId) data = d; })
      .catch((e) => { if (id === requestId) error = String(e); });
  };

  $effect(() => {
    const el = plotEl;
    if (!el) return;
    load(Math.round(el.clientWidth || 800));
    const ro = new ResizeObserver(() => {
      const w = Math.round(el.clientWidth);
      if (w <= 0) return;
      if (Math.abs(w - fetchWidth) / Math.max(fetchWidth, 1) > 0.1) load(w);
      // ignore sub-threshold jitter (scrollbars, sub-pixel layout): each setSize redraws the plot
      else if (plot && Math.abs(w - margW - plot.width) >= RESIZE_MIN_PX) plot.setSize({ width: w - margW, height: 260 });
    });
    ro.observe(el);
    return () => ro.disconnect();
  });


  // y-views (2as.17): the user's pick lives in the panel spec; `pending` is the optimistic value until the snapshot catches up
  let pending = $state<YView | null | undefined>(undefined);
  const chosen = $derived(pending !== undefined ? pending : (panel.spec.y.selected ?? null));
  const viewKey = $derived(JSON.stringify(chosen));
  let bandPick = $state(false);
  let originOff = $state(false);
  // indexed view: series ÷ baseline (log, 1 centred); everything downstream draws `drawn`
  const ix = $derived(
    data?.kind === "time" && (chosen?.mode as string | undefined) === "indexed" && data.index
      ? indexSeries(data.series, data.index, { quantile: data.dataset.representation === "quantile", nMin: data.dataset.n_min ?? null })
      : null,
  );
  // filtered/raw data views (4ok.9): the user's pick, else Claude's default; optimistic until the snapshot catches up
  let pendingView = $state<DataViewName | null>(null);
  $effect(() => {
    void panel.spec.signal?.selected;
    pendingView = null;
  });
  const viewNow = $derived<DataViewName | null>(
    ((pendingView ?? panel.spec.signal?.selected ?? panel.spec.signal?.default) as DataViewName | undefined) ?? null,
  );
  const filtDrawn = $derived(data?.kind === "time" && data.filter && viewNow ? drawnFor(viewNow, data) : null);
  const pickView = (v: DataViewName) => {
    pendingView = v;
    selectDataView(panel.id, v).catch((e) => { pendingView = null; error = String(e); });
  };
  const drawn = $derived(
    ix && !ix.refused ? ix.series : filtDrawn ? filtDrawn.series : data?.kind === "time" ? data.series : [],
  );
  let idxBusy = $state(false);
  const yst = $derived(
    data?.kind === "time"
      ? yStats(drawn, { quantile: data.dataset.representation === "quantile", nMin: data.dataset.n_min ?? null })
      : data?.kind === "fleet" ? fleetStats(data) : null,
  );
  // catalog-derived y inputs (2as.10): natural bounds, physical limit, normal range
  const yctx = $derived(panel.spec.y.context ?? null);
  const yres = $derived(
    data?.kind === "fleet" ? fleetY(data, yctx, chosen) : yst ? (ix?.refused ? { ...resolveY(null, yst, yctx), refused: `${chosen?.label}: ${ix.refused}` } : resolveY(chosen, yst, yctx)) : null,
  );
  // the operating profile is computed on first view: look again a few times until it lands
  $effect(() => {
    if (!yctx?.notes.some((n) => n.startsWith("profile_pending"))) return;
    let tries = 0;
    const t = setInterval(() => {
      if (++tries > 4) return clearInterval(t);
      refreshYContext(panel.id).catch(() => {});
    }, 6000);
    return () => clearInterval(t);
  });
  $effect(() => {
    void panel.spec.y.selected;
    pending = undefined; // server state wins once it arrives
  });
  // indexed needs the server's baseline payload: no optimistic state, show progress until it lands
  const pickIndexed = (baseline: "window" | "previous" | "week") => {
    idxBusy = true;
    bandPick = false;
    selectYView(panel.id, { mode: "indexed", baseline }).catch((e) => (error = String(e))).finally(() => (idxBusy = false));
  };
  const pick = (body: Parameters<typeof selectYView>[1], optimistic: YView | null) => {
    pending = optimistic;
    bandPick = false;
    selectYView(panel.id, body).catch((e) => { pending = undefined; error = String(e); });
  };


  // marginal histogram (4ok.6): server computes it; the toggle lives in the panel spec
  const MARGINAL_W = 84;
  let margEl = $state<HTMLCanvasElement | null>(null);
  let rugEl = $state<HTMLCanvasElement | null>(null);
  let rugCellsNow: RugCell[] = [];
  let rugBounds: [number, number] = [0, 0];
  let rugTip = $state<{ x: number; y: number; text: string } | null>(null);
  // footer note under the pointer/focus -> band on the plot; rug cell under the pointer -> active notes
  let focus = $state<Where | null>(null);
  let activeNotes = $state<string[]>([]);
  function setFocus(w: Where | null) {
    focus = w;
    plot?.redraw(false);
  }
  function onRugLeave() {
    rugTip = null;
    activeNotes = [];
  }
  function onRugMove(e: MouseEvent) {
    if (data?.kind !== "time" || !data.bucket_state) return;
    const c = e.offsetX < rugBounds[0] || e.offsetX > rugBounds[1] ? null : hitRug(rugCellsNow, e.offsetX, e.offsetY);
    if (!c) { onRugLeave(); return; }
    const s = data.bucket_state[c.row];
    activeNotes = notesAt(notes, s.id, c.ts);
    const sd = data.series.find((x) => x.id === s.id);
    rugTip = { x: e.offsetX + 8, y: (rugEl?.offsetTop ?? 0) + e.offsetY + 12, text: rugHint(c, s, data.effective_step_ms, sd?.labels ? seriesName(sd.labels) : s.id, data.dataset.representation !== "quantile") };
  }
  let margBusy = $state(false);
  const indexedOn = $derived((chosen?.mode as string | undefined) === "indexed");
  const marg = $derived(data?.kind === "time" && !indexedOn ? (data.marginal ?? null) : null);
  const margW = $derived(marg ? MARGINAL_W : 0);
  // refetch panel data when what the server computes for it changes (marginal, indexed baseline)
  const dataKey = $derived(JSON.stringify([panel.spec.marginal ?? null, (panel.spec.y.selected?.mode as string | undefined) === "indexed" ? panel.spec.y.selected : null]));
  let lastKey: string | null = null;
  $effect(() => {
    const k = dataKey;
    if (lastKey !== null && k !== lastKey && fetchWidth) untrack(() => load(fetchWidth));
    lastKey = k;
  });
  const toggleMarginal = (ref: "previous" | "week" | "profile" | null) => {
    margBusy = true;
    setMarginal(panel.id, ref).catch((e) => (error = String(e))).finally(() => (margBusy = false));
  };

  let fleetView = $state<"band" | "heat" | "multiples">("band");
  // the heat view's y axis is members, not values: no y-view chips, badge, context strip or y notes there
  const yIsValues = $derived(!(data?.kind === "fleet" && fleetView === "heat"));
  const notes = $derived(
    data
      ? panelNotes(data.caveats, {
          yScaledToData: panel.spec.y.range_mode === "data" && yIsValues,
          located: data.located,
          yContext: yIsValues ? yctx : null,
          auto: panel.spec.auto ?? null,
          unit: panel.spec.y.unit,
          nMin: data.dataset.n_min ?? null,
          representation: data.dataset.representation,
          filter: data?.kind === "time" && data.filter ? { label: data.filter.filter, reason: data.filter.reason } : null,
          indexed: ix && !ix.refused && data?.kind === "time" && data.index
            ? { label: data.index.label, skipped: ix.skipped, hidden: ix.hidden, nonPositive: ix.nonPositive }
            : null,
          marginal: marg
            ? { what: marg.what, ref: marg.reference.label, n: marg.windows.map((w) => w.n), nMin: marg.n_min, author: marg.author, reason: marg.reason }
            : null,
          yView:
            yIsValues && (chosen || yres?.refused)
              ? { label: chosen?.label ?? "", reason: chosen?.reason ?? null, author: chosen?.author ?? "user", refused: yres?.refused ?? null }
              : null,
        })
      : [],
  );


  // Heatmap facets render independently: report once per fetched dataset, when every facet drew.
  const facetStats = new Map<number, { ms: number; cells: number }>();
  let reportedFor: PanelData | null = null;
  // `again`: single-plot panels with view switches (fleet) report every redraw, not once per dataset
  const onFacetRendered = (i: number, total: number, ms: number, cells: number, facetH: number, again = false) => {
    facetStats.set(i, { ms, cells });
    if (!data || (reportedFor === data && !again) || facetStats.size < total) return;
    reportedFor = data;
    const all = [...facetStats.values()];
    const cellSum = all.reduce((a, f) => a + f.cells, 0);
    const msMax = Math.max(...all.map((f) => f.ms));
    heatCells = cellSum;
    reportRender({ panel_id: panel.id, render_ms: msMax, points: cellSum, width_px: fetchWidth, height_px: facetH * total })
      .then((r) => (render = { ms: msMax, exceeded: r.budget_exceeded }))
      .catch((e) => (error = String(e)));
  };
  let heatCells = $state(0);
  // heatmap view options (local UI state): colour by count or by share of the column, colormap, quantile outline
  let heatColor = $state<"count" | "density">("count");
  let heatCmap = $state<"viridis" | "cividis">("viridis");
  let heatQ = $state<number | null>(null);
  // percentile-band view of the same payload: no refetch, the server already sent the bands
  let heatView = $state<"heatmap" | "percentiles">("heatmap");
  let heatQs = $state<number[]>(DEFAULT_QUANTILES);
  // follow the spec when Claude re-shows the panel with other quantiles; local picks last until then
  let specQsKey: string | null = null;
  $effect.pre(() => {
    const qs = panel.spec.layers[0]?.quantiles ?? DEFAULT_QUANTILES;
    const k = JSON.stringify(qs);
    if (k !== specQsKey) {
      specQsKey = k;
      heatQs = qs;
    }
  });
  $effect(() => {
    if (data?.kind === "heatmap") heatView = data.mark === "percentiles" ? "percentiles" : "heatmap";
  });

  const close = () => closePanel(panel.id).catch((e) => (error = String(e)));

  // reference layers (2as.11): switching one changes the payload, so fetch it again
  let overlayBusy = $state(false);
  const toggleOverlay = (key: "normal" | "limit" | "ghost", on: boolean) => {
    overlayBusy = true;
    setOverlays(panel.id, { [key]: on })
      .then(() => load(fetchWidth || 800))
      .catch((e) => (error = String(e)))
      .finally(() => (overlayBusy = false));
  };
  // follow-up (2as.19): fast errors flatter latency, so offer success and failure apart
  let splitBusy = $state(false);
  let splitNote = $state<string | null>(null);
  const split = () => {
    splitBusy = true;
    splitNote = null;
    splitOutcome(panel.id)
      .then((r) => {
        const bits = [r.success && `successes ${r.success.panel}`, r.failure && `failures ${r.failure.panel}`].filter(Boolean);
        splitNote = `by ${r.label}: ${bits.join(", ")}${r.excluded.length ? ` (left out: ${r.excluded.join(", ")})` : ""}${r.note ? `. ${r.note}` : ""}`;
      })
      .catch((e) => (error = String(e)))
      .finally(() => (splitBusy = false));
  };
  // reframings (2as.15) are proposals: accepting one opens a new panel, this one is untouched
  let reframeBusy = $state(false);
  const reframe = (i: number) => {
    reframeBusy = true;
    reframePanel(panel.id, i).catch((e) => (error = String(e))).finally(() => (reframeBusy = false));
  };
  // a profile that finishes later arrives as a new y context: look at the layers again
  let ctxKey: string | null = null;
  $effect(() => {
    const k = JSON.stringify(panel.spec.y.context ?? null);
    if (ctxKey !== null && k !== ctxKey && data?.kind === "time") untrack(() => load(fetchWidth || 800));
    ctxKey = k;
  });

  $effect(() => {
    const el = plotEl;
    const d = data;
    if (!d || d.kind !== "time" || !el) return;
    void annKey; // tracked: rebuild the plot when the annotation set changes
    void viewNow; // tracked: filtered/raw switches the drawn series
    void viewKey; // tracked: log/linear scale cannot change in place, so rebuild on a view change
    const bandMode = bandPick; // tracked: band pick drags on y instead of x
    void margW; // tracked: the plot shrinks while the marginal is on
    const m = untrack(() => marg);
    const yr = untrack(() => yres);
    const anns = untrack(() => panelAnns);
    const mode = theme.effective; // tracked: rebuild the plot when the theme flips
    const colors = readAnnotationColors(el);
    const { stroke, grid, palette } = plotColors(el, mode);
    const dpr = window.devicePixelRatio || 1;
    const drawnNow = untrack(() => drawn);
    const model = toUplot(
      drawnNow,
      { start: d.dataset.start_ms, end: d.dataset.end_ms, step: d.effective_step_ms },
      {
        quantile: d.dataset.representation === "quantile",
        nMin: d.dataset.n_min ?? null,
        context: untrack(() => filtDrawn?.context),
        edges: untrack(() => (viewNow === "raw" ? undefined : d.filter?.edges)),
        overlays: overlayDraw(d.overlays),
        palette,
      },
    );
    const width = (el.clientWidth || 800) - margW;
    const rugExtra = rugAxisExtra(d.bucket_state?.length ?? 0);
    const unit = d.panel.spec.y.unit;
    const up = measureFirstDraw(
      (onDraw) =>
        new uPlot(
          {
            width, height: 260, series: model.series, bands: model.bands,
            tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
            scales: {
              x: { time: true },
              y: yr?.range ? { distr: yr.log ? 3 : 1, log: 10, range: () => yr.range! } : {},
            },
            // axis/grid colors from CSS tokens so they follow the theme
            axes: [
              // a rug sits between the plot floor and the tick labels: grow the axis, push the labels
              // below it, and drop the tick marks (they would cut through the rug rows)
              rugExtra > 0
                ? { stroke, grid: { stroke: grid }, ticks: { show: false }, size: 50 + rugExtra, gap: 5 + rugExtra }
                : { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
              yr?.log && d.index
                ? {
                    label: "ratio to baseline (log)", stroke, grid: { stroke: grid }, ticks: { stroke: grid },
                    splits: () => ratioTicks(yr.range![1]),
                    values: (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtRatio(t))),
                  }
                : {
                    label: unit ?? "value (unit unknown)", stroke, grid: { stroke: grid }, ticks: { stroke: grid },
                    size: axisGutterSize(),
                    values: (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtSI(t, unit))),
                  },
            ],
            // brush = x-only selection; we open a menu instead of zooming
            cursor: { drag: bandMode ? { setScale: false, x: false, y: true } : { setScale: false, x: true, y: false } },
            hooks: {
              ready: [
                (u: uPlot) => {
                  const rows = u.root.querySelectorAll(".u-legend .u-series");
                  model.legendHidden.forEach((i) => rows[i]?.classList.add("tn-hidden"));
                },
              ],
              draw: [
                onDraw,
                (u: uPlot) => {
                  const ops = drawOps(
                    anns,
                    { min: u.scales.x.min ?? 0, max: u.scales.x.max ?? 0 },
                    { min: u.scales.y.min ?? 0, max: u.scales.y.max ?? 0 },
                  );
                  annCount = ops.length;
                  if (d.index && yr?.log) {
                    const y1 = u.valToPos(1, "y", true);
                    const c = u.ctx;
                    c.save(); c.strokeStyle = stroke; c.lineWidth = 1; c.setLineDash([]);
                    c.beginPath(); c.moveTo(u.bbox.left, y1); c.lineTo(u.bbox.left + u.bbox.width, y1); c.stroke();
                    c.fillStyle = stroke; c.font = `${10 * dpr}px sans-serif`; c.fillText("1 = baseline", u.bbox.left + 4 * dpr, y1 - 3 * dpr);
                    c.restore();
                  }
                  originOff = nonZeroOrigin(u.scales.y.min ?? 0, u.scales.y.max ?? 0, !!yr?.log);
                  if (m && margEl) {
                    const top = u.bbox.top / dpr, bottom = top + u.bbox.height / dpr;
                    const mctx = setupCanvas(margEl, MARGINAL_W, el.clientHeight || 260);
                    if (mctx) drawMarginal(mctx, m.windows, m.n_min, { toPx: (v) => u.valToPos(v, "y"), top, bottom, width: MARGINAL_W, fg: stroke, muted: grid });
                  }
                  if (rugEl && d.bucket_state?.length) {
                    const bs = d.bucket_state;
                    const left = u.bbox.left / dpr;
                    const rctx = setupCanvas(rugEl, u.width, rugHeight(bs.length));
                    if (rctx) {
                      rugEl.style.top = `${rugTop((u.bbox.top + u.bbox.height) / dpr)}px`;
                      // same array toUplot coloured by, so tints match the lines
                      const order = new Map(drawnNow.map((s, k) => [s.id, k]));
                      const grey = getComputedStyle(el).getPropertyValue("--muted").trim();
                      rugCellsNow = rugCells(bs, d.effective_step_ms, (ms) => left + u.valToPos(ms / 1000, "x"));
                      rugBounds = [left, left + u.bbox.width / dpr];
                      const fg = getComputedStyle(el).getPropertyValue("--fg").trim();
                      rctx.save();
                      rctx.beginPath(); rctx.rect(left, 0, u.bbox.width / dpr, rugHeight(bs.length)); rctx.clip();
                      drawRug(rctx, rugCellsNow, {
                        tint: (row) => {
                          const k = order.get(bs[row].id);
                          // not drawn as a line: faint neutral, never the solid EMPTY grey
                          return k === undefined ? rgba(fg, 0.12) : rgba(palette[k % palette.length], 0.2);
                        },
                        grey, line: grey,
                      });
                      rctx.restore();
                    }
                  }
                  const fw = untrack(() => focus);
                  if (fw) {
                    const c = u.ctx, l = u.bbox.left, r = l + u.bbox.width;
                    c.save(); c.fillStyle = getComputedStyle(el).getPropertyValue("--warn").trim(); c.globalAlpha = 0.15;
                    for (const f of focusRects(fw, (ms) => u.valToPos(ms / 1000, "x", true), l, r)) c.fillRect(f.x, u.bbox.top, f.w, u.bbox.height);
                    c.restore();
                  }
                  drawAnnotations(u, ops, colors, dpr);
                },
              ],
              setSelect: [
                (u: uPlot) => {
                  const s = u.select;
                  if (bandMode) {
                    if (s.height >= 3) {
                      const hi = u.posToVal(s.top, "y"), lo = u.posToVal(s.top + s.height, "y");
                      pick({ mode: "band", lo, hi }, { mode: "band", label: "y band", lo, hi });
                    }
                    u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
                    return;
                  }
                  if (s.width < 3) return;
                selection = {
                  x0: u.posToVal(s.left, "x"),
                  x1: u.posToVal(s.left + s.width, "x"),
                  // u.select is relative to the plot area (CSS px);
                  // bbox is in canvas px, so scale it back to CSS px.
                  left: s.left + u.bbox.left / dpr,
                  top: u.bbox.top / dpr + 4,
                  width: s.width,
                };
                },
              ],
            },
          },
          model.data, el,
        ),
      (ms) => {
        reportRender({ panel_id: panel.id, render_ms: ms, points: model.points, width_px: width })
          .then((r) => (render = { ms, exceeded: r.budget_exceeded }))
          .catch((e) => (error = String(e)));
      },
    );
    plot = up;
    return () => {
      plot = null;
      selection = null;
      up.destroy();
    };
  });
</script>

<svelte:window onkeydown={(e) => { if (e.key === "Escape") bandPick = false; }} />

<section
  class="panel"
  id="panel-{panel.id}"
  data-panel-id={panel.id}
  data-annotation-count={annCount}
  data-overlays={data?.kind === "time" ? Object.keys(overlayDraw(data.overlays) ?? {}).join(",") : undefined}
  data-heatmap-cells={data?.kind === "heatmap" ? heatCells : undefined}
  data-percentile-bands={data?.kind === "heatmap" && heatView === "percentiles" ? heatCells : undefined}
  data-render-ms={render ? render.ms.toFixed(1) : undefined}
  data-budget-exceeded={render ? String(render.exceeded) : undefined}
>
  <header>
    <a class="obj-id" href="#/panel/{panel.id}" title="Panel {panel.id}">{panel.id}</a>
    <span class="question">Q: {panel.question}</span>
    {#if panel.answered_by}
      <a class="status answered-by" href="#finding-{panel.answered_by}">answered → {panel.answered_by}</a>
    {:else}
      <span class="status {panel.status}">{panel.status}</span>
    {/if}
    <PinButton object={panel.id} />
    <button class="close" type="button" aria-label="Close panel" onclick={close}>×</button>
  </header>
  {#if error}<div class="error">{error}</div>{/if}
  <div bind:this={plotEl} class="plot" style="position: relative">
    {#if data?.kind === "time" && (data.bucket_state?.length ?? 0) > 0}
      <canvas class="rug" bind:this={rugEl} data-rug aria-label="Coverage rug: where data is missing"
        onmousemove={onRugMove} onmouseleave={onRugLeave}></canvas>
      {#if rugTip}<div class="rug-tip" style="left:{rugTip.x}px;top:{rugTip.y}px">{rugTip.text}</div>{/if}
    {/if}
    {#if marg}
      <canvas class="marginal" bind:this={margEl} data-marginal data-marginal-basis={marg.basis}
        title="{marg.what} · filled: now · dashed: {marg.reference.label}"></canvas>
      <span class="marginal-head" data-marginal-n>{#each marginalHeader(marg.windows, marg.n_min) as line (line)}<span>{line}</span>{/each}</span>
    {/if}
    {#if data && data.kind === "heatmap"}
      {@const hm = data}
      {#if heatView === "percentiles"}
        {#if overlay(hm.series.length, heatQs.length)}
          <PercentilePlot
            data={hm} series={hm.series} qs={heatQs} width={fetchWidth} height={hm.facet_height_px}
            unit={hm.panel.spec.y.unit}
            onRendered={(ms, cells) => onFacetRendered(0, 1, ms, cells, hm.facet_height_px)}
            onBrush={(b) => (selection = { ...b, top: 4 })}
          />
          <CountStrip data={hm} series={hm.series} width={fetchWidth} />
        {:else}
          {#each hm.series as s, i (s.id)}
            <PercentilePlot
              data={hm} series={[s]} qs={heatQs} width={fetchWidth} height={hm.facet_height_px}
              unit={hm.panel.spec.y.unit}
              onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
              onBrush={(b) => (selection = { ...b, top: facetTop(hm.series.map(() => false), i, hm.facet_height_px) })}
            />
            <CountStrip data={hm} series={[s]} width={fetchWidth} />
          {/each}
        {/if}
      {:else}
      {#each hm.series as s, i (s.id)}
        <HeatmapPlot
          data={hm} series={s} width={fetchWidth} height={hm.facet_height_px}
          unit={hm.panel.spec.y.unit}
          color={heatColor} cmapName={heatCmap} overlayQ={heatQ} focusWhere={focus}
          onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
          onBrush={(b) => (selection = { ...b, top: facetTop(hm.series.map((f) => !!f.state), i, hm.facet_height_px) })}
        />
        <CountStrip data={hm} series={[s]} width={fetchWidth} />
      {/each}
      {/if}
      <div class="legend heat-controls">
        view:
        <button type="button" class:on={heatView === "heatmap"} onclick={() => (heatView = "heatmap")}>heatmap</button>
        <button type="button" class:on={heatView === "percentiles"} onclick={() => (heatView = "percentiles")}>percentile bands</button>
        {#if heatView === "percentiles"}
          · bands:
          {#each QUANTILE_CHOICES as q (q)}
            <button type="button" class:on={heatQs.includes(q)} disabled={!heatQs.includes(q) && heatQs.length >= MAX_QUANTILES}
              onclick={() => (heatQs = toggleQuantile(heatQs, q))}>{qLabel(q)}</button>
          {/each}
          <span class="hint">bucket holding q per {fmtStep(hm.effective_step_ms)} column, only where n ≥ 10/(1−q); faded lane: too few observations</span>
        {/if}
      </div>
      <div class="legend heat-controls">
        colour:
        <button type="button" class:on={heatColor === "count"} onclick={() => (heatColor = "count")}>count</button>
        <button type="button" class:on={heatColor === "density"} onclick={() => (heatColor = "density")}>share of column</button>
        · colormap:
        <button type="button" class:on={heatCmap === "viridis"} onclick={() => (heatCmap = "viridis")}>viridis</button>
        <button type="button" class:on={heatCmap === "cividis"} onclick={() => (heatCmap = "cividis")}>cividis</button>
        · outline bucket holding:
        {#each [null, 0.5, 0.95, 0.99] as q (q)}
          <button type="button" class:on={heatQ === q} onclick={() => (heatQ = q)}>{q === null ? "none" : `p${q * 100}`}</button>
        {/each}
        {#if heatQ !== null}(only where n ≥ {Math.ceil(10 / (1 - heatQ))}){/if}
      </div>
      {@const cm = colormap(heatCmap)}
      {@const cmax = colorMaxOf(hm.series, heatColor)}
      <div class="legend color-key">
        <span class="ramp" style="background: linear-gradient(to right, {[0, 0.25, 0.5, 0.75, 1].map((t) => cm(t)).join(', ')})"></span>
        {#if heatColor === "count"}
          0 → {Number(cmax.toPrecision(3))} per bucket per {fmtStep(hm.effective_step_ms)}; scale log(1+count) — rare buckets stay visible, counts are not linear in colour
        {:else}
          0 → {(100 * cmax).toPrecision(3)}% of the column, linear
        {/if}
        · no values trimmed · hatched: no data · dimmed: n &lt; {hm.dataset.n_min}{#if hm.value_merge > 1} · {hm.value_merge} source buckets per row{/if}
      </div>
    {/if}
    {#if data && data.kind === "spectrum"}
      <SpectrumPlot data={data} width={fetchWidth} onRendered={(ms, pts) => onFacetRendered(0, 1, ms, pts, 240)} />
    {/if}
    {#if data && data.kind === "spc"}
      {@const sc = data}
      {#each sc.series as s, i (s.id)}
        <SpcPlot data={sc} series={s} width={fetchWidth}
          onRendered={(ms, pts) => onFacetRendered(i, sc.series.length, ms, pts, 220)} />
      {/each}
    {/if}
    {#if data && data.kind === "littles"}
      {@const lt = data}
      {#each lt.series as s, i (s.id)}
        <LittlesPlot data={lt} series={s} width={fetchWidth}
          onRendered={(ms, pts) => onFacetRendered(i, lt.series.length, ms, pts, 300)} />
      {/each}
      {#if lt.unmatched.length}
        <div class="legend" data-littles-unmatched>Not judged — missing from some signals: {lt.unmatched.map((u) => `${Object.entries(u.labels).map(([k, v]) => `${k}=${v}`).join(", ") || "total"} (no ${u.missing_in.join(", ")})`).join("; ")}</div>
      {/if}
      {#if lt.more_groups}<div class="legend">{lt.more_groups} more groups not drawn (see the check's summary)</div>{/if}
    {/if}
    {#if data && data.kind === "seasonal"}
      {@const sz = data}
      {#each sz.series as s, i (s.id)}
        <SeasonalPlot data={sz} series={s} width={fetchWidth}
          onRendered={(ms, pts) => onFacetRendered(i, sz.series.length, ms, pts, 220)} />
      {/each}
    {/if}
    {#if data && data.kind === "fleet"}
      <FleetPlot data={data} width={fetchWidth} range={yres?.range ?? null} unit={panel.spec.y.unit ?? null} bind:view={fleetView} onRendered={(ms, pts, h) => onFacetRendered(0, 1, ms, pts, h ?? 260, true)} />
    {/if}
    {#if data && data.kind === "spectrogram"}
      {@const sg = data}
      {#each sg.series as s, i (s.id)}
        <SpectrogramPlot data={sg} series={s} width={fetchWidth} height={260}
          onRendered={(ms, cells) => onFacetRendered(i, sg.series.length, ms, cells, 260)} />
      {/each}
      <div class="legend">window {fmtStep(sg.segment_ms)} · hop {fmtStep(sg.hop_ms)} ({Math.round(sg.overlap * 100)}% overlap): each column summarizes one window · period resolution 1/{fmtStep(sg.segment_ms)}, rows show the max within the row · periods {Math.round(sg.limits.shortest_s)}s–{Math.round(sg.limits.longest_s)}s · colour: share of window variance, linear 0–1 · faint: below the 1% false-alarm level · hatched: window under 50% covered</div>
    {/if}
    {#if data && data.kind === "histogram"}
      {@const hg = data}
      {#each hg.series.flatMap((s) => s.windows.map((w) => windowBadge(w, hg.effective_step_ms)).filter((b) => b !== null)) as b, i (i)}
        <span class="chip caveat" data-window-coverage title={b.title}>{b.text}</span>
      {/each}
      {#each hg.series as s, i (s.id)}
        <DistributionPlot
          data={hg} series={s} width={fetchWidth} unit={hg.panel.spec.y.unit}
          onRendered={(ms, points) => onFacetRendered(i, hg.series.length, ms, points, 220)}
        />
      {/each}
    {/if}
    {#if (data?.kind === "time" || data?.kind === "fleet") && yIsValues && yres && yres.effective && (yres.zoomed || yres.log || yres.effective.mode === "semantic" || (yres.reference && chosen?.mode === "reference"))}
      <span class="y-badge" data-y-badge>{badgeText(yres.effective, yres, panel.spec.y.unit, data.kind === "time" ? data.index?.label : undefined, yctx)}</span>
    {/if}
    {#if (data?.kind === "time" || data?.kind === "fleet") && yIsValues && yres?.zoomed && yres.range && yst?.all}
      {@const cs = contextStrip(stripExtent(yst.all, yctx), yres.range)}
      <span class="y-strip" style="bottom:{32 + (data.kind === "time" ? rugAxisExtra(data.bucket_state?.length ?? 0) : 0)}px" title="where this view sits within the full data range"><i style="bottom:{cs.bottomPct}%;height:{cs.heightPct}%"></i></span>
    {/if}
    {#if data?.kind === "time" && originOff}<span class="y-origin" data-y-origin>y ≠ 0</span>{/if}
    {#if selection}
      {#key selection}
        <SelectionMenu
          panelId={panel.id}
          x0={selection.x0}
          x1={selection.x1}
          left={selection.left}
          top={selection.top}
          width={selection.width}
          onCancel={cancelSelection}
          distribution={data?.kind === "heatmap" || !!data?.dataset.histogram}
        />
      {/key}
    {/if}
  </div>
  {#if data?.kind === "time" && data.bucket_state_more}
    <div class="rug-more">{rugMoreLabel(data.bucket_state_more)}</div>
  {/if}
  {#if data?.kind === "time" && data.overlays?.flags}
    <div class="legend overlays" role="group" aria-label="Reference layers">
      layers:
      {#each overlayChips(data.overlays) as c (c.key)}
        <button
          type="button" data-overlay={c.key} title={c.title} class:on={c.on}
          disabled={!c.enabled || overlayBusy} aria-pressed={c.on}
          onclick={() => toggleOverlay(c.key, !c.on)}
        >{c.label}</button>
      {/each}
    </div>
  {/if}
  {#if data?.dataset.histogram}
    <div class="legend reframes" role="group" aria-label="Follow-ups">
      follow-up:
      <button type="button" data-follow-up="split-outcome" disabled={splitBusy}
        title="Fast errors flatter latency and slow errors hide in it: show successful and failed requests apart (two new panels; this one stays)."
        onclick={split}>split by success / failure</button>
      {#if splitNote}<span class="hint" data-split-note>{splitNote}</span>{/if}
    </div>
  {/if}
  {#if data?.kind === "time" && yctx?.reframes?.length}
    <div class="legend reframes" role="group" aria-label="Reframings">
      reframe:
      {#each yctx.reframes as r, i (r.title)}
        <button
          type="button" data-reframe={i} title={`${r.reason} (${r.basis}). Opens a new panel; this one stays.`}
          disabled={reframeBusy} onclick={() => reframe(i)}
        >{r.title}</button>
      {/each}
    </div>
  {/if}
  {#if (data?.kind === "time" || data?.kind === "fleet") && yIsValues && yst}
    <div class="legend y-views" role="group" aria-label="Y-axis view">
      y:
      {#each offeredViews(yst, yctx).filter((o) => data?.kind === "time" || !["indexed", "meaningful", "log"].includes(o.mode)) as o (o.mode + (o.baseline ?? ""))}
        <button
          type="button" disabled={!o.enabled} title={o.title} class:suggest={o.suggest}
          class:on={o.mode === "band" ? bandPick || (chosen?.mode === "band" && !chosen?.id) : (chosen?.mode ?? "auto") === o.mode && !chosen?.id && (o.baseline ?? null) === (chosen?.baseline ?? null)}
          onclick={() => o.mode === "band" ? (bandPick = !bandPick) : o.mode === "indexed" ? pickIndexed(o.baseline!) : pick({ mode: o.mode }, o.mode === "auto" ? null : { mode: o.mode, label: o.label })}
        >{o.label}</button>
      {/each}
      {#if (panel.spec.y.views ?? []).length}
        · Claude:
        {#each panel.spec.y.views ?? [] as v (v.id)}
          <button type="button" class="suggested" class:on={chosen?.id === v.id} title={v.reason ?? ""} data-y-suggestion={v.id} onclick={() => pick({ suggestion: v.id! }, v)}>{v.label}</button>
        {/each}
      {/if}
      {#if bandPick}<span class="hint">drag vertically on the plot · Esc cancels</span>{/if}
      {#if idxBusy}<span class="hint">fetching baseline…</span>{/if}
    </div>
  {/if}
  {#if data?.kind === "time" && data.filter}
    <div class="legend y-views" role="group" aria-label="Data view">
      data:
      {#each data.filter.offered as v (v)}
        <button type="button" class:on={viewNow === v} data-view={v}
          title={v === data.filter.default ? `Claude's pick: ${data.filter.reason}` : ""}
          onclick={() => pickView(v)}>{VIEW_LABELS[v]}{v === data.filter.default ? " · Claude" : ""}</button>
      {/each}
      <span class="hint">{data.filter.filter}; dashed: filter edge (unreliable)</span>
    </div>
  {/if}
  {#if data?.kind === "time" && data.series.some((s) => s.lo)}
    {@const bandLegend = intervalLegend(data.dataset)}
    {#if bandLegend}<div class="legend" data-interval-legend>band: {bandLegend}</div>{/if}
  {/if}
  {#if data?.kind === "time"}
    {@const fixed = data.dataset.producer?.kind === "code"}
    <div class="legend y-views" role="group" aria-label="Marginal histogram">
      marginal:
      <button type="button" class:on={!panel.spec.marginal} onclick={() => toggleMarginal(null)}>off</button>
      {#each [["previous", "vs previous window"], ["week", "vs last week"], ["profile", "vs normal profile"]] as [r, label] (r)}
        <button type="button" disabled={indexedOn || margBusy || fixed}
          title={fixed ? "a code output is fixed data: no reference window to fetch" : indexedOn ? "the marginal shows values; it is off in the indexed view" : ""}
          class:on={panel.spec.marginal?.reference === r} data-marginal-ref={r}
          onclick={() => toggleMarginal(r as "previous" | "week" | "profile")}>{label}</button>
      {/each}
      {#if panel.spec.marginal?.author === "claude"}<span class="hint">Claude: {panel.spec.marginal.reason}</span>{/if}
      {#if margBusy}<span class="hint">fetching reference…</span>{/if}
    </div>
  {/if}
  {#if data}
    {@const parts = provenanceParts(data.dataset)}
    <div class="shown">
      <p class="what">{describeShown(data.dataset, fmtStep(data.effective_step_ms), data.kind, "mark" in data ? (data.kind === "heatmap" ? heatView === "percentiles" ? "percentiles" : "" : data.mark) : "", fleetView)}</p>
      <p class="where">
        <span data-provenance>{#if parts}{@const [pre, node, post] = parts}{pre}<button type="button" class="ref-chip obj-id" data-code-link={node} title="View the code of {node}" onclick={() => openCode?.(node)}>{node}</button>{post}{:else}{provenanceText(data.dataset)}{/if}</span> · {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step
        {fmtStep(data.effective_step_ms)}
      </p>
    </div>
    <details class="query">
      <summary>Query</summary>
      <pre><code>{data.dataset.expr}</code></pre>
    </details>
    {#if notes.length > 0}
      <ul class="notes" aria-label="Notes and warnings">
        {#each notes as note (note.key)}
          <!-- svelte-ignore a11y_no_noninteractive_tabindex (focus mirrors hover so keyboard users get the same graph highlight) -->
          <li
            class="note {note.kind}" class:active={activeNotes.includes(note.key)} data-note={note.key}
            tabindex="0"
            onmouseenter={() => setFocus(note.where ?? null)} onmouseleave={() => setFocus(null)}
            onfocus={() => setFocus(note.where ?? null)} onblur={() => setFocus(null)}
          >
            <span class="tag">{note.kind === "caveat" ? "Caveat" : "Note"}</span>
            {note.text}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
  {#if data}
    <MetricCard panelId={panel.id} />
  {/if}
  {#if panelThreads.length > 0}
    <div class="threads">
      {#each panelThreads as thread (thread.id)}
        <PanelThread {thread} />
      {/each}
    </div>
  {/if}
</section>
