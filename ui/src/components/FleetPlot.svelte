<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    FLEET_HUE, OUTLIER_COLORS, bandFills, coverageGaps, fleetAxisLabel, fleetKey, fleetLegend, nearestOutlier, outlierMarkIdx,
    outlierText, toFleetUplot,
  } from "../chart/fleet";
  import { HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { FleetPanelData } from "../lib/api";
  import FleetHeat from "./FleetHeat.svelte";
  import FleetSmallMultiples from "./FleetSmallMultiples.svelte";

  let { data, width, height = 260, range = null, unit = null, onRendered }: {
    data: FleetPanelData; width: number; height?: number; range?: [number, number] | null; unit?: string | null;
    onRendered: (ms: number, points: number, heightPx?: number) => void;
  } = $props();

  // band + lines (default), member x time heatmap, or small multiples of the top outliers
  type View = "band" | "heat" | "multiples";
  let view = $state<View>("band");
  const views: [View, string][] = [["band", "band + outliers"], ["heat", "member × time"], ["multiples", "small multiples"]];

  let el = $state<HTMLDivElement | null>(null);
  let tip = $state<{ x: number; y: number; text: string } | null>(null);
  const key = $derived(fleetKey(theme.effective === "dark"));
  const axisLabel = $derived(fleetAxisLabel(data, unit));

  $effect(() => {
    const host = el;
    if (!host || view !== "band") return;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const median = FLEET_HUE[mode === "dark" ? "dark" : "light"].line;
    const bg = getComputedStyle(host).getPropertyValue("--bg").trim() || (mode === "dark" ? "#16181d" : "#ffffff");
    const m = toFleetUplot(data);
    const gaps = coverageGaps(data);
    const dark = mode === "dark";
    const fills = bandFills(dark);
    const labelOf: Record<string, string> = { lo: "min", hi: "max", q10: "10%", q90: "90%", q25: "25%", q75: "75%" };
    let k = 0;
    const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series => {
      if (r === "median") return { label: "median", stroke: median, width: 2, points: { show: false } };
      if (r === "outlier") {
        const o = data.outliers[k];
        return { label: o.id, stroke: OUTLIER_COLORS[k++ % OUTLIER_COLORS.length], width: 1.5, points: { show: false } };
      }
      return { ...HIDDEN_SERIES, label: labelOf[r] };
    })];
    const u = new uPlot(
      {
        width, height, series,
        bands: m.bands.map((b, i) => ({ ...b, fill: fills[i] })),
        axes: plotAxes(stroke, grid, { label: data.normalise === "member" ? "× own median" : unit ?? undefined }),
        legend: { show: false },
        ...(range ? { scales: { y: { range: (): [number, number] => range } } } : {}),
        cursor: { drag: { x: false, y: false } },
        hooks: {
          setCursor: [
            (p: uPlot) => {
              const { idx, left, top } = p.cursor;
              if (idx == null || left == null || top == null || left < 0 || top < 0) { tip = null; return; }
              const yv = p.posToVal(top, "y");
              const span = Math.abs(p.posToVal(0, "y") - p.posToVal(p.bbox.height / (window.devicePixelRatio || 1), "y"));
              const o = nearestOutlier(data, idx, yv, span * 0.04);
              if (!o) { tip = null; return; }
              const v = o.values[idx] as number;
              tip = { x: left + 12, y: top + 12, text: `${o.id} · ${fmtTimeZ(data.ts[idx])} · ${Number(v.toPrecision(4))}` };
            },
          ],
          draw: [
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              // coverage strip: steps where alive members did not report (darker = more missing)
              const y0 = p.bbox.top + p.bbox.height - 4 * dpr;
              const w = Math.max(1, (p.bbox.width / Math.max(data.ts.length, 1)));
              for (const g of gaps) {
                c.fillStyle = dark ? `rgba(220,220,220,${0.15 + 0.7 * g.share})` : `rgba(60,60,60,${0.15 + 0.7 * g.share})`;
                c.fillRect(p.valToPos(g.x, "x", true) - w / 2, y0, w, 4 * dpr);
              }
              // outlier markers where a member leaves the 10-90 envelope (ringed in the background colour)
              data.outliers.forEach((_, i) => {
                c.fillStyle = OUTLIER_COLORS[i % OUTLIER_COLORS.length];
                c.strokeStyle = bg; c.lineWidth = 1.5 * dpr;
                for (const j of outlierMarkIdx(data, i)) {
                  const x = p.valToPos(data.ts[j] / 1000, "x", true), y = p.valToPos(data.outliers[i].values[j] as number, "y", true);
                  c.beginPath(); c.arc(x, y, 3 * dpr, 0, 2 * Math.PI); c.fill(); c.stroke();
                }
              });
              // outlier labels at their last drawn point
              c.font = `${11 * dpr}px sans-serif`;
              data.outliers.forEach((o, i) => {
                let j = o.values.length - 1;
                while (j >= 0 && o.values[j] === null) j--;
                if (j < 0) return;
                c.fillStyle = OUTLIER_COLORS[i % OUTLIER_COLORS.length];
                const x = p.valToPos(data.ts[j] / 1000, "x", true), y = p.valToPos(o.values[j] as number, "y", true);
                c.textAlign = "right";
                c.fillText(o.id, x - 4 * dpr, y - 4 * dpr);
              });
              c.restore();
            },
          ],
        },
      },
      m.data, host,
    );
    onRendered(performance.now() - t0, (m.data[0] as number[]).length * (m.data.length - 1), height);
    return () => u.destroy();
  });
