// Panel groups (bead czt.3): a USE / RED / Little's law binding drawn as one linked set of panels.
// Pure helpers: which panels belong to which group, in what order, and how the roles read.
import type { Panel, PanelGroup, RoleVerdict } from "./api";

/** Role order per binding kind (the catalog's BINDING_ROLES). */
export const ROLE_ORDER: Record<string, string[]> = {
  RED: ["rate", "errors", "duration"],
  USE: ["utilization", "saturation", "errors"],
  littles_law: ["arrival_rate", "latency", "concurrency", "check"], // check: czt.2's L vs λ·W panel
};

export const KIND_LABELS: Record<string, string> = { RED: "RED", USE: "USE", littles_law: "Little's law" };

/** The symbol a role carries in its model, shown next to its name. */
export const ROLE_SYMBOLS: Record<string, string> = { arrival_rate: "λ", latency: "W", concurrency: "L" };

export const FORM_LABELS: Record<string, string> = {
  rate: "per-second rate",
  error_ratio: "errors ÷ requests, Wilson 95% band",
  errors: "errors per second",
  distribution: "latency distribution",
  mean: "mean (sum ÷ count)",
  value: "as reported",
  utilization: "share of capacity in use",
  saturation: "work waiting",
  concurrency: "requests in flight",
  littles: "L vs λ·W per window, propagated 95% interval",
};

export const roleTitle = (role: string): string => {
  if (role === "check") return "L vs λ·W";
  const name = role.replace(/_/g, " ");
  return ROLE_SYMBOLS[role] ? `${name} (${ROLE_SYMBOLS[role]})` : name;
};

export type LayoutItem =
  | { kind: "panel"; panel: Panel }
  | { kind: "group"; group: PanelGroup; members: Record<string, Panel> };

/**
 * Panels and open groups in one list, newest first. A group stands where its newest member (or the
 * group itself) would; a panel naming a group the snapshot does not hold is shown on its own.
 */
export function layoutItems(panels: Panel[], groups: PanelGroup[]): LayoutItem[] {
  const open = new Map(groups.filter((g) => !g.closed).map((g) => [g.id, g]));
  const members = new Map<string, Record<string, Panel>>();
  const at = new Map<string, number>();
  const items: { item: LayoutItem; t: number; order: number }[] = [];
  panels.forEach((p, i) => {
    const gid = p.spec.group?.id;
    const g = gid ? open.get(gid) : undefined;
    if (!g || !p.spec.group) {
      items.push({ item: { kind: "panel", panel: p }, t: p.created_at_ms, order: i });
      return;
    }
    const m = members.get(g.id) ?? {};
    m[p.spec.group.role] = p;
    members.set(g.id, m);
    at.set(g.id, Math.max(at.get(g.id) ?? g.created_at_ms, p.created_at_ms));
  });
  let k = panels.length;
  for (const g of open.values()) {
    items.push({ item: { kind: "group", group: g, members: members.get(g.id) ?? {} }, t: at.get(g.id) ?? g.created_at_ms, order: k++ });
  }
  // stable: newest first; ties keep the panel list's own order (already newest first)
  return items.sort((a, b) => b.t - a.t || a.order - b.order).map((x) => x.item);
}

/** Roles in model order: the group's own record, plus any member panel it does not list yet. */
export function orderedRoles(group: PanelGroup, members: Record<string, Panel>): string[] {
  const order = ROLE_ORDER[group.kind] ?? [];
  const known = new Set([...group.roles.map((r) => r.role), ...Object.keys(members)]);
  return [...order.filter((r) => known.has(r) || group.roles.length === 0), ...[...known].filter((r) => !order.includes(r))];
}

/** The time domain every panel of the group draws: the heatmap's column grid over the window. */
export function groupDomain(g: Pick<PanelGroup, "start_ms" | "end_ms" | "step_ms">): [number, number] {
  const k = g.step_ms;
  return [Math.ceil(g.start_ms / k) * k - k, Math.ceil(g.end_ms / k) * k];
}

/** x position (px) of `ms` on a plot area [left, left+width] showing `domain`, or null outside. */
export function xAt(ms: number, domain: [number, number], left: number, width: number): number | null {
  const [a, b] = domain;
  if (b <= a || ms < a || ms > b) return null;
  return left + ((ms - a) / (b - a)) * width;
}

/** The time under px on that plot area, or null outside it. */
export function msAt(px: number, domain: [number, number], left: number, width: number): number | null {
  if (width <= 0 || px < left || px > left + width) return null;
  return domain[0] + ((px - left) / width) * (domain[1] - domain[0]);
}

/** One line under the header: where the roles came from. */
export function basisText(g: Pick<PanelGroup, "basis" | "binding_origin" | "suggestion">): string {
  return g.basis === "binding"
    ? `confirmed binding${g.binding_origin ? ` (${g.binding_origin})` : ""}`
    : `suggestion ${g.suggestion ?? ""}, not confirmed`;
}

/** A role's verdict badge (bead czt.4): what moved, how, from when; title = the full sentence. */
export function verdictBadge(v: RoleVerdict | null | undefined): { label: string; tone: "moved" | "steady" | "unknown"; title: string } | null {
  if (!v) return null;
  const title = v.text ?? v.status;
  if (v.status === "changed") {
    const arrow = v.direction === "higher" ? "↑" : v.direction === "lower" ? "↓" : "";
    const when = v.onset?.at ? ` from ${v.onset.at.slice(11, 16)}Z` : v.onset?.before ? " before the window" : "";
    const cap = v.at_capacity ? " · at capacity" : "";
    return { label: `${arrow} ${v.pattern ?? "changed"}${cap}${when}`.trim(), tone: "moved", title };
  }
  if (v.status === "no_change") return { label: "no change", tone: "steady", title };
  return { label: v.status.replace(/_/g, " "), tone: "unknown", title };
}
