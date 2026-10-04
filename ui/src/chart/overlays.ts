// ui/src/chart/overlays.ts — reference layers on time panels (bead 2as.11). Pure.
import type { GhostSeries, BandSeries, LineData, OverlayFlags, OverlaysPayload } from "../lib/api";

export interface Chip {
  key: keyof OverlayFlags;
  label: string;
  on: boolean;
  enabled: boolean;
  title: string;
}

/** What the chip is called: a lone limit keeps the familiar name, a mix says what it holds. */
export function limitLabel(lines: LineData[] | undefined): string {
  if (!lines?.length || (lines.length === 1 && (lines[0].kind ?? "limit") === "limit")) return "limit line";
  const kinds = [...new Set(lines.map((x) => x.kind ?? "limit"))];
  return `${kinds.join(" + ")} (${lines.length})`;
}

/** "pack (confidence 0.85)" etc.: shown on hover and in the metric card, never a bare number
 *  (bead 2as.15 provenance rule). */
export function provenance(origin?: string | null, confidence?: number | null): string {
  if (!origin) return "";
  const pct = confidence != null ? ` (confidence ${confidence.toFixed(2)})` : "";
  return `${origin}${pct}`;
}

/** Every line says who put it there and why: no unexplained numbers. */
export function lineTitle(l: LineData): string {
  return `${l.label ?? l.metric} (${l.kind ?? "limit"}): origin: ${provenance(l.origin, l.confidence) || "unknown"}; ${l.basis}`;
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
      key: "limit", label: limitLabel(l.lines), on: ov.flags.limit && l.available, enabled: l.available,
      title: l.available
        ? (l.lines ?? []).map(lineTitle).join("\n")
        : `context lines unavailable: ${l.reason ?? "no bounded_by relation"}`,
    },
    {
      key: "ghost", label: "last week", on: ov.flags.ghost && g.available, enabled: g.available,
      title: !g.available
        ? `last week unavailable: ${g.reason ?? "no earlier time range"}`
        : !g.loaded
        ? "the same time range one week earlier (fetched when switched on)"
        : g.series && g.series.length === 0
          ? "no data in the same time range one week earlier: the source holds less history, or the series is new"
          : "the same time range one week earlier, dashed",
    },
  ];
}

export interface OverlayDraw {
  normal?: Record<string, BandSeries>;
  lines?: LineData[];
  ghost?: GhostSeries[];
}

/** What toUplot should draw: only layers that are switched on, available, and carry data. */
export function overlayDraw(ov: OverlaysPayload | null | undefined): OverlayDraw | undefined {
  if (!ov) return undefined;
  const out: OverlayDraw = {};
  if (ov.flags.normal && ov.normal.available && ov.normal.series) out.normal = ov.normal.series;
  if (ov.flags.limit && ov.limit.available && ov.limit.lines?.length) out.lines = ov.limit.lines;
  if (ov.flags.ghost && ov.ghost.loaded && ov.ghost.series?.length) out.ghost = ov.ghost.series;
  return Object.keys(out).length ? out : undefined;
}
