import type { SeriesData, TimePanelData } from "../lib/api";

export type DataViewName = "overlay" | "filtered" | "removed" | "raw";
export const VIEW_LABELS: Record<DataViewName, string> = {
  overlay: "filtered over raw", filtered: "filtered", removed: "raw + removed part", raw: "raw",
};

/** What to draw for a data view of a filtered panel: main series plus an optional context set. */
export function drawnFor(view: DataViewName, d: TimePanelData): {
  series: SeriesData[]; context?: { role: "raw" | "removed"; series: SeriesData[] };
} {
  const raw = d.raw ?? [];
  switch (view) {
    case "overlay": return { series: d.series, context: { role: "raw", series: raw } };
    case "raw": return { series: raw };
    case "removed": return { series: raw, context: { role: "removed", series: d.removed ?? [] } };
    default: return { series: d.series };
  }
}
