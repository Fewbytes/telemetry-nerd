const UNITS: [string, number][] = [["w", 604800], ["d", 86400], ["h", 3600], ["m", 60], ["s", 1]];
export function fmtPeriod(sec: number): string {
  for (const [u, s] of UNITS) if (sec >= s) return `${Number((sec / s).toPrecision(sec / s < 10 ? 2 : 3))}${u}`;
  return `${Number((sec * 1000).toPrecision(2))}ms`;
}
const NICE = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
  86400, 172800, 604800, 1209600, 2419200];
export const periodTicks = (minS: number, maxS: number): number[] => NICE.filter((v) => v >= minS && v <= maxS);
