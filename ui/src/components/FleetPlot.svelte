<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    FLEET_HUE, OUTLIER_COLORS, bandFills, coverageGaps, fleetAxisLabel, fleetKey, fleetLegend, groupEnds, groupFill, groupStyle,
    grouped, nearestOutlier, outlierEnds, outlierMarkIdx, outlierText, placeEndLabels, toFleetUplot, untrustedSpans, type EndLabel,
  } from "../chart/fleet";
  import { drawHatch, HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { FleetPanelData } from "../lib/api";
  import FleetHeat from "./FleetHeat.svelte";
  import { GROUP_LABEL_PX } from "../lib/groupLink.svelte";
  import FleetSmallMultiples from "./FleetSmallMultiples.svelte";

  type View = "band" | "heat" | "multiples";
  let { data, width, height = 260, range = null, unit = null, view = $bindable("band"), onRendered, xRange = null, gutter = null, onPlot }: {
    data: FleetPanelData; width: number; height?: number; range?: [number, number] | null; unit?: string | null; view?: View;
    onRendered: (ms: number, points: number, heightPx?: number) => void;
    /** a panel group's shared x domain (seconds) and y gutter, so the role lines up with the others */
    xRange?: [number, number] | null; gutter?: number | null;
    onPlot?: (u: uPlot) => void;
  } = $props();

  // band + lines (default), member x time heatmap, or small multiples of the top outliers
  const views: [View, string][] = [["band", "band + outliers"], ["heat", "member × time"], ["multiples", "small multiples"]];

  let el = $state<HTMLDivElement | null>(null);
  let tip = $state<{ x: number; y: number; px: number; py: number; text: string } | null>(null);
  let tipColor = $state("");
  const key = $derived(fleetKey(theme.effective === "dark", data));
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
    const isGrouped = grouped(data);
    const fills = isGrouped ? [bandFills(dark)[0], ...(data.clusters ?? []).map((_, g) => groupFill(dark, g))] : bandFills(dark);
    const labelOf: Record<string, string> = { lo: "min", hi: "max", q10: "10%", q90: "90%", q25: "25%", q75: "75%", cq25: "25%", cq75: "75%" };
    let k = 0, g = 0;
    const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r): uPlot.Series => {
      if (r === "median") return { label: "median", stroke: median, width: 2, points: { show: false } };
      if (r === "cmedian") {
        const st = groupStyle(dark, g);
        return { label: `group ${data.clusters![g++].id} median`, stroke: st.line, width: 2, dash: [...st.dash], points: { show: false } };
      }
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
        axes: plotAxes(stroke, grid, { label: data.normalise === "member" ? "× own median" : unit ?? undefined, ...(gutter ? { size: gutter - GROUP_LABEL_PX, labelSize: GROUP_LABEL_PX, labelGap: 0 } : {}) }),
        legend: { show: false },
        scales: {
          ...(range ? { y: { range: (): [number, number] => range } } : {}),
          ...(xRange ? { x: { time: true, range: (): [number, number] => xRange } } : {}),
        },
        ...(gutter ? { padding: [10, 0, 0, 0] as uPlot.Padding } : {}),
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
              const ox = p.bbox.left / (window.devicePixelRatio || 1), oy = p.bbox.top / (window.devicePixelRatio || 1);
              tipColor = OUTLIER_COLORS[data.outliers.indexOf(o) % OUTLIER_COLORS.length];
              tip = { x: ox + left + 12, y: oy + top + 12, px: ox + left, py: oy + p.valToPos(v, "y"), text: `${o.id} · ${fmtTimeZ(data.ts[idx])} · ${Number(v.toPrecision(4))}` };
            },
          ],
          draw: [
            (p: uPlot) => { onPlot?.(p); },
            (p: uPlot) => {
              const c = p.ctx, dpr = window.devicePixelRatio || 1;
              c.save();
              // data unknown (located untrusted_data, oyi): hatched over the whole height, never read as values
              drawHatch(p, untrustedSpans(data).map(([s0, s1]): [number, number] => [s0 / 1000, s1 / 1000]), dark);
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
              // outlier labels at their last drawn point, de-collided (cis): labels sharing x are
              // spread vertically in order, joined to their point by a short leader when moved, and
              // haloed in the background colour so lines under them never cut through the text
              const fs = 11 * dpr, lh = 13 * dpr;
              c.font = `${fs}px sans-serif`;
              c.textAlign = "right"; c.textBaseline = "middle"; c.lineJoin = "round";
              const items: (EndLabel & { text: string; color: string; px: number; py: number })[] = [];
              const endLabel = (e: { j: number; v: number } | null, text: string, color: string) => {
                if (!e) return;
                const px = p.valToPos(data.ts[e.j] / 1000, "x", true), py = p.valToPos(e.v, "y", true);
                items.push({ text, color, px, py, right: px - 6 * dpr, w: c.measureText(text).width, y: py - 6 * dpr });
              };
              outlierEnds(data).forEach((e, i) => {
                const o = data.outliers[i];
                endLabel(e, isGrouped && o.cluster ? `${o.id} · ${o.cluster}` : o.id, OUTLIER_COLORS[i % OUTLIER_COLORS.length]);
              });
              if (isGrouped) groupEnds(data).forEach((e, gi) => endLabel(e, `${data.clusters![gi].id} (${data.clusters![gi].size})`, groupStyle(dark, gi).line));
              const ys = placeEndLabels(items, lh, p.bbox.top, p.bbox.top + p.bbox.height);
              // placed label boxes (css px), for checking that none overlap
              host.dataset.fleetLabels = JSON.stringify(items.map((it, k) => [
                Math.round((it.right - it.w) / dpr), Math.round((ys[k] - lh / 2) / dpr), Math.round(it.w / dpr), Math.round(lh / dpr),
              ]));
              items.forEach((it, k) => {
                const col = it.color, y = ys[k];
                if (Math.abs(y - it.y) > 2 * dpr) {
                  c.strokeStyle = col; c.lineWidth = 1 * dpr;
                  c.beginPath(); c.moveTo(it.px - 1 * dpr, it.py); c.lineTo(it.right + 2 * dpr, y); c.stroke();
                }
                c.strokeStyle = bg; c.lineWidth = 3 * dpr;
                c.strokeText(it.text, it.right, y);
                c.fillStyle = col;
                c.fillText(it.text, it.right, y);
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
        <li data-fleet-key-id={k.id}><span class="sw {k.id.startsWith('group-') ? 'group' : k.id}" class:hatch={k.hatch} style:background={k.hatch ? undefined : k.swatch}
          style:border-top={k.line ? `2px ${k.line.style} ${k.line.color}` : undefined}></span>{k.label}</li>
      {/each}
    </ul>
    <div class="wrap"><div bind:this={el}></div>{#if tip}<i class="hov" style:left="{tip.px}px" style:top="{tip.py}px" style:background={tipColor}></i><div class="tip" style:left="{tip.x}px" style:top="{tip.y}px">{tip.text}</div>{/if}</div>
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
  .sw.hatch { background: repeating-linear-gradient(135deg, var(--muted, #888) 0 1px, transparent 1px 5px); }
  .wrap { position: relative; }
  .hov { position: absolute; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 50%; border: 1.5px solid var(--bg, #fff); pointer-events: none; z-index: 1; }
  .tip { position: absolute; pointer-events: none; background: var(--bg, #fff); color: var(--fg, #222); border: 1px solid var(--muted, #888);
    font-size: 0.75em; padding: 2px 6px; white-space: nowrap; z-index: 2; }
  .outliers { font-size: 0.8em; margin: 2px 0 6px 0; padding: 0; list-style: none; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
</style>
