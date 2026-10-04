<script lang="ts">
  import { seriesName, seriesPalette } from "../chart/toUplot";
  import { theme } from "../lib/theme.svelte";
  import {
    bars, ecdf, exactWindow, fractionOver, maxEcdfGapAtEdges, quantileAxis, quantileBoxes, survival, type BarMode,
  } from "../chart/distribution";
  import { qLabel } from "../chart/percentiles";
  import { setupCanvas } from "../chart/canvas";
  import { cellSpan, minSamples, valueAt, valueAxis } from "../chart/heatmap";
  import { fmtValue, valueTicks } from "../chart/axis";
  import { fmtRange } from "../lib/format";
  import type { HistogramPanelData, WindowHist } from "../lib/api";

  let { data, series, width, unit, onRendered }: {
    data: HistogramPanelData;
    series: HistogramPanelData["series"][number];
    width: number;
    unit: string | null;
    onRendered: (ms: number, points: number) => void;
  } = $props();
  // per-theme series colours: the light theme needs darker variants for 3:1 contrast
  const palette = $derived(seriesPalette(theme.effective));

  const HEIGHT = 220;
  const AXIS_LEFT = 40;
  const AXIS_BOTTOM = 28;
  let canvas = $state<HTMLCanvasElement | null>(null);
  type View = "histogram" | "ecdf" | "quantile_curve" | "ccdf";
  let view = $state<View>("histogram");
  let qMode = $state<"linear" | "nines">("nines");
  let yMode = $state<"auto" | "log" | "linear">("auto");
  let pinned = $state<number | null>(null); // CCDF threshold pinned by a click
  let thresh = $state<number | null>(null); // histogram/ECDF threshold: drag to read below / in bucket / above
  let dragging = $state(false);
  let userMode = $state<BarMode | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let spans: { lo: number | null; hi: number | null; x0: number; x1: number }[][] = [];

  const windows = $derived(series.windows);
  const multi = $derived(windows.length > 1);
  // counts are not comparable across windows with different n: default to share per decade
  const mode = $derived<BarMode>(multi && (userMode === null || userMode === "count") ? "density" : (userMode ?? "count"));
  const nMin = $derived(data.dataset.n_min ?? 0);
  const plotW = $derived(Math.max(10, width - AXIS_LEFT));
  const plotH = HEIGHT - AXIS_BOTTOM;


  const exact = $derived(windows.map(exactWindow));
  const nMax = $derived(Math.max(10, ...exact.map((w) => w.n)));
  const logTop = $derived(Math.log10(nMax));
  const qAxisFor = () => quantileAxis(qMode, nMax, plotW);
  const edgesLoHi = () => ({ lo: exact.flatMap((w) => w.lo), hi: exact.flatMap((w) => w.hi) });
  const ccdfMin = $derived(Math.min(1, ...exact.filter((w) => w.n > 0).map((w) => 1 / w.n)));
  const yS = (f: number) => {
    const l = Math.log10(Math.max(f, ccdfMin));
    return plotH - 4 - ((l - Math.log10(ccdfMin)) / (0 - Math.log10(ccdfMin) || 1)) * (plotH - 8);
  };

  /** Quantile function (boxes over q) and CCDF (log-log). Source buckets only: boxes and exact edge dots. */
  function drawCumulative(ctx: CanvasRenderingContext2D, v: (n: string) => string): number {
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, plotH); ctx.lineTo(plotW, plotH); ctx.stroke();
    const { lo, hi } = edgesLoHi();
    let count = 0;
    ctx.fillStyle = v("--muted"); ctx.font = "10px sans-serif";
    if (view === "quantile_curve") {
      const qa = qAxisFor();
      const va = valueAxis(lo, hi, plotH, yMode === "auto" ? "auto" : yMode);
      exact.forEach((w, k) => {
        const color = palette[k % palette.length];
        const { boxes, qMax } = quantileBoxes(w);
        for (const b of boxes) {
          const [p0, p1] = cellSpan(va, b.lo, b.hi);
          const x0 = qa.pos(b.q0), x1 = qa.pos(b.q1);
          ctx.fillStyle = color; ctx.strokeStyle = color;
          ctx.globalAlpha = b.faded ? 0.08 : 0.25;
          ctx.fillRect(x0, plotH - p1, Math.max(x1 - x0, 1), Math.max(p1 - p0, 1));
          ctx.globalAlpha = b.faded ? 0.3 : 1;
          ctx.strokeRect(x0 + 0.5, plotH - p1, Math.max(x1 - x0 - 1, 1), Math.max(p1 - p0, 1));
          ctx.globalAlpha = 1;
          count++;
        }
        if (w.n > 0) {
          ctx.setLineDash([4, 3]); ctx.strokeStyle = color;
          ctx.beginPath(); ctx.moveTo(qa.pos(qMax), 0); ctx.lineTo(qa.pos(qMax), plotH); ctx.stroke(); ctx.setLineDash([]);
        }
      });
      ctx.fillStyle = v("--muted"); ctx.textAlign = "center"; ctx.textBaseline = "top";
      for (const q of qa.ticks) ctx.fillText(qLabel(q), qa.pos(q), plotH + 3);
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      for (const t of valueTicks(va)) ctx.fillText(fmtValue(t, unit), -4, plotH - va.pos(t));
    } else {
      const ha = valueAxis(lo, hi, plotW, "log");
      exact.forEach((w, k) => {
        const color = palette[k % palette.length];
        const steps = survival(w);
        steps.forEach((st, i) => {
          if (!(st.s0 > 0)) return;
          const [x0, x1] = cellSpan(ha, st.lo, st.hi);
          const yTop = yS(st.s0), yBot = st.s1 > 0 ? yS(st.s1) : plotH - 4;
          ctx.fillStyle = color; ctx.strokeStyle = color;
          ctx.globalAlpha = st.faded ? 0.3 : 0.15;
          ctx.fillRect(x0, yTop, Math.max(x1 - x0, 1), Math.max(yBot - yTop, 1));
          ctx.globalAlpha = st.faded ? 0.3 : 1;
          if (st.s1 > 0) {
            ctx.beginPath(); ctx.arc(x1, yS(st.s1), 2.5, 0, 2 * Math.PI); ctx.fill(); // exact at the edge
            const next = steps[i + 1];
            const nx = next ? cellSpan(ha, next.lo, next.hi)[0] : plotW;
            ctx.beginPath(); ctx.moveTo(x1, yS(st.s1)); ctx.lineTo(nx, yS(st.s1)); ctx.stroke();
          }
          ctx.globalAlpha = 1;
          count++;
        });
      });
      if (pinned !== null) {
        ctx.strokeStyle = v("--fg"); ctx.setLineDash([3, 3]);
        const x = ha.pos(pinned);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, plotH); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.fillStyle = v("--muted"); ctx.textAlign = "center"; ctx.textBaseline = "top";
      for (const t of valueTicks(ha)) ctx.fillText(fmtValue(t, unit), ha.pos(t), plotH + 3);
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      for (let e = 0; e >= Math.log10(ccdfMin) - 1e-9; e--) ctx.fillText(e === 0 ? "1" : `1e${e}`, -4, yS(10 ** e));
    }
    ctx.restore();
    return count;
  }

  const over = (x: number): string[] =>
    exact.map((w, k) => {
      const r = fractionOver(w, x);
      const name = w.label || `range ${k + 1}`;
      if (r === null) return `${name}: no data`;
      if (r.exact) return `${name}: P(X > ${fmtValue(x, unit)}) = ${(100 * r.f).toPrecision(3)}% (${Number(r.count.toPrecision(4))} of ${Number(w.n.toPrecision(4))})`;
      const edge = (e: number | null, inf: string) => (e === null ? inf : fmtValue(e, unit));
      return `${name}: between ${(100 * r.min).toPrecision(3)}% and ${(100 * r.max).toPrecision(3)}% (inside (${edge(r.lo, "−∞")}, ${edge(r.hi, "+∞")}])`;
    });


  /** below / in the straddled bucket / above, per window: the middle number is the bucket-edge uncertainty. */
  const readout = (x: number): string[] =>
    exact.map((w, k) => {
      const r = fractionOver(w, x);
      const name = w.label || `range ${k + 1}`;
      if (r === null) return `${name}: no data`;
      const pct = (f: number) => `${(100 * f).toPrecision(3)}%`;
      if (r.exact) return `${name} at ${fmtValue(x, unit)}: below ${pct(1 - r.f)} · above ${pct(r.f)} (edge: exact)`;
      return `${name} at ${fmtValue(x, unit)}: below ${pct(1 - r.max)} · in bucket ${pct(r.max - r.min)} · above ${pct(r.min)}`;
    });
  const histAxis = () => {
    const { lo, hi } = edgesLoHi();
    return valueAxis(lo, hi, plotW);
  };

  const gap = $derived(multi ? maxEcdfGapAtEdges(exactWindow(windows[0]), exactWindow(windows[1])) : null);

  $effect(() => {
    view = data.mark as View;
  });

  $effect(() => {
    const el = canvas;
    if (!el) return;
    const t0 = performance.now();
    const ctx = setupCanvas(el, width, HEIGHT);
    if (!ctx) return;
    if (view === "quantile_curve" || view === "ccdf") {
      const css = getComputedStyle(el);
      const points = drawCumulative(ctx, (name) => css.getPropertyValue(name).trim());
      onRendered(performance.now() - t0, points);
      return;
    }
    const css = getComputedStyle(el);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const lo = windows.flatMap((w) => w.lo);
    const hi = windows.flatMap((w) => w.hi);
    const axis = valueAxis(lo, hi, plotW);
    const barsByWindow = windows.map((w) => bars(w, mode));
    const yMax =
      view === "ecdf"
        ? 1
        : Math.max(1e-12, ...barsByWindow.flatMap((b) => b.map((x) => x.y ?? 0)));
    const y = (f: number) => plotH - (f / yMax) * (plotH - 4);
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    ctx.strokeStyle = v("--grid");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, plotH);
    ctx.lineTo(plotW, plotH);
    ctx.stroke();
    let points = 0;
    spans = [];
    windows.forEach((w, k) => {
      const color = palette[k % palette.length];
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1.5;
      if (view === "histogram") {
        const list = barsByWindow[k];
        spans[k] = list.map((b) => {
          const [x0, x1] = cellSpan(axis, b.lo, b.hi);
          return { lo: b.lo, hi: b.hi, x0, x1 };
        });
        list.forEach((b, i) => {
          if (b.y === null) return;
          points++;
          const { x0, x1 } = spans[k][i];
          const top = y(b.y);
          if (k === 0) {
            ctx.globalAlpha = 0.25;
            ctx.fillRect(x0, top, Math.max(x1 - x0, 1), plotH - top);
            ctx.globalAlpha = 1;
          }
          ctx.strokeRect(x0 + 0.5, top, Math.max(x1 - x0 - 1, 1), plotH - top);
        });
      } else {
        const steps = ecdf(exactWindow(w));
        spans[k] = steps.map((s) => {
          const [x0, x1] = cellSpan(axis, s.lo, s.hi);
          return { lo: s.lo, hi: s.hi, x0, x1 };
        });
        steps.forEach((s, i) => {
          points++;
          const { x0, x1 } = spans[k][i];
          // inside a bucket the ECDF is only known to lie in [F(lo), F(hi)]: a box, never a line
          ctx.globalAlpha = 0.15;
          ctx.fillRect(x0, y(s.f1), Math.max(x1 - x0, 1), y(s.f0) - y(s.f1));
          ctx.globalAlpha = 1;
          ctx.beginPath();
          ctx.arc(x1, y(s.f1), 2.5, 0, 2 * Math.PI); // exact at the bucket edge
          ctx.fill();
          ctx.beginPath();
          ctx.moveTo(x1, y(s.f1));
          ctx.lineTo(spans[k][i + 1]?.x0 ?? plotW, y(s.f1)); // exact until the next bucket starts
          ctx.stroke();
        });
      }
    });
    // axes
    ctx.fillStyle = v("--muted");
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const t of valueTicks(axis)) ctx.fillText(fmtValue(t, unit), axis.pos(t), plotH + 3);
    if (axis.over) ctx.fillText(`> ${fmtValue(axis.max, unit)}`, plotW - 14, plotH + 14);
    if (axis.under) ctx.fillText(`≤ ${fmtValue(axis.min, unit)}`, 14, plotH + 14);
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const yTicks = view === "ecdf" ? [0, 0.5, 1] : [0, yMax / 2, yMax];
    for (const t of yTicks) {
      const label = view === "ecdf" ? `${Math.round(t * 100)}%` : mode === "count" ? String(Number(t.toPrecision(3))) : t.toPrecision(2);
      ctx.fillText(label, -4, y(t));
    }
    if (thresh !== null) {
      ctx.strokeStyle = v("--fg"); ctx.setLineDash([3, 3]);
      const tx = axis.pos(thresh);
      ctx.beginPath(); ctx.moveTo(tx, 0); ctx.lineTo(tx, plotH); ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.restore();
    onRendered(performance.now() - t0, points);
  });

  const local = (e: MouseEvent) => {
    const b = canvas!.getBoundingClientRect();
    return { x: e.clientX - b.left - AXIS_LEFT, y: e.clientY - b.top };
  };

  const share = (w: WindowHist, i: number) => {
    const t = w.c.reduce((a, b) => a + b, 0);
    return t > 0 ? (100 * w.c[i]) / t : 0;
  };

  function move(e: MouseEvent) {
    const p = local(e);
    if (view === "ccdf") {
      const { lo, hi } = edgesLoHi();
      const ha = valueAxis(lo, hi, plotW, "log");
      let x = valueAt(ha, p.x);
      // snap to a source edge within 4 px: the read-off is then exact
      const edge = [...new Set([...lo, ...hi])].filter((e): e is number => e !== null && e > 0)
        .find((e) => Math.abs(ha.pos(e) - p.x) <= 4);
      if (edge !== undefined) x = edge;
      tip = { x: p.x + AXIS_LEFT + 8, y: p.y + 8, text: over(x).join("\n") };
      return;
    }
    if (view === "quantile_curve") {
      const q = qMode === "linear" ? p.x / plotW : 1 - 10 ** (-(p.x / plotW) * logTop);
      if (!(q > 0 && q < 1)) { tip = null; return; }
      const lines = exact.map((w, k) => {
        const { boxes } = quantileBoxes(w);
        const b = boxes.find((x) => q > x.q0 && q <= x.q1);
        const name = w.label || `range ${k + 1}`;
        if (!b) return `${name}: no data`;
        const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
        return `${name}: ${qLabel(q)} in (${edge(b.lo, "−∞")}, ${edge(b.hi, "+∞")}]${b.faded ? ` · n ${Number(w.n.toPrecision(3))} < ${minSamples(q)}` : ""}`;
      });
      tip = { x: p.x + AXIS_LEFT + 8, y: p.y + 8, text: lines.join("\n") };
      return;
    }
    const lines: string[] = [];
    windows.forEach((w, k) => {
      const list = spans[k] ?? [];
      const bi = list.findIndex((s) => p.x >= s.x0 && p.x < Math.max(s.x1, s.x0 + 1));
      if (bi < 0) return;
      // bars()/ecdf() list buckets ordered by upper edge: find the matching source bucket
      const s = list[bi];
      const idx = w.hi.findIndex((h, i) => h === s.hi && w.lo[i] === s.lo);
      if (idx < 0) return;
      const edge = (x: number | null, inf: string) => (x === null ? inf : fmtValue(x, unit));
      lines.push(
        `${w.label || `range ${k + 1}`}: (${edge(s.lo, "−∞")}, ${edge(s.hi, "+∞")}] · count ${Number(w.c[idx].toPrecision(4))} · ${share(w, idx).toPrecision(3)}%`,
      );
    });
    tip = lines.length ? { x: p.x + AXIS_LEFT + 8, y: p.y + 8, text: lines.join("\n") } : null;
  }
