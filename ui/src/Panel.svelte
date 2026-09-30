<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { closePanel, fetchPanelData, reportRender, type Panel, type PanelData } from "./lib/api";
  import { toUplot } from "./chart/toUplot";
  import { measureFirstDraw } from "./chart/measureDraw";

  let { panel }: { panel: Panel } = $props();

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
    const model = toUplot(data.series, {
      start: data.dataset.start_ms, end: data.dataset.end_ms, step: data.effective_step_ms,
    });
    const width = el.clientWidth || 800;
    const unit = data.panel.spec.y.unit;
    const plot = measureFirstDraw(
      (onDraw) =>
        new uPlot(
          {
            width, height: 260, series: model.series, bands: model.bands,
            tzDate: (ts: number) => uPlot.tzDate(new Date(ts * 1e3), "Etc/UTC"),
            scales: { x: { time: true } },
            axes: [{}, { label: unit ?? "value (unit unknown)" }],
            hooks: { draw: [onDraw] },
          },
          model.data, el,
        ),
      (ms) => {
        reportRender({ panel_id: panel.id, render_ms: ms, points: model.points, width_px: width })
          .then((r) => (render = { ms, exceeded: r.budget_exceeded }))
          .catch((e) => (error = String(e)));
      },
    );
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
    {#if panel.answered_by}<span class="answered">answered → {panel.answered_by}</span>{/if}
    <button class="close" type="button" aria-label="Close panel" onclick={close}>×</button>
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
