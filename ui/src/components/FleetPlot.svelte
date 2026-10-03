<script lang="ts">
  import uPlot from "uplot";
  import "uplot/dist/uPlot.min.css";
  import {
    FLEET_HUE, MUTED_LINE, OUTLIER_COLORS, THRESHOLD_DASH, bandFills, bandView, boundFill, boundsAt, boundsText, coverageGaps, fleetAxisLabel,
    fleetKey, fleetLegend, groupEnds, groupFill, groupStyle, groupZoneFills, grouped, isolatedIdx, missingSteps, modeText, nearestOutlier,
    outlierEnds, outlierText, outsideRug, outsideUnflagged, placeEndLabels, toFleetUplot, untrustedSpans, type BandView, type EndLabel,
  } from "../chart/fleet";
  import { drawHatch, HIDDEN_SERIES, plotAxes } from "../chart/plotKit";
  import { fmtTimeZ } from "../lib/format";
  import { plotColors, theme } from "../lib/theme.svelte";
  import type { FleetPanelData } from "../lib/api";
  import FleetHeat from "./FleetHeat.svelte";
  import { GROUP_LABEL_PX } from "../lib/groupLink.svelte";
  import FleetSmallMultiples from "./FleetSmallMultiples.svelte";

  type View = "band" | "quantiles" | "heat" | "multiples";
  let { data, width, height = 260, range = null, unit = null, view = $bindable("band"), onRendered, xRange = null, gutter = null, onPlot }: {
    data: FleetPanelData; width: number; height?: number; range?: [number, number] | null; unit?: string | null; view?: View;
    onRendered: (ms: number, points: number, heightPx?: number) => void;
    /** a panel group's shared x domain (seconds) and y gutter, so the role lines up with the others */
    xRange?: [number, number] | null; gutter?: number | null;
    onPlot?: (u: uPlot) => void;
  } = $props();

  // SPC band + outliers (default: the reference the tests use), the descriptive quantile spread,
  // member x time heatmap, or small multiples of the top outliers
  const views: [View, string][] = [["band", "SPC band + outliers"], ["quantiles", "spread (quantiles)"], ["heat", "member × time"], ["multiples", "small multiples"]];

  let el = $state<HTMLDivElement | null>(null);
  let tip = $state<{ x: number; y: number; px: number; py: number; text: string } | null>(null);
  let tipColor = $state("");
  // the band view drawn: SPC zones, or quantiles (an older payload without zones: quantiles)
  const bv = $derived<BandView>(bandView(data, view === "quantiles" ? "quantiles" : "spc"));
  const key = $derived(fleetKey(theme.effective === "dark", data, bv));
  const axisLabel = $derived(fleetAxisLabel(data, unit, bv));

  $effect(() => {
    const host = el;
    if (!host || (view !== "band" && view !== "quantiles")) return;
    const vw = bv;
    const t0 = performance.now();
    const mode = theme.effective;
    const { stroke, grid } = plotColors(host, mode);
    const median = FLEET_HUE[mode === "dark" ? "dark" : "light"].line;
    const bg = getComputedStyle(host).getPropertyValue("--bg").trim() || (mode === "dark" ? "#16181d" : "#ffffff");
    const m = toFleetUplot(data, vw);
    const gaps = coverageGaps(data);
    const dark = mode === "dark";
    const muted = MUTED_LINE[dark ? "dark" : "light"];
    const isGrouped = grouped(data);
    const zoned = vw === "spc";
    const groupedZones = isGrouped && m.roles.includes("lo3");
    const fills = groupedZones ? (data.clusters ?? []).flatMap((_, g) => groupZoneFills(dark, g))
      : isGrouped ? [bandFills(dark)[0], ...(data.clusters ?? []).map((_, g) => groupFill(dark, g))]
      : bandFills(dark, vw);
    const labelOf: Record<string, string> = {
      lo: "min", hi: "max", q10: "10%", q90: "90%", q25: "25%", q75: "75%", cq25: "25%", cq75: "75%", lo3: "−3σ", hi3: "+3σ", lo2: "−2σ", hi2: "+2σ",
    };
    const dpr0 = window.devicePixelRatio || 1;
    let g = 0;
    const colorOf = (k: number) => OUTLIER_COLORS[k % OUTLIER_COLORS.length];
    const series: uPlot.Series[] = [{}, ...m.roles.slice(1).map((r, c): uPlot.Series => {
      const k = m.owner[c + 1];
      if (r === "median") return { label: zoned ? "median (centre)" : "median", stroke: median, width: 2, points: { show: false } };
      if (r === "cmedian") {
        const st = groupStyle(dark, g);
        return { label: `group ${data.clusters![g++].id} median`, stroke: st.line, width: 2, dash: [...st.dash], points: { show: false } };
      }
      if (r === "thrlo" || r === "thrhi") return { label: "flag threshold", stroke: muted, width: 1, dash: THRESHOLD_DASH.map((v) => v * dpr0), points: { show: false } };
      if (r === "outlier") return { label: data.outliers[k!].id, stroke: colorOf(k!), width: 1.5, points: { show: false } };
      if (r === "muted") return { label: `${data.outliers[k!].id} (inside)`, stroke: muted, width: 1.2, points: { show: false } };
      if (r === "episode") return { label: `${data.outliers[k!].id} episode`, stroke: colorOf(k!), width: 2.2, points: { show: false } };
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
              const ox = p.bbox.left / (window.devicePixelRatio || 1), oy = p.bbox.top / (window.devicePixelRatio || 1);
              if (!o) {
                // SPC: the rug of unflagged member-steps beyond 3σ (top edge); quantiles: missing-member bounds
                const n3 = data.spc?.outside3[idx] ?? 0;
                const text = zoned
                  ? (top < 10 && n3 > 0 ? `${fmtTimeZ(data.ts[idx])} · ${n3} member${n3 > 1 ? "s" : ""} ${data.spc!.outside3_note}` : null)
                  : boundsText(data, idx, fmtTimeZ);
                tipColor = muted;
                tip = text ? { x: ox + left + 12, y: oy + top + 12, px: ox + left, py: oy + top, text } : null;
                return;
              }
              const v = o.values[idx] as number;
              tipColor = OUTLIER_COLORS[data.outliers.indexOf(o) % OUTLIER_COLORS.length];
              const note = zoned && outsideUnflagged(data, o, idx) ? ` · ${data.spc!.outside3_note}` : "";
              tip = { x: ox + left + 12, y: oy + top + 12, px: ox + left, py: oy + p.valToPos(v, "y"), text: `${o.id} · ${fmtTimeZ(data.ts[idx])} · ${Number(v.toPrecision(4))}${note}` };
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
              const top = p.bbox.top, bottom = p.bbox.top + p.bbox.height;
              const clampY = (v: number) => (v === Infinity ? top : v === -Infinity ? bottom : Math.min(bottom, Math.max(top, p.valToPos(v, "y", true))));
              if (!zoned) {
                // missing-member bounds (nq6): each drawn quantile's [lower, upper] bound at steps where alive
                // members did not report, stacked light cells; unbounded reaches the plot edge
                c.fillStyle = boundFill(dark);
                for (const j of missingSteps(data)) {
                  const x = p.valToPos(data.ts[j] / 1000, "x", true);
                  for (const b of boundsAt(data, j)) {
                    const y1 = clampY(b.hi), y2 = clampY(b.lo);
                    c.fillRect(x - w / 2, y1, w, Math.max(1, y2 - y1));
                  }
                }
              } else {
                // rug (top edge): steps with member-steps beyond 3σ that no test flagged
                for (const r of outsideRug(data)) {
                  c.fillStyle = dark ? `rgba(200,205,210,${0.4 + 0.5 * Math.min(1, r.count / 5)})` : `rgba(90,95,100,${0.4 + 0.5 * Math.min(1, r.count / 5)})`;
                  c.fillRect(p.valToPos(r.x, "x", true) - Math.max(w, dpr) / 2, top, Math.max(w, dpr), 5 * dpr);
                }
              }
              // dots where a drawn column has an isolated sample (a line cannot show it), ringed in the background colour
              m.roles.forEach((r, col) => {
                if (r !== "outlier" && r !== "muted" && r !== "episode") return;
                const vals = m.data[col] as (number | null)[];
                c.fillStyle = r === "muted" ? muted : OUTLIER_COLORS[m.owner[col]! % OUTLIER_COLORS.length];
                c.strokeStyle = bg; c.lineWidth = 1.5 * dpr;
                for (const j of isolatedIdx(vals)) {
                  const x = p.valToPos(data.ts[j] / 1000, "x", true), y = p.valToPos(vals[j] as number, "y", true);
                  c.beginPath(); c.arc(x, y, (r === "episode" ? 3.5 : 3) * dpr, 0, 2 * Math.PI); c.fill(); c.stroke();
                }
              });
              // episode brackets on the time axis (transients, and level / change outliers with episodes beyond their level)
              let lane = 0, brackets = 0;
              data.outliers.forEach((o, i) => {
                if (!o.episodes.length) return;
                const yb = y0 - (5 + 4 * lane++) * dpr;
                c.strokeStyle = OUTLIER_COLORS[i % OUTLIER_COLORS.length]; c.lineWidth = 1.5 * dpr;
                for (const e of o.episodes) {
                  const x1 = p.valToPos(e.start_ms / 1000, "x", true) - w / 2, x2 = Math.max(x1 + 3 * dpr, p.valToPos(e.end_ms / 1000, "x", true) + w / 2);
                  c.beginPath(); c.moveTo(x1, yb - 4 * dpr); c.lineTo(x1, yb); c.lineTo(x2, yb); c.lineTo(x2, yb - 4 * dpr); c.stroke();
                  brackets++;
                }
              });
              host.dataset.fleetBrackets = String(brackets);
              host.dataset.fleetBand = vw;
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
                const id = isGrouped && o.cluster ? `${o.id} · ${o.cluster}` : o.id;
                endLabel(e, `${id} · ${modeText(o, fmtTimeZ, unit)}`, OUTLIER_COLORS[i % OUTLIER_COLORS.length]);
              });
              if (isGrouped) groupEnds(data, vw).forEach((e, gi) => endLabel(e, `${data.clusters![gi].id} (${data.clusters![gi].size})`, groupStyle(dark, gi).line));
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
      {#if v === "band" || v === "quantiles" || (v === "heat" && data.heat) || (v === "multiples" && data.outliers.length)}
        <button type="button" class:on={view === v} aria-pressed={view === v} data-fleet-view-btn={v} onclick={() => (view = v)}>{label}</button>
      {/if}
    {/each}
  </div>
  {#if view === "band" || view === "quantiles"}
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
    <div class="legend" data-fleet-legend>{fleetLegend(data, bv)}</div>
    {#if data.outliers.length}
      <ul class="outliers">
        {#each data.outliers as o, i (o.id)}
          <li data-fleet-kind={o.kind}><span class="swatch" class:muted={o.kind === "transient"} style:background={OUTLIER_COLORS[i % OUTLIER_COLORS.length]}></span>{outlierText(o, fmtTimeZ, unit)}</li>
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
  .sw.flag { height: 0; border-left: 0; border-right: 0; border-bottom: 0; }
  .sw.transient { height: 3px; border-left: 0; border-right: 0; border-bottom: 0; }
  .sw.rug { width: 2px; height: 9px; border: 0; }
  .sw.outlier { width: 8px; height: 8px; border-radius: 50%; border: 0; }
  .sw.hatch { background: repeating-linear-gradient(135deg, var(--muted, #888) 0 1px, transparent 1px 5px); }
  .wrap { position: relative; }
  .hov { position: absolute; width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px; border-radius: 50%; border: 1.5px solid var(--bg, #fff); pointer-events: none; z-index: 1; }
  .tip { position: absolute; pointer-events: none; background: var(--bg, #fff); color: var(--fg, #222); border: 1px solid var(--muted, #888);
    font-size: 0.75em; padding: 2px 6px; white-space: nowrap; z-index: 2; }
  .outliers { font-size: 0.8em; margin: 2px 0 6px 0; padding: 0; list-style: none; }
  .swatch { display: inline-block; width: 10px; height: 3px; margin-right: 6px; vertical-align: middle; }
  .swatch.muted { border-left: 4px solid var(--muted, #888); }
</style>