</script>

<div class="fleet" data-fleet-members={data.members} data-fleet-outliers={data.outlier_count} data-fleet-view={view}>
  <div class="legend views" role="group" aria-label="Fleet view">
    view:
    {#each views as [v, label] (v)}
      {#if v === "band" || (v === "heat" && data.heat) || (v === "multiples" && data.outliers.length)}
        <button type="button" class:on={view === v} aria-pressed={view === v} data-fleet-view-btn={v} onclick={() => (view = v)}>{label}</button>
      {/if}
    {/each}
  </div>
  {#if view === "band"}
    <div class="encoding" data-fleet-encoding>{axisLabel}</div>
    <ul class="key" data-fleet-key aria-label="Fleet chart encoding">
      {#each key as k (k.id)}
        <li><span class="sw {k.id}" style:background={k.swatch}></span>{k.label}</li>
      {/each}
    </ul>
    <div class="wrap"><div bind:this={el}></div>{#if tip}<div class="tip" style:left="{tip.x + 50}px" style:top="{tip.y}px">{tip.text}</div>{/if}</div>
  {/if}
  {#if view === "heat"}
    <FleetHeat {data} {width} {onRendered} />
  {:else if view === "multiples"}
    <FleetSmallMultiples {data} {width} {range} {onRendered} />
  {:else}
    <div class="legend">{fleetLegend(data)}</div>
    {#if data.outliers.length}
      <ul class="outliers">
        {#each data.outliers as o, i (o.id)}
          <li><span class="swatch" style:background={OUTLIER_COLORS[i % OUTLIER_COLORS.length]}></span>{outlierText(o, fmtTimeZ)}</li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .legend { font-size: 0.8em; margin: 2px 0; }
  .views button { font-size: inherit; margin-right: 4px; }
  .views button.on { font-weight: 600; text-decoration: underline; }
  .encoding { font-size: 0.8em; font-weight: 600; margin: 2px 0; }
  .key { display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 0.75em; margin: 2px 0 4px 0; padding: 0; list-style: none; }
  .sw { display: inline-block; width: 18px; height: 9px; margin-right: 4px; vertical-align: middle; border: 1px solid var(--grid, #ccc); }
  .sw.median { height: 2px; border: 0; }
  .sw.outlier { width: 8px; height: 8px; border-radius: 50%; border: 0; }
  .wrap { position: relative; }
  .tip { position: absolute; pointer-events: none; background: var(--bg, #fff); color: var(--fg, #222); border: 1px solid var(--muted, #888);
    font-size: 0.75em; padding: 2px 6px; white-space: nowrap; z-index: 2; }
  .outliers { font-size: 0.8em; margin: 2px 0 6px 0; padding: 0; list-style: none; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
</style>
