<script lang="ts">
  import { untrack } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    closePanel, fetchPanelData, reportRender,
    type Annotation, type Panel, type PanelData, type Thread,
  } from "./lib/api";
  import { toUplot } from "./chart/toUplot";
  import { describeShown, panelNotes } from "./lib/panelNotes";
  import { measureFirstDraw } from "./chart/measureDraw";
  import { drawAnnotations, drawOps, readAnnotationColors } from "./chart/annotations";
  import { plotColors, theme } from "./lib/theme.svelte";
  import SelectionMenu from "./components/SelectionMenu.svelte";
  import PanelThread from "./components/PanelThread.svelte";
  import PinButton from "./components/PinButton.svelte";
  import HeatmapPlot from "./components/HeatmapPlot.svelte";
  import DistributionPlot from "./components/DistributionPlot.svelte";

  let { panel, annotations = [], threads = [] }: {
    panel: Panel; annotations?: Annotation[]; threads?: Thread[];
  } = $props();

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
  const fmtStep = (ms: number) => (ms % 60_000 === 0 ? `${ms / 60_000}m` : `${ms / 1000}s`);

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
      else if (plot && Math.abs(w - plot.width) >= RESIZE_MIN_PX) plot.setSize({ width: w, height: 260 });
    });
    ro.observe(el);
    return () => ro.disconnect();
  });

  const notes = $derived(
    data
      ? panelNotes(data.caveats, {
          yScaledToData: panel.spec.y.range_mode === "data",
          nMin: data.dataset.n_min ?? null,
          representation: data.dataset.representation,
        })
      : [],
  );


  // Heatmap facets render independently: report once per fetched dataset, when every facet drew.
  const facetStats = new Map<number, { ms: number; cells: number }>();
  let reportedFor: PanelData | null = null;
  const onFacetRendered = (i: number, total: number, ms: number, cells: number, facetH: number) => {
    facetStats.set(i, { ms, cells });
    if (!data || reportedFor === data || facetStats.size < total) return;
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

  const close = () => closePanel(panel.id).catch((e) => (error = String(e)));

  $effect(() => {
    const el = plotEl;
    const d = data;
    if (!d || d.kind !== "time" || !el) return;
    void annKey; // tracked: rebuild the plot when the annotation set changes
    const anns = untrack(() => panelAnns);
    const mode = theme.effective; // tracked: rebuild the plot when the theme flips
    const colors = readAnnotationColors(el);
    const { stroke, grid } = plotColors(el, mode);
    const dpr = window.devicePixelRatio || 1;
    const model = toUplot(
      d.series,
      { start: d.dataset.start_ms, end: d.dataset.end_ms, step: d.effective_step_ms },
      { quantile: d.dataset.representation === "quantile", nMin: d.dataset.n_min ?? null },
    );
    const width = el.clientWidth || 800;
    const unit = d.panel.spec.y.unit;
    const up = measureFirstDraw(
      (onDraw) =>
        new uPlot(
          {
            width, height: 260, series: model.series, bands: model.bands,
            tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
            scales: { x: { time: true } },
            // axis/grid colors from CSS tokens so they follow the theme
            axes: [
              { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
              { label: unit ?? "value (unit unknown)", stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
            ],
            // brush = x-only selection; we open a menu instead of zooming
            cursor: { drag: { setScale: false, x: true, y: false } },
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
                  drawAnnotations(u, ops, colors, dpr);
                },
              ],
              setSelect: [
                (u: uPlot) => {
                  const s = u.select;
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

<section
  class="panel"
  id="panel-{panel.id}"
  data-panel-id={panel.id}
  data-annotation-count={annCount}
  data-heatmap-cells={data?.kind === "heatmap" ? heatCells : undefined}
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
    {#if data && data.kind === "heatmap"}
      {@const hm = data}
      {#each hm.series as s, i (s.id)}
        <HeatmapPlot
          data={hm} series={s} width={fetchWidth} height={hm.facet_height_px}
          unit={hm.panel.spec.y.unit}
          color={heatColor} cmapName={heatCmap} overlayQ={heatQ}
          onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
          onBrush={(b) => (selection = { ...b, top: i * (hm.facet_height_px + 14) + 4 })}
        />
      {/each}
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
      <div class="legend">colour: count per bucket per {fmtStep(hm.effective_step_ms)} (log scale) · hatched: no data · dimmed: n &lt; {hm.dataset.n_min}{#if hm.value_merge > 1} · {hm.value_merge} source buckets per row{/if}</div>
    {/if}
    {#if data && data.kind === "histogram"}
      {@const hg = data}
      {#each hg.series as s, i (s.id)}
        <DistributionPlot
          data={hg} series={s} width={fetchWidth} unit={hg.panel.spec.y.unit}
          onRendered={(ms, points) => onFacetRendered(i, hg.series.length, ms, points, 220)}
        />
      {/each}
    {/if}
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
  {#if data}
    <div class="shown">
      <p class="what">{describeShown(data.dataset, fmtStep(data.effective_step_ms), data.kind)}</p>
      <p class="where">
        {data.dataset.source} · {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step
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
          <li class="note {note.kind}" data-note={note.key}>
            <span class="tag">{note.kind === "caveat" ? "Caveat" : "Note"}</span>
            {note.text}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
  {#if panelThreads.length > 0}
    <div class="threads">
      {#each panelThreads as thread (thread.id)}
        <PanelThread {thread} />
      {/each}
    </div>
  {/if}
</section>
