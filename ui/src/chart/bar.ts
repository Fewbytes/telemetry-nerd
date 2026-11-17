// Bar mark layout (spec section 3, Bar): pure geometry and rules, so the component only paints.
// Bars encode length, so the baseline is always zero: the value domain always contains 0 and is
// never truncated or logarithmic.

export const BAR_HORIZONTAL_LABEL_CHARS = 12; // a longer label switches to horizontal bars
export const BAR_HORIZONTAL_COUNT = 12; // more bars than this switch to horizontal bars
export const BAR_VALUE_LABEL_COUNT = 12; // value labels on the bars up to this many

export const BAR_LABEL_CHAR_PX = 7; // 12px sans-serif, generous so the longest category label is never clipped
export const BAR_LABEL_PAD_PX = 10;
export const BAR_VALUE_ROOM_PX = 48; // beyond the longest bar (right when horizontal, above when vertical), for its value label

export type BarOrder = "descending" | "given";

/** A missing value (`null`) is drawn as "no value", never as zero. */
export interface BarInput { label: string; value: number | null }

export interface Bar {
  label: string;
  value: number | null;
  /** extent along the value axis as fractions of the domain (0 = domain low, 1 = domain high); null: no bar */
  span: [number, number] | null;
}

export interface BarLayout {
  orientation: "horizontal" | "vertical";
  bars: Bar[];
  domain: [number, number];
  /** where the zero baseline sits, as a fraction of the domain */
  baseline: number;
  valueLabels: boolean;
  /** "view as table": the same rows in drawn order */
  table: { label: string; value: string }[];
}

export function layoutBars(rows: BarInput[], order: BarOrder): BarLayout {
  const ordered = order === "given"
    ? rows
    : [...rows].sort((a, b) => (b.value ?? -Infinity) - (a.value ?? -Infinity)); // stable; missing last
  const present = ordered.flatMap((r) => (r.value === null ? [] : [r.value]));
  const lo = Math.min(0, ...present), hi = Math.max(0, ...present);
  const width = hi - lo || 1;
  const frac = (v: number) => (v - lo) / width;
  const bars: Bar[] = ordered.map((r) => ({
    ...r,
    span: r.value === null ? null : [frac(Math.min(0, r.value)), frac(Math.max(0, r.value))],
  }));
  return {
    orientation: rows.some((r) => r.label.length > BAR_HORIZONTAL_LABEL_CHARS) || rows.length > BAR_HORIZONTAL_COUNT
      ? "horizontal" : "vertical",
    bars,
    domain: [lo, hi],
    baseline: frac(0),
    valueLabels: rows.length <= BAR_VALUE_LABEL_COUNT,
    table: ordered.map((r) => ({ label: r.label, value: r.value === null ? "no value" : String(r.value) })),
  };
}

/** Width reserved left of horizontal bars for the longest category label. */
export const barLabelGutterPx = (bars: Bar[]): number =>
  Math.max(0, ...bars.map((b) => b.label.length)) * BAR_LABEL_CHAR_PX + BAR_LABEL_PAD_PX;
