<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import { fmtPeriod, periodTicks } from "../chart/period";
  import { layoutPeakLabels, limitZones, peakMarks, toSpectrumUplot } from "../chart/spectrum";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { SpectrumPanelData } from "../lib/api";

  let { data, width, onRendered }: { data: SpectrumPanelData; width: number; onRendered: (ms: number, points: number) => void } = $props();

  let el = $state<HTMLDivElement | null>(null);
  const significant = $derived(data.series.reduce((n, s) => n + s.peaks.filter((p) => p.significant).length, 0));
  const redNoise = $derived(data.caveats.includes("red_noise"));

  $effect(() => {
    const host = el;
    if (!host) return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const m = toSpectrumUplot(data);
    const [xmin, xmax] = m.xRange;
    const zones = limitZones(data.limits, xmin, xmax);
    const marks = data.series.flatMap((s, k) => peakMarks(s).map((p, i) => ({ ...p, k, id: `${k}:${i}` })));
    const u = new uPlot(
      {
        width, height: 240, series: m.series,
        scales: { x: { time: false, distr: 3, log: 10, range: () => [xmin, xmax] }, y: { range: [0, 1] } },
        axes: [
          { stroke, grid: { stroke: grid }, ticks: { stroke: grid }, splits: () => periodTicks(xmin, xmax), values: (_u: uPlot, ts: (number | null)[]) => ts.map((t) => (t == null ? "" : fmtPeriod(t))) },
          { label: "share of variance explained", stroke, grid: { stroke: grid }, ticks: { stroke: grid } },
        ],
        cursor: { drag: { x: false, y: false } },
        hooks: {
          draw: [
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              c.font = `${10 * dpr}px sans-serif`;
              for (const z of zones) {
                const x0 = p.valToPos(z.from, "x", true), x1 = p.valToPos(z.to, "x", true);
                c.strokeStyle = grid; c.lineWidth = 1;
                c.save(); c.beginPath(); c.rect(x0, p.bbox.top, x1 - x0, p.bbox.height); c.clip(); c.beginPath();
                for (let d = -p.bbox.height; d < x1 - x0; d += 8 * dpr) { c.moveTo(x0 + d, p.bbox.top + p.bbox.height); c.lineTo(x0 + d + p.bbox.height, p.bbox.top); }
                c.stroke(); c.restore();
                c.fillStyle = stroke; c.fillText(z.text, x0 + 4 * dpr, p.bbox.top + 12 * dpr);
              }
              const toPx = (x: number) => p.valToPos(x, "x", true);
              const widthOf = (text: string) => c.measureText(text).width;
              const placements = new Map(layoutPeakLabels(marks, toPx, widthOf).map((pl) => [pl.id, pl]));
              for (const mk of marks) {
                const y = p.valToPos(mk.y, "y", true), xa = p.valToPos(mk.lo, "x", true), xb = p.valToPos(mk.hi, "x", true), xc = p.valToPos(mk.x, "x", true);
                c.strokeStyle = stroke; c.lineWidth = 2 * dpr;
                c.beginPath(); c.moveTo(xa, y - 6 * dpr); c.lineTo(xb, y - 6 * dpr); c.stroke(); // the interval, not just the point
                const placed = placements.get(mk.id);
                const row = placed?.row ?? 0;
                c.fillStyle = stroke; c.fillText(placed?.text ?? mk.text, xc + 4 * dpr, y - 10 * dpr - row * 12 * dpr);
              }
              c.restore();
            },
          ],
        },
      },
      m.data, host,
    );
    onRendered(performance.now() - t0, m.data[0].length * data.series.length);
    return () => u.destroy();
  });
</script>

<div class="spectrum" data-spectrum-peaks={significant}>
  <div bind:this={el}></div>
  <div class="legend">
    Lomb-Scargle, linear trend removed · step {Math.round(data.effective_step_ms / 1000)}s · dashed: 1% false-alarm level (white noise) · dotted: 1% level against AR(1) red noise (significant = above both){redNoise ? "; long periods overstated by white noise (autocorrelation)" : ""} · {significant} significant peak{significant === 1 ? "" : "s"}
  </div>
</div>
