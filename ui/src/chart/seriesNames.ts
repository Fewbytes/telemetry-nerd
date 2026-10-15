import { seriesName } from "./toUplot";

export type Labels = Record<string, string>;

const MISSING = "—";

/** short display name for each label set, given ALL label sets drawn together in one panel */
export function seriesLabels(all: Labels[]): string[] {
  const keys = [...new Set(all.flatMap((l) => Object.keys(l)))].sort((a, b) => a.localeCompare(b));
  const varying = keys.filter((k) => all.some((l) => l[k] !== all[0][k]));
  const names = all.map((l) => {
    if (varying.length === 1) return l[varying[0]] ?? MISSING;
    if (varying.length > 1) return varying.map((k) => `${k}=${l[k] ?? MISSING}`).join(", ");
    if (l.__name__ !== undefined) return l.__name__;
    const rest = keys.filter((k) => l[k] !== undefined);
    return rest.length ? rest.map((k) => `${k}=${l[k]}`).join(", ") : "total";
  });
  const seen = new Set<string>();
  return names.map((n, i) => {
    if (!seen.has(n)) { seen.add(n); return n; }
    return `${n} #${i + 1}`;
  });
}

/** lookup built once per panel: short name for one series' labels, falling back to seriesName */
export function shortNamer(all: Labels[]): (labels: Labels) => string {
  const byKey = new Map<string, string>();
  const names = seriesLabels(all);
  all.forEach((l, i) => { const k = seriesName(l); if (!byKey.has(k)) byKey.set(k, names[i]); });
  return (labels) => byKey.get(seriesName(labels)) ?? seriesName(labels);
}
