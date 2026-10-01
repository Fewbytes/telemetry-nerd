<script lang="ts">
  import { untrack } from "svelte";
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    closePanel, fetchPanelData, reportRender, selectYView,
    type Annotation, type Panel, type PanelData, type Thread, type YView,
  } from "./lib/api";
  import { toUplot } from "./chart/toUplot";
  import { describeShown, panelNotes } from "./lib/panelNotes";
  import { badgeText, contextStrip, nonZeroOrigin, offeredViews, resolveY, yStats } from "./chart/yview";
  import { measureFirstDraw } from "./chart/measureDraw";
  import { drawAnnotations, drawOps, readAnnotationColors } from "./chart/annotations";
  import { plotColors, theme } from "./lib/theme.svelte";
  import SelectionMenu from "./components/SelectionMenu.svelte";
  import PanelThread from "./components/PanelThread.svelte";
  import PinButton from "./components/PinButton.svelte";
  import HeatmapPlot from "./components/HeatmapPlot.svelte";
  import PercentilePlot from "./components/PercentilePlot.svelte";
  import { DEFAULT_QUANTILES, QUANTILE_CHOICES, MAX_QUANTILES, overlay, qLabel, toggleQuantile } from "./chart/percentiles";
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


  // y-views (2as.17): the user's pick lives in the panel spec; `pending` is the optimistic value until the snapshot catches up
  let pending = $state<YView | null | undefined>(undefined);
  const chosen = $derived(pending !== undefined ? pending : (panel.spec.y.selected ?? null));
  const viewKey = $derived(JSON.stringify(chosen));
  let bandPick = $state(false);
  let originOff = $state(false);
  const yst = $derived(
    data?.kind === "time"
      ? yStats(data.series, { quantile: data.dataset.representation === "quantile", nMin: data.dataset.n_min ?? null })
      : null,
  );
  const yres = $derived(yst ? resolveY(chosen, yst) : null);
  $effect(() => {
    void panel.spec.y.selected;
    pending = undefined; // server state wins once it arrives
  });
  const pick = (body: Parameters<typeof selectYView>[1], optimistic: YView | null) => {
    pending = optimistic;
    bandPick = false;
    selectYView(panel.id, body).catch((e) => { pending = undefined; error = String(e); });
  };

  const notes = $derived(
    data
      ? panelNotes(data.caveats, {
          yScaledToData: panel.spec.y.range_mode === "data",
          nMin: data.dataset.n_min ?? null,
          representation: data.dataset.representation,
          yView:
            chosen || yres?.refused
              ? { label: chosen?.label ?? "", reason: chosen?.reason ?? null, author: chosen?.author ?? "user", refused: yres?.refused ?? null }
              : null,
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
  // percentile-band view of the same payload: no refetch, the server already sent the bands
  let heatView = $state<"heatmap" | "percentiles">("heatmap");
  let heatQs = $state<number[]>(panel.spec.layers[0]?.quantiles ?? DEFAULT_QUANTILES);
  $effect(() => {
    if (data?.kind === "heatmap") heatView = data.mark === "percentiles" ? "percentiles" : "heatmap";
  });

  const close = () => closePanel(panel.id).catch((e) => (error = String(e)));

  $effect(() => {
    const el = plotEl;
    const d = data;
    if (!d || d.kind !== "time" || !el) return;
    void annKey; // tracked: rebuild the plot when the annotation set changes
    void viewKey; // tracked: log/linear scale cannot change in place, so rebuild on a view change
    const bandMode = bandPick; // tracked: band pick drags on y instead of x
    const yr = untrack(() => yres);
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
            scales: {
              x: { time: true },
              y: yr?.range ? { distr: yr.log ? 3 : 1, log: 10, range: () => yr.range! } : {},
            },
            // axis/grid colors from CSS tokens so they follow the theme
            axes: [
              { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
              { label: unit ?? "value (unit unknown)", stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
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
                  originOff = nonZeroOrigin(u.scales.y.min ?? 0, u.scales.y.max ?? 0, !!yr?.log);
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
        {:else}
          {#each hm.series as s, i (s.id)}
            <PercentilePlot
              data={hm} series={[s]} qs={heatQs} width={fetchWidth} height={hm.facet_height_px}
              unit={hm.panel.spec.y.unit}
              onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
              onBrush={(b) => (selection = { ...b, top: i * (hm.facet_height_px + 14) + 4 })}
            />
          {/each}
        {/if}
      {:else}
      {#each hm.series as s, i (s.id)}
        <HeatmapPlot
          data={hm} series={s} width={fetchWidth} height={hm.facet_height_px}
          unit={hm.panel.spec.y.unit}
          color={heatColor} cmapName={heatCmap} overlayQ={heatQ}
          onRendered={(ms, cells) => onFacetRendered(i, hm.series.length, ms, cells, hm.facet_height_px)}
          onBrush={(b) => (selection = { ...b, top: i * (hm.facet_height_px + 14) + 4 })}
        />
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
    {#if data?.kind === "time" && yres && (yres.zoomed || yres.log)}
      <span class="y-badge" data-y-badge>{badgeText(chosen!, yres, panel.spec.y.unit)}</span>
    {/if}
    {#if data?.kind === "time" && yres?.zoomed && yres.range && yst?.all}
      {@const cs = contextStrip(yst.all, yres.range)}
      <span class="y-strip" title="where this view sits within the full data range"><i style="bottom:{cs.bottomPct}%;height:{cs.heightPct}%"></i></span>
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
  {#if data?.kind === "time" && yst}
    <div class="legend y-views" role="group" aria-label="Y-axis view">
      y:
      {#each offeredViews(yst) as o (o.mode)}
        <button
          type="button" disabled={!o.enabled} title={o.title} class:suggest={o.suggest}
          class:on={o.mode === "band" ? bandPick || (chosen?.mode === "band" && !chosen?.id) : (chosen?.mode ?? "auto") === o.mode && !chosen?.id}
          onclick={() => o.mode === "band" ? (bandPick = !bandPick) : pick({ mode: o.mode }, o.mode === "auto" ? null : { mode: o.mode, label: o.label })}
        >{o.label}</button>
      {/each}
      {#if (panel.spec.y.views ?? []).length}
        · Claude:
        {#each panel.spec.y.views ?? [] as v (v.id)}
          <button type="button" class="suggested" class:on={chosen?.id === v.id} title={v.reason ?? ""} data-y-suggestion={v.id} onclick={() => pick({ suggestion: v.id! }, v)}>{v.label}</button>
        {/each}
      {/if}
      {#if bandPick}<span class="hint">drag vertically on the plot · Esc cancels</span>{/if}
    </div>
  {/if}
  {#if data}
    <div class="shown">
      <p class="what">{describeShown(data.dataset, fmtStep(data.effective_step_ms), data.kind, "mark" in data ? (data.kind === "heatmap" ? heatView === "percentiles" ? "percentiles" : "" : data.mark) : "")}</p>
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
