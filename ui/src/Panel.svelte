<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { fetchPanelData, reportRender, type Panel, type PanelData } from "./api";
  import { toUplot } from "./chart/toUplot";

  let { panel }: { panel: Panel } = $props();

  let plotEl = $state<HTMLDivElement | null>(null);
  let data = $state<PanelData | null>(null);
  let error = $state<string | null>(null);
  let render = $state<{ ms: number; exceeded: boolean } | null>(null);

  const fmtTime = (ms: number) => new Date(ms).toISOString().replace(".000Z", "Z");
  const fmtStep = (ms: number) => (ms % 60_000 === 0 ? `${ms / 60_000}m` : `${ms / 1000}s`);

  $effect(() => {
    const width = Math.round(plotEl?.clientWidth || 800);
    fetchPanelData(panel.id, width)
      .then((d) => (data = d))
      .catch((e) => (error = String(e)));
  });

  $effect(() => {
    const el = plotEl;
    if (!data || !el) return;
    const model = toUplot(data.series);
    const width = el.clientWidth || 800;
    const unit = data.panel.spec.y.unit;
    const t0 = performance.now();
    const plot = new uPlot(
      {
        width, height: 260, series: model.series, bands: model.bands,
        scales: { x: { time: true } },
        axes: [{}, { label: unit ?? "value (unit unknown)" }],
      },
      model.data, el,
    );
    const ms = performance.now() - t0;
    reportRender({ panel_id: panel.id, render_ms: ms, points: model.points, width_px: width })
      .then((r) => (render = { ms, exceeded: r.budget_exceeded }))
      .catch((e) => (error = String(e)));
    return () => plot.destroy();
  });
</script>

<section
  class="panel"
  id="panel-{panel.id}"
  data-panel-id={panel.id}
  data-render-ms={render ? render.ms.toFixed(1) : undefined}
  data-budget-exceeded={render ? String(render.exceeded) : undefined}
>
  <header>
    <span class="question">Q: {panel.question}</span>
    <span class="status {panel.status}">{panel.status}</span>
  </header>
  {#if panel.spec.y.range_mode === "data"}
    <div class="badge">y scaled to data (no reference range yet)</div>
  {/if}
  {#if error}<div class="error">{error}</div>{/if}
  <div bind:this={plotEl} class="plot"></div>
  {#if data}
    <footer>
      {data.dataset.source} · <code>{data.dataset.expr}</code> ·
      {fmtTime(data.dataset.start_ms)} – {fmtTime(data.dataset.end_ms)} · step
      {fmtStep(data.effective_step_ms)} · {data.dataset.representation}, min/max envelope
      {#if data.caveats.length > 0}&nbsp;· caveats: {data.caveats.join(", ")}{/if}
    </footer>
  {/if}
</section>
