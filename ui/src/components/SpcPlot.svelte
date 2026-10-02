<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { baselineSpan, DECIDING, markTip, nearestMark, spcLegend, toSpcUplot, violationMarks, type SpcMark } from "../chart/spc";
  import { seriesName } from "../chart/toUplot";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { SpcPanelData } from "../lib/api";

  let { data, series, width, height = 220, onRendered }: {
    data: SpcPanelData; series: SpcPanelData["series"][number]; width: number; height?: number;
    onRendered: (ms: number, points: number) => void;
  } = $props();

  let el = $state<HTMLDivElement | null>(null);
  let wrap = $state<HTMLDivElement | null>(null);
  /** supplementary run rules (2 of 3, 4 of 5, 8 in a row): drawn hollow, can be hidden */
  let supplementary = $state(true);
  let tip = $state<{ x: number; y: number; text: string; flip: boolean } | null>(null);
  const VIOLATION = "#D55E00";
  const marks = $derived(violationMarks(series, supplementary));
  const hasSupplementary = $derived((series.violations ?? []).some((v) => !v.rules.some((r) => DECIDING.has(r))));
  // read by the draw/cursor hooks, so toggling only redraws (the plot is not re-created)
  let drawn: SpcMark[] = [];
  let plot: uPlot | null = null;

  $effect(() => {
    drawn = marks;
    tip = null;
    plot?.redraw(false);
  });

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toSpcUplot(series, data.effective_step_ms);
    const xs = m.data[0] as number[];
    const step = data.effective_step_ms;
    const hidden = { stroke: "transparent", width: 0, points: { show: false } };
    const fills = [
      mode === "dark" ? "rgba(140,140,140,0.18)" : "rgba(120,120,120,0.12)",
      ...Array(3).fill(mode === "dark" ? "rgba(170,170,170,0.30)" : "rgba(80,80,80,0.22)"),
    ];
    const u = new uPlot(
      {
        width, height,
        series: [
          {},
          { label: seriesName(series.labels), stroke: "#0072B2", width: 1.25 },
          { label: "centre", stroke, width: 1, dash: [6, 3], points: { show: false } },
          { label: "−3σ", stroke: grid, width: 1, points: { show: false } },
          { label: "+3σ", stroke: grid, width: 1, points: { show: false } },
          { label: "+3σ 99% lo", ...hidden }, { label: "+3σ 99% hi", ...hidden },
          { label: "−3σ 99% lo", ...hidden }, { label: "−3σ 99% hi", ...hidden },
          { label: "centre 99% lo", ...hidden }, { label: "centre 99% hi", ...hidden },
        ],
        bands: m.bands.map((b, k) => ({ ...b, fill: fills[k] })),
        legend: { show: false },
        axes: [
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
        ],
        cursor: { drag: { x: false, y: false }, points: { show: false } },
        hooks: {
          drawClear: [
            (p: uPlot) => {
              if (data.baseline.kind === "reference") return; // a separate earlier window: not plotted
              const span = xs.length ? baselineSpan(data.baseline, xs[0], xs[xs.length - 1]) : null;
              if (!span) return;
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              const x0 = p.valToPos(span[0], "x", true), x1 = p.valToPos(span[1], "x", true);
              c.save();
              c.fillStyle = mode === "dark" ? "rgba(86,180,233,0.10)" : "rgba(86,180,233,0.12)";
              c.fillRect(x0, p.bbox.top, x1 - x0, p.bbox.height);
              c.fillStyle = stroke; c.font = `${10 * dpr}px sans-serif`;
              c.fillText("baseline", x0 + 4 * dpr, p.bbox.top + 12 * dpr);
              c.restore();
            },
          ],
          draw: [
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              c.lineWidth = 1.5 * dpr;
              c.strokeStyle = VIOLATION; c.fillStyle = VIOLATION;
              for (const mk of drawn) {
                const x = p.valToPos(mk.x, "x", true), y = p.valToPos(mk.y, "y", true);
                c.beginPath(); c.arc(x, y, 3.5 * dpr, 0, 2 * Math.PI);
                if (mk.deciding) c.fill(); else c.stroke();
              }
              c.restore();
            },
          ],
          setCursor: [
            (p: uPlot) => {
              const { left, top } = p.cursor;
              const box = wrap;
              if (left == null || top == null || left < 0 || !box) { tip = null; return; }
              const hit = nearestMark(drawn, left, top, (mk) => [p.valToPos(mk.x, "x"), p.valToPos(mk.y, "y")]);
              if (!hit) { tip = null; return; }
              const o = p.over.getBoundingClientRect(), w = box.getBoundingClientRect();
              const x = o.left - w.left + left, y = o.top - w.top + top;
              const flip = x > w.width / 2;
              tip = { x: flip ? x - 12 : x + 12, y: y + 12, text: markTip(hit, step), flip };
            },
          ],
        },
      },
      m.data, host,
    );
    plot = u;
    onRendered(performance.now() - t0, xs.length);
    return () => { plot = null; u.destroy(); };
  });
</script>

<div class="spc" data-spc-violations={marks.length} data-spc-mode={series.mode} data-spc-seasonal={series.seasonal ?? "none"}>
  <div class="verdict">{seriesName(series.labels)}: <b>{series.verdict.replace("_", " ")}</b>{series.also.length ? ` (also ${series.also.join(", ").replaceAll("_", " ")})` : ""}</div>
  <div class="plot" bind:this={wrap} role="presentation" onmouseleave={() => (tip = null)}>
    <div bind:this={el}></div>
    {#if tip}
      <div class="spc-tip" class:flip={tip.flip} data-spc-tip style="left:{tip.x}px;top:{tip.y}px">{tip.text}</div>
    {/if}
  </div>
  <div class="legend">
    {spcLegend(series, data.baseline, supplementary)}
    {#if hasSupplementary}
      <label class="supp"><input type="checkbox" bind:checked={supplementary} data-spc-supplementary /> supplementary run rules</label>
    {/if}
  </div>
</div>

<style>
  .verdict { font-size: 0.85em; margin: 2px 0; }
  .plot { position: relative; }
  .spc-tip {
    position: absolute; white-space: pre; font-size: 11px; background: var(--fg); color: var(--bg);
    padding: 4px 6px; border-radius: 4px; pointer-events: none; z-index: 5;
  }
  .spc-tip.flip { transform: translateX(-100%); }
  .supp { margin-left: 6px; white-space: nowrap; }
</style>
