<script lang="ts">
  import { OUTLIER_COLORS, type FleetData } from "../chart/fleet";
  import {
    DIVERGING, GAP, binHeat, divergingLut, heatLegendText, heatTip, labelYs, lutIndex, rowPx, type HeatGrid,
  } from "../chart/fleetHeat";
  import { setupCanvas } from "../chart/canvas";
  import { theme } from "../lib/theme.svelte";

  let { data, width, onRendered }: {
    data: FleetData; width: number;
    onRendered: (ms: number, cells: number, heightPx: number) => void;
  } = $props();

  const heat = $derived(data.heat);
  const AXIS_LEFT = 150, AXIS_BOTTOM = 18;
  const plotW = $derived(Math.max(60, width - AXIS_LEFT));
  const rowH = $derived(rowPx(heat?.rows.length ?? 1));
  const plotH = $derived((heat?.rows.length ?? 1) * rowH);
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  const fmtTime = (ms: number) => new Date(ms).toISOString().slice(11, 16) + "Z";
  const stops = $derived(DIVERGING[theme.effective === "dark" ? "dark" : "light"].join(", "));
  let grid: HeatGrid | null = null;

  $effect(() => {
    const el = canvas, h = heat;
    if (!el || !h) return;
    const t0 = performance.now();
    const mode = theme.effective === "dark" ? "dark" : "light";
    const css = getComputedStyle(el);
    const fg = css.getPropertyValue("--fg").trim() || (mode === "dark" ? "#e6e6e6" : "#222");
    const muted = css.getPropertyValue("--muted").trim() || "#777";
    const W = plotW, H = plotH, total = AXIS_LEFT + W, totalH = H + AXIS_BOTTOM;
    const ctx = setupCanvas(el, total, totalH);
    if (!ctx) return;
    const g = binHeat(h, data.ts.length, Math.floor(W / 2)); // >= 2 px per column
    grid = g;
    // one pixel per (bin, row) into an offscreen image, then scaled up without smoothing
    const lut = divergingLut(mode);
    const img = new ImageData(g.bins, g.rows);
    for (let i = 0; i < g.rows * g.bins; i++) {
      if (g.kind[i] !== 0) continue; // transparent: gaps and absent cells are textured / blank
      const k = lutIndex(g.z[i], h.z_cap) * 3;
      img.data[i * 4] = lut[k]; img.data[i * 4 + 1] = lut[k + 1]; img.data[i * 4 + 2] = lut[k + 2]; img.data[i * 4 + 3] = 255;
    }
    const off = document.createElement("canvas");
    off.width = g.bins; off.height = g.rows;
    off.getContext("2d")!.putImageData(img, 0, 0);
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, W, H);
    // silent-while-alive runs: dots (never a colour, never "zero")
    ctx.fillStyle = muted;
    const cw = W / g.bins;
    for (let r = 0; r < g.rows; r++) {
      let b = 0;
      while (b < g.bins) {
        if (g.kind[r * g.bins + b] !== GAP) { b++; continue; }
        const s = b;
        while (b < g.bins && g.kind[r * g.bins + b] === GAP) b++;
        const x0 = s * cw, x1 = b * cw, yc = r * rowH + rowH / 2;
        for (let x = x0 + 2; x < x1; x += 4) ctx.fillRect(x - 0.75, yc - 0.75, 1.5, 1.5);
      }
    }
    // time axis
    ctx.fillStyle = muted; ctx.strokeStyle = muted; ctx.font = "11px sans-serif"; ctx.textBaseline = "top";
    const ticks = Math.max(2, Math.min(6, Math.floor(W / 110)));
    for (let i = 0; i <= ticks; i++) {
      const f = i / ticks, ti = Math.min(data.ts.length - 1, Math.round(f * (data.ts.length - 1)));
      ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
      ctx.fillRect(Math.min(W - 1, f * W), H, 1, 3);
      ctx.fillText(fmtTime(data.ts[ti]), f * W, H + 4);
    }
    ctx.restore();
    // outlier labels in the gutter: ink text, the outlier's colour as a swatch (colour never carries the text)
    const marked = h.rows.flatMap((r, i) => (r.rank !== null ? [{ r, i }] : []));
    const ys = labelYs(marked.map((m) => m.i), rowH);
    ctx.font = "11px sans-serif"; ctx.textBaseline = "middle"; ctx.textAlign = "right";
    marked.forEach(({ r, i }, j) => {
      const col = r.rank !== null && r.rank < OUTLIER_COLORS.length ? OUTLIER_COLORS[r.rank] : muted;
      ctx.fillStyle = col;
      ctx.fillRect(AXIS_LEFT - 5, i * rowH, 3, rowH); // bracket on the row itself
      ctx.fillRect(AXIS_LEFT - 144, ys[j] - 4, 8, 8);
      ctx.fillStyle = fg;
      let text = r.id;
      while (text.length > 3 && ctx.measureText(text).width > 118) text = text.slice(0, -2) + "…";
      ctx.textAlign = "left";
      ctx.fillText(text, AXIS_LEFT - 133, ys[j]);
    });
    onRendered(performance.now() - t0, g.rows * g.bins, totalH);
  });

  function onMove(e: MouseEvent) {
    const h = heat, g = grid;
    if (!h || !g) return;
    const x = e.offsetX - AXIS_LEFT, y = e.offsetY;
    if (x < 0 || x >= plotW || y < 0 || y >= plotH) { tip = null; return; }
    const r = h.rows[Math.floor(y / rowH)];
    const c = Math.min(data.ts.length - 1, Math.floor((x / plotW) * data.ts.length));
    const absent = r.first < 0 || c < r.first || c > r.last;
    const text = absent && r.z[c] == null ? `${r.id} · ${fmtTime(data.ts[c])} · not reporting (before first / after last report)` : heatTip(r, r.z[c], fmtTime(data.ts[c]));
    tip = { x: e.offsetX + 12, y: e.offsetY + 12, text };
  }
</script>

{#if heat}
  <div class="fh" data-fleet-heat data-fleet-heat-rows={heat.rows.length}>
    <div class="wrap">
      <canvas bind:this={canvas} onmousemove={onMove} onmouseleave={() => (tip = null)}
        aria-label="Member by time heatmap of deviation from the other members"></canvas>
      {#if tip}<div class="tip" style:left="{tip.x}px" style:top="{tip.y}px">{tip.text}</div>{/if}
    </div>
    <div class="bar" aria-hidden="true">
      <span>below −{heat.z_cap}</span>
      <span class="ramp" style:background="linear-gradient(to right, {stops})"></span>
      <span>above +{heat.z_cap}</span>
    </div>
    <div class="legend">{heatLegendText(heat, heat.rows.length)}</div>
  </div>
{:else}
  <div class="legend" data-fleet-heat-missing>no member × time matrix in this payload</div>
{/if}

<style>
  .wrap { position: relative; }
  .tip { position: absolute; pointer-events: none; background: var(--bg, #fff); color: var(--fg, #222); border: 1px solid var(--muted, #888);
    font-size: 0.75em; padding: 2px 6px; white-space: nowrap; z-index: 2; }
  .legend { font-size: 0.8em; margin: 2px 0; }
  .bar { display: flex; align-items: center; gap: 6px; font-size: 0.75em; margin: 4px 0 0 150px; }
  .ramp { display: inline-block; width: 160px; height: 8px; border: 1px solid var(--muted, #888); }
</style>
