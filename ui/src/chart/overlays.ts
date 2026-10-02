// ui/src/chart/overlays.ts — reference layers on time panels (bead 2as.11). Pure.
import type { GhostSeries, BandSeries, OverlayFlags, OverlaysPayload, SeriesData } from "../lib/api";

export interface Chip {
  key: keyof OverlayFlags;
  label: string;
  on: boolean;
  enabled: boolean;
  title: string;
}

/** One chip per layer: why it is unavailable is the tooltip, never silence. */
export function overlayChips(ov: OverlaysPayload): Chip[] {
  const n = ov.normal, l = ov.limit, g = ov.ghost;
  return [
    {
      key: "normal", label: "normal band", on: ov.flags.normal && n.available, enabled: n.available,
      title: n.available
        ? `${n.label}, from the operating profile${n.stale ? " (stale: a refresh is due)" : ""}` +
          (n.unmatched?.length ? `; ${n.unmatched.length} series have no profile` : "")
        : `normal band unavailable: ${n.reason ?? "no operating profile"}`,
    },
    {
      key: "limit", label: "limit line", on: ov.flags.limit && l.available, enabled: l.available,
      title: l.available ? `${l.label}, from the bounded_by relation in the catalog` : `limit line unavailable: ${l.reason ?? "no bounded_by relation"}`,
    },
    {
      key: "ghost", label: "last week", on: ov.flags.ghost && g.available, enabled: g.available,
      title: !g.loaded
        ? "the same window one week earlier (fetched when switched on)"
        : g.series && g.series.length === 0
          ? "no data in the same window one week earlier: the source holds less history, or the series is new"
          : "the same window one week earlier, dashed",
    },
  ];
}

export interface OverlayDraw {
  normal?: Record<string, BandSeries>;
  limit?: SeriesData[];
  ghost?: GhostSeries[];
}

/** What toUplot should draw: only layers that are switched on, available, and carry data. */
export function overlayDraw(ov: OverlaysPayload | null | undefined): OverlayDraw | undefined {
  if (!ov) return undefined;
  const out: OverlayDraw = {};
  if (ov.flags.normal && ov.normal.available && ov.normal.series) out.normal = ov.normal.series;
  if (ov.flags.limit && ov.limit.available && ov.limit.series) out.limit = ov.limit.series;
  if (ov.flags.ghost && ov.ghost.loaded && ov.ghost.series?.length) out.ghost = ov.ghost.series;
  return Object.keys(out).length ? out : undefined;
}
