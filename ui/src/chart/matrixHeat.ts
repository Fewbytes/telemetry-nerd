import { colormap } from "./colormap";

/** Cell colour of a caller-supplied matrix: viridis, linear between the matrix min and max
 *  (darkest = min, lightest = max), the mapping the payload's legend states. null = missing, blank. */
export function matrixCellColor(value: number | null, min: number, max: number): string | null {
  if (value === null) return null;
  return colormap("viridis")(max > min ? (value - min) / (max - min) : 0);
}

export const matrixCellTip = (row: string, column: string, value: number | null): string =>
  `${row} · ${column} · ${value === null ? "missing" : value}`;
