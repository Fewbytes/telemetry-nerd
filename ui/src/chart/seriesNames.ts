import { seriesName } from "./toUplot";

export type Labels = Record<string, string>;

const MISSING = "—";

/** short display name for each label set, given ALL label sets drawn together in one panel */
export function seriesLabels(all: Labels[]): string[] {
  const keys = [...new Set(all.flatMap((l) => Object.keys(l)))].sort((a, b) => a.localeCompare(b));
  const varying = keys.filter((k) => all.some((l) => l[k] !== all[0][k]));
  const others = varying.filter((k) => k !== "__name__");
  const names = all.map((l) => {
    if (varying.length === 1 && others.length === 1) return l[others[0]] ?? MISSING;
    if (varying.length) {
      // a varying metric name leads, bare; the other varying labels follow as k=v
      const head = varying.includes("__name__") ? [l.__name__ ?? MISSING] : [];
      const rest = others.map((k) => `${k}=${l[k] ?? MISSING}`).join(", ");
      return head.length && rest ? `${head[0]} · ${rest}` : head.length ? head[0] : rest;
    }
    if (l.__name__ !== undefined) return l.__name__;
    const rest = keys.filter((k) => l[k] !== undefined);
    return rest.length ? rest.map((k) => `${k}=${l[k]}`).join(", ") : "total";
  });
  const seen = new Set(names);
  const dupes = new Map<string, number>();
  const used = new Set<string>();
  return names.map((n) => {
    if (!used.has(n)) { used.add(n); return n; }
    let c = dupes.get(n) ?? 1, cand: string;
    do { cand = `${n} #${++c}`; } while (seen.has(cand) || used.has(cand));
    dupes.set(n, c);
    used.add(cand);
    return cand;
  });
}

/**
 * lookup built once per panel: short name for one series' labels, falling back to seriesName.
 * Identical label sets are one series by design (they share a name), so ` #n` only shows up
 * in `seriesLabels` output, never through this lookup.
 */
export function shortNamer(all: Labels[]): (labels: Labels) => string {
  const byKey = new Map<string, string>();
  const names = seriesLabels(all);
  all.forEach((l, i) => { const k = seriesName(l); if (!byKey.has(k)) byKey.set(k, names[i]); });
  return (labels) => byKey.get(seriesName(labels)) ?? seriesName(labels);
}
