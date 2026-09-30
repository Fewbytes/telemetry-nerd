<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    closePanel, fetchPanelData, reportRender,
    type Annotation, type Panel, type PanelData,
  } from "./lib/api";
  import { toUplot } from "./chart/toUplot";
  import { measureFirstDraw } from "./chart/measureDraw";
  import { drawAnnotations, drawOps, readAnnotationColors } from "./chart/annotations";
  import SelectionMenu from "./components/SelectionMenu.svelte";

  let { panel, annotations = [] }: { panel: Panel; annotations?: Annotation[] } = $props();

  const panelAnns = $derived(
    annotations.filter((a) => !a.deleted && (a.panel === null || a.panel === panel.id)),
  );

  // Brush selection: x range in seconds (from uPlot scales) + px position over the plot.
  interface Selection { x0: number; x1: number; left: number; top: number; width: number }
  let selection = $state<Selection | null>(null);
  let annCount = $state(0);
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
      if (w > 0 && Math.abs(w - fetchWidth) / Math.max(fetchWidth, 1) > 0.1) load(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  });

  const close = () => closePanel(panel.id).catch((e) => (error = String(e)));

  $effect(() => {
    const el = plotEl;
    if (!data || !el) return;
    const anns = panelAnns; // tracked: rebuild the plot when annotations change
    const colors = readAnnotationColors(el);
    const dpr = window.devicePixelRatio || 1;
    const model = toUplot(data.series, {
      start: data.dataset.start_ms, end: data.dataset.end_ms, step: data.effective_step_ms,
    });
    const width = el.clientWidth || 800;
    const unit = data.panel.spec.y.unit;
    const up = measureFirstDraw(
      (onDraw) =>
        new uPlot(
          {
            width, height: 260, series: model.series, bands: model.bands,
            tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
            scales: { x: { time: true } },
            axes: [{}, { label: unit ?? "value (unit unknown)" }],
            // brush = x-only selection; we open a menu instead of zooming
            cursor: { drag: { setScale: false, x: true, y: false } },
            hooks: {
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
  data-render-ms={render ? render.ms.toFixed(1) : undefined}
  data-budget-exceeded={render ? String(render.exceeded) : undefined}
>
  <header>
    <span class="question">Q: {panel.question}</span>
    <span class="status {panel.status}">{panel.status}</span>
    {#if panel.answered_by}<span class="answered">answered → {panel.answered_by}</span>{/if}
    <button class="close" type="button" aria-label="Close panel" onclick={close}>×</button>
  </header>
  {#if panel.spec.y.range_mode === "data"}
    <div class="badge">y scaled to data (no reference range yet)</div>
  {/if}
  {#if error}<div class="error">{error}</div>{/if}
  <div bind:this={plotEl} class="plot" style="position: relative">
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
        />
      {/key}
    {/if}
  </div>
  {#if data}
    <footer>
      {data.dataset.source} · <code>{data.dataset.expr}</code> ·
      {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step
      {fmtStep(data.effective_step_ms)} · {data.dataset.representation}, min/max envelope
      {#if data.caveats.length > 0}&nbsp;· caveats: {data.caveats.join(", ")}{/if}
    </footer>
  {/if}
</section>
