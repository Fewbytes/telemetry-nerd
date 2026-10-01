import type { ValueAxis } from "./heatmap";

export function valueTicks(a: ValueAxis): number[] {
  if (a.kind === "linear") {
    const raw = (a.max - a.min) / 5;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
    const out: number[] = [];
    for (let v = Math.ceil(a.min / step) * step; v <= a.max + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
    return out;
  }
  const lo = Math.ceil(Math.log10(a.min) - 1e-9), hi = Math.floor(Math.log10(a.max) + 1e-9);
  const mults = hi - lo <= 2 ? [1, 2, 5] : [1];
  const out: number[] = [];
  for (let e = lo - 1; e <= hi; e++)
    for (const m of mults) {
      const v = Number((m * 10 ** e).toPrecision(12));
      if (v >= a.min * (1 - 1e-9) && v <= a.max * (1 + 1e-9)) out.push(v);
    }
  return out;
}

const sig = (v: number) => String(Number(v.toPrecision(3)));

export function fmtValue(v: number, unit: string | null): string {
  if (unit === "s" && Math.abs(v) < 1 && v !== 0) return `${sig(v * 1000)} ms`;
  return unit ? `${sig(v)} ${unit}` : sig(v);
}
