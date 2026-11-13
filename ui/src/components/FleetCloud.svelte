<script lang="ts">
  import { FLEET_HUE, MUTED_LINE } from "../chart/fleet";
  import { cloudGrid, cloudLegendText, cloudTip, type CloudGrid } from "../chart/fleetCloud";
  import { RUG_GAP, drawRug, hitRug, rugCells, rugHeight, rugHint, type RugCell } from "../chart/rug";
  import { setupCanvas } from "../chart/canvas";
  import { fmtTimeZ } from "../lib/format";
  import type { FleetPanelData } from "../lib/api";
  import { theme } from "../lib/theme.svelte";
  import CompactText from "./panel/CompactText.svelte";

  let { data, width, height, compact = false, onRendered }: {
    data: FleetPanelData; width: number; height: number;
    /** simple mode: dense captions as one line (CompactText) */
    compact?: boolean;
    onRendered: (ms: number, cells: number, heightPx: number) => void;
  } = $props();

  const cloud = $derived(data.cloud);
  const AXIS_LEFT = 56, AXIS_BOTTOM = 18;
  const plotW = $derived(Math.max(60, width - AXIS_LEFT));
  const plotH = $derived(Math.max(80, height - AXIS_BOTTOM));
  const rugRows = $derived(cloud?.rug ? 1 : 0);
  let canvas = $state<HTMLCanvasElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  let grid: CloudGrid | null = null;
  let cells: RugCell[] = [];

  $effect(() => {
    const el = canvas, c = cloud;
    if (!el || !c) return;
    const t0 = performance.now();
    const dark = theme.effective === "dark";
    const hue = FLEET_HUE[dark ? "dark" : "light"];
    const muted = MUTED_LINE[dark ? "dark" : "light"];
    const W = plotW, H = plotH, rugH = rugHeight(rugRows);
    const ctx = setupCanvas(el, AXIS_LEFT + W, H + RUG_GAP + rugH + AXIS_BOTTOM);
    if (!ctx) return;
    const g = cloudGrid(c, Math.floor(W / 2)); // >= 2 px per column
    grid = g;
    const [fr, fg, fb] = [1, 3, 5].map((i) => parseInt(hue.fill.slice(i, i + 2), 16));
    ctx.save();
    ctx.translate(AXIS_LEFT, 0);
    const cw = W / g.cols, bh = H / g.bins;
    // opacity is the share itself against the densest cell, so equal shares look equal across columns
    for (let k = 0; k < g.cols; k++) {
      for (let b = 0; b < g.bins; b++) {
        const s = g.share[k * g.bins + b];
        if (s <= 0) continue;
        ctx.fillStyle = `rgba(${fr},${fg},${fb},${(0.08 + 0.92 * (s / g.max)).toFixed(3)})`;
        ctx.fillRect(k * cw, H - (b + 1) * bh, Math.ceil(cw), Math.ceil(bh));
      }
    }
    const lo = c.edges[0], hi = c.edges.at(-1)!;
    const yOf = (v: number) => (c.log ? H * (1 - (Math.log(v) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) : H * (1 - (v - lo) / (hi - lo)));
    ctx.strokeStyle = hue.line; ctx.lineWidth = 1.5; ctx.beginPath();
    let pen = false;
    data.band.median.forEach((v, j) => {
      if (v === null) { pen = false; return; }
      const x = ((j + 0.5) / data.ts.length) * W, y = yOf(v);
      if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      pen = true;
    });
    ctx.stroke();
    ctx.fillStyle = muted; ctx.strokeStyle = muted; ctx.font = "11px sans-serif"; ctx.textBaseline = "top";
    const ticks = Math.max(2, Math.min(6, Math.floor(W / 110)));
    for (let i = 0; i <= ticks; i++) {
      const f = i / ticks, ti = Math.min(data.ts.length - 1, Math.round(f * (data.ts.length - 1)));
      ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
      ctx.fillRect(Math.min(W - 1, f * W), H + RUG_GAP + rugH, 1, 3);
      ctx.fillText(fmtTimeZ(data.ts[ti]), f * W, H + RUG_GAP + rugH + 4);
    }
    if (c.rug) {
      ctx.save();
      ctx.translate(0, H + RUG_GAP);
      const t = data.ts, span = data.effective_step_ms;
      cells = rugCells([c.rug], span, (ms) => ((ms - (t[0] - span)) / (span * t.length)) * W);
      drawRug(ctx, cells, { tint: () => `rgba(${fr},${fg},${fb},0.45)`, grey: muted, line: muted });
      ctx.restore();
    }
    ctx.restore();
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(String(Number(hi.toPrecision(3))), AXIS_LEFT - 4, 6);
    ctx.fillText(String(Number(lo.toPrecision(3))), AXIS_LEFT - 4, H - 6);
    onRendered(performance.now() - t0, g.cols * g.bins, H + RUG_GAP + rugH + AXIS_BOTTOM);
  });

  function onMove(e: MouseEvent) {
    const c = cloud, g = grid;
    if (!c || !g) return;
    const x = e.offsetX - AXIS_LEFT, y = e.offsetY;
    const rugY = y - plotH - RUG_GAP;
    const hit = c.rug && rugY >= 0 ? hitRug(cells, x, rugY) : null;
    if (hit && c.rug) {
      const reasons = `${c.rug.reporting[hit.i]} of ${c.rug.alive[hit.i]} members reporting`;
      tip = { x: e.offsetX + 12, y: e.offsetY + 12, text: `${rugHint(hit, c.rug, data.effective_step_ms, "all members", true)}\n${reasons}` };
      return;
    }
    if (x < 0 || x >= plotW || y < 0 || y >= plotH) { tip = null; return; }
    const j = Math.min(data.ts.length - 1, Math.floor((x / plotW) * data.ts.length));
    const b = Math.min(g.bins - 1, Math.floor(((plotH - y) / plotH) * g.bins));
    tip = { x: e.offsetX + 12, y: e.offsetY + 12, text: cloudTip(data, c, j, b, fmtTimeZ(data.ts[j])) };
  }
</script>

{#if cloud}
  <div class="fc" data-fleet-cloud data-fleet-cloud-silent={cloud.silent_members.length}>
    <div class="wrap">
      <canvas bind:this={canvas} onmousemove={onMove} onmouseleave={() => (tip = null)}
        aria-label="Density cloud of the members' values over time, with the group's coverage strip"></canvas>
      {#if tip}<div class="tip" style:left="{tip.x}px" style:top="{tip.y}px">{tip.text}</div>{/if}
    </div>
    <div class="legend"><CompactText text={cloudLegendText(cloud, data.members)} {compact} /></div>
  </div>
{:else}
  <div class="legend" data-fleet-cloud-missing>no density cloud in this payload</div>
{/if}

<style>
  .wrap { position: relative; }
  .tip { position: absolute; pointer-events: none; background: var(--bg, #fff); color: var(--fg, #222); border: 1px solid var(--muted, #888);
    font-size: 0.75em; padding: 2px 6px; white-space: pre; z-index: 2; }
  .legend { font-size: 0.8em; margin: 2px 0; }
</style>