</script>

<div class="facet">
  <div class="facet-label">{seriesName(series.labels)}</div>
  <div class="dist-controls">
    <button type="button" class:on={view === "histogram"} onclick={() => (view = "histogram")}>histogram</button>
    <button type="button" class:on={view === "ecdf"} onclick={() => (view = "ecdf")}>ECDF</button>
    <button type="button" class:on={view === "quantile_curve"} onclick={() => (view = "quantile_curve")}>quantile</button>
    <button type="button" class:on={view === "ccdf"} onclick={() => (view = "ccdf")}>CCDF</button>
    {#if view === "quantile_curve"}
      {#each ["linear", "nines"] as m (m)}
        <button type="button" class:on={qMode === m} onclick={() => (qMode = m as "linear" | "nines")}>x: {m}</button>
      {/each}
      {#each ["auto", "log", "linear"] as m (m)}
        <button type="button" class:on={yMode === m} onclick={() => (yMode = m as "auto" | "log" | "linear")}>y: {m}</button>
      {/each}
    {/if}
    {#if view === "histogram"}
      {#each ["count", "share", "density"] as m (m)}
        <button
          type="button"
          class:on={mode === m}
          disabled={m === "count" && multi}
          title={m === "count" && multi ? "time ranges have different n: counts are not comparable" : ""}
          onclick={() => (userMode = m as BarMode)}>{m}</button
        >
      {/each}
    {/if}
  </div>
  <canvas
    bind:this={canvas}
    class="distribution"
    onmousedown={(e) => {
      if (view !== "histogram" && view !== "ecdf") return;
      dragging = true;
      thresh = valueAt(histAxis(), local(e).x);
    }}
    onmouseup={() => (dragging = false)}
    onmousemove={(e) => {
      if (dragging && (view === "histogram" || view === "ecdf")) thresh = valueAt(histAxis(), local(e).x);
      move(e);
    }}
    onmouseleave={() => { tip = null; dragging = false; }}
    onclick={(e) => {
      if (view !== "ccdf") return;
      const { lo, hi } = edgesLoHi();
      const ha = valueAxis(lo, hi, plotW, "log");
      const x = valueAt(ha, local(e).x);
      pinned = pinned === null ? x : null;
    }}
  ></canvas>
  {#if tip}<div class="tip" style="left: {tip.x}px; top: {tip.y}px; white-space: pre">{tip.text}</div>{/if}
  <div class="legend">
    {#each windows as w, k (k)}
      <div>
        <span class="sw" style="background: {palette[k % palette.length]}"></span>
        {w.label || `range ${k + 1}`} {fmtRange(w.start_ms, w.end_ms)} · n = {Number(w.n.toPrecision(4))} ({w.columns} steps){#if w.n > 0 && w.n < nMin}
          · low n{/if}
      </div>
    {/each}
    {#if gap}
      <div>max |ΔF| at bucket edges = {gap.gap.toFixed(2)} at {fmtValue(gap.at, unit)} (lower bound, edges where both ECDFs are exact)</div>
    {/if}
    {#if (view === "histogram" || view === "ecdf") && thresh !== null}
      {#each readout(thresh) as line (line)}<div>threshold: {line}</div>{/each}
      <div><button type="button" onclick={() => (thresh = null)}>clear threshold</button></div>
    {:else if view === "histogram" || view === "ecdf"}
      <div class="hint">drag on the plot to read the share below / in-bucket / above a threshold</div>
    {/if}
    {#if view === "ccdf" && pinned !== null}
      {#each over(pinned) as line (line)}<div>pinned: {line}</div>{/each}
    {/if}
    {#if data.value_merge > 1 && (view === "histogram")}<div>{data.value_merge} source buckets per bar</div>{/if}
  </div>
</div>

<style>
  .facet { position: relative; }
  .facet-label { font-size: 11px; color: var(--muted); }
  .dist-controls { display: flex; gap: 4px; margin: 2px 0; }
  .dist-controls button { font-size: 11px; padding: 1px 6px; border: 1px solid var(--border); background: none; color: var(--muted); border-radius: 4px; cursor: pointer; }
  .dist-controls button.on { color: var(--fg); border-color: var(--fg); }
  .dist-controls button:disabled { opacity: 0.4; cursor: not-allowed; }
  .sw { display: inline-block; width: 10px; height: 10px; margin-right: 4px; border-radius: 2px; }
  .tip { position: absolute; pointer-events: none; background: var(--bg); border: 1px solid var(--border); padding: 2px 6px; font-size: 11px; z-index: 5; }
</style>
