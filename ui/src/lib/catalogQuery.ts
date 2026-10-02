// ui/src/lib/catalogQuery.ts — the catalog view's filter state and its URL (bead 2as.13). Pure.
import type { CatalogRow } from "./api";

export const PAGE = 50;
export interface CatalogFilters {
  source: string; q: string; prefix: string;
  origin: "" | "user" | "claude" | "stats" | "context" | "pack" | "metadata" | "rule";
  weak: boolean; // winner below 0.6
  conflicts: boolean; findings: boolean;
  reviewed: "" | "yes" | "no";
  removed: boolean;
  members: boolean; // list family members too (default: a family stands for them)
  family: string; // only the members of this family
  sort: "name" | "weakest" | "conflicts";
  page: number; // 0-based
}
export const WEAK_BELOW = 0.6;
export const defaults = (source = ""): CatalogFilters => ({
  source, q: "", prefix: "", origin: "", weak: false, conflicts: false, findings: false, reviewed: "",
  removed: false, members: false, family: "", sort: "name", page: 0,
});

/** Only what differs from the defaults goes in the query: the URL says what is filtered. */
export function catalogParams(f: CatalogFilters): URLSearchParams {
  const p = new URLSearchParams({ source: f.source, limit: String(PAGE), offset: String(f.page * PAGE) });
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.prefix.trim()) p.set("prefix", f.prefix.trim());
  if (f.origin) p.set("origin", f.origin);
  if (f.weak) p.set("max_confidence", String(WEAK_BELOW));
  if (f.conflicts) p.set("conflicts", "1");
  if (f.findings) p.set("findings", "1");
  if (f.reviewed) p.set("reviewed", f.reviewed);
  if (f.removed) p.set("removed", "1");
  if (f.members) p.set("members", "1");
  if (f.family) p.set("family", f.family);
  if (f.sort !== "name") p.set("sort", f.sort);
  return p;
}

export const activeFilters = (f: CatalogFilters): number =>
  [f.q.trim(), f.prefix.trim(), f.origin, f.weak, f.conflicts, f.findings, f.reviewed, f.removed, f.members, f.family].filter(Boolean).length;

export function pageInfo(total: number, page: number): { from: number; to: number; pages: number; prev: boolean; next: boolean } {
  const pages = Math.max(1, Math.ceil(total / PAGE));
  const from = total === 0 ? 0 : page * PAGE + 1;
  return { from, to: Math.min(total, (page + 1) * PAGE), pages, prev: page > 0, next: page + 1 < pages };
}

/** One short badge per thing worth a second look in a row. */
export function rowBadges(r: CatalogRow): { key: string; text: string; tone: "warn" | "info" }[] {
  const out: { key: string; text: string; tone: "warn" | "info" }[] = [];
  if (!r.present) out.push({ key: "removed", text: "no longer in the source", tone: "info" });
  if (r.is_family) {
    const s = r.family_info?.status === "confirmed" ? ", confirmed" : "";
    out.push({ key: "family", text: `family · ${(r.family_members ?? 0).toLocaleString("en-US")} metrics${s}`, tone: "info" });
  }
  if (r.conflicts.length) out.push({ key: "conflicts", text: `conflict: ${r.conflicts.join(", ")}`, tone: "warn" });
  for (const f of r.findings) out.push({ key: `finding-${f.kind}`, text: f.kind.replace(/_/g, " "), tone: "warn" });
  if (r.verdict && r.verdict !== "inconclusive" && r.verdict !== "no data") out.push({ key: "verdict", text: `scan: ${r.verdict}`, tone: "info" });
  return out;
}
