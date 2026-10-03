import { describe, expect, it } from "vitest";
import type { Panel, PanelGroup } from "./api";
import { basisText, groupDomain, layoutItems, msAt, orderedRoles, roleTitle, verdictBadge, xAt } from "./groups";

const panel = (id: string, t: number, group?: { id: string; role: string }): Panel => ({
  id, question: `q ${id}`, status: "open", dataset_ids: ["d1"], created_at_ms: t, answered_by: null, closed: false,
  spec: { layers: [{ mark: "line+envelope", data: "d1" }], y: { range_mode: "data", unit: null, label: null }, group },
});

const group = (id: string, t: number, roles: string[] = [], over: Partial<PanelGroup> = {}): PanelGroup => ({
  id, kind: "RED", key: "cart", source: "default", author: "claude", created_at_ms: t,
  start_ms: 0, end_ms: 3_600_000, step_ms: 60_000, basis: "suggestion", binding_origin: null,
  suggestion: "RED:otel_http", join_on: ["service_name"], matchers: {}, error_matcher: null,
  roles: roles.map((role) => ({
    role, metric: "m", panel: null, form: "rate", view: "lines", members: 1, notes: [],
    suggestion: null, why: null, gap: null, error: null,
  })),
  notes: [], closed: false, reframed_from: null, ...over,
});

describe("layoutItems", () => {
  it("gathers a group's panels into one item placed where its newest member stands", () => {
    const panels = [
      panel("p5", 50),
      panel("p4", 40, { id: "pg1", role: "duration" }),
      panel("p3", 30, { id: "pg1", role: "errors" }),
      panel("p2", 20, { id: "pg1", role: "rate" }),
      panel("p1", 10),
    ];
    const items = layoutItems(panels, [group("pg1", 15, ["rate", "errors", "duration"])]);
    expect(items.map((i) => (i.kind === "group" ? i.group.id : i.panel.id))).toEqual(["p5", "pg1", "p1"]);
    const g = items[1];
    expect(g.kind === "group" && Object.keys(g.members).sort()).toEqual(["duration", "errors", "rate"]);
  });

  it("shows a panel on its own when its group is closed or unknown, and a gap-only group too", () => {
    const panels = [panel("p2", 20, { id: "pg9", role: "rate" }), panel("p1", 10, { id: "pg1", role: "rate" })];
    const items = layoutItems(panels, [group("pg1", 5, [], { closed: true }), group("pg2", 30)]);
    expect(items.map((i) => (i.kind === "group" ? i.group.id : i.panel.id))).toEqual(["pg2", "p2", "p1"]);
  });
});

describe("orderedRoles", () => {
  it("keeps the model's role order and lists every role while the group is still drawing", () => {
    expect(orderedRoles(group("pg1", 0, ["duration", "rate"]), {})).toEqual(["rate", "duration"]);
    expect(orderedRoles(group("pg1", 0), {})).toEqual(["rate", "errors", "duration"]);
  });
});

describe("shared x axis", () => {
  it("uses the heatmap's column grid over the window", () => {
    expect(groupDomain({ start_ms: 90_000, end_ms: 3_590_000, step_ms: 60_000 })).toEqual([60_000, 3_600_000]);
  });
  it("maps time to px and back on any plot area, nothing outside", () => {
    const d: [number, number] = [0, 1000];
    expect(xAt(250, d, 64, 400)).toBe(164);
    expect(msAt(164, d, 64, 400)).toBe(250);
    expect(xAt(2000, d, 64, 400)).toBeNull();
    expect(msAt(10, d, 64, 400)).toBeNull();
  });
});

describe("labels", () => {
  it("names roles with their model symbol and says where the binding came from", () => {
    expect(roleTitle("arrival_rate")).toBe("arrival rate (λ)");
    expect(roleTitle("errors")).toBe("errors");
    expect(roleTitle("check")).toBe("L vs λ·W");
    expect(basisText({ basis: "binding", binding_origin: "claude", suggestion: null })).toBe("confirmed binding (claude)");
    expect(basisText({ basis: "suggestion", binding_origin: null, suggestion: "USE:node_cpu" })).toContain("not confirmed");
  });
});

describe("verdictBadge", () => {
  it("reads a changed role as direction, pattern and onset; others plainly", () => {
    const changed = verdictBadge({
      status: "changed", direction: "higher", pattern: "burst", at_capacity: false, text: "errors higher (burst) from 12:40Z",
      onset: { at: "2026-10-02T12:40:00+00:00", interval: ["2026-10-02T12:37:00+00:00", "2026-10-02T12:41:00+00:00"], basis: "cusum" },
    });
    expect(changed).toEqual({ label: "↑ burst from 12:40Z", tone: "moved", title: "errors higher (burst) from 12:40Z" });
    const cap = verdictBadge({ status: "changed", direction: "higher", pattern: "level", at_capacity: true, text: null, onset: { at: null, before: "x", basis: "before_window" } });
    expect(cap?.label).toBe("↑ level · at capacity before the window");
    expect(verdictBadge({ status: "no_change", direction: null, pattern: null, at_capacity: false, text: "rate: no change" })?.tone).toBe("steady");
    expect(verdictBadge({ status: "insufficient", direction: null, pattern: null, at_capacity: false, text: null })).toEqual({ label: "insufficient", tone: "unknown", title: "insufficient" });
    expect(verdictBadge(undefined)).toBeNull();
    // spec §5.4: the source leads the hover; an undetermined change says so on the badge
    const und = verdictBadge({ status: "changed", direction: "lower", pattern: "level", at_capacity: false, text: "rate lower (level)", source: "undetermined" });
    expect(und?.label).toBe("↓ level · source?");
    expect(und?.title).toBe("source undetermined: rate lower (level)");
    expect(verdictBadge({ status: "no_change", direction: null, pattern: null, at_capacity: false, text: "rate: no change", source: "common_cause" })?.title).toBe("common cause: rate: no change");
    expect(verdictBadge({ status: "no_change", direction: null, pattern: null, at_capacity: false, text: "rate: no change (common cause)", source: "common_cause" })?.title).toBe("rate: no change (common cause)");
  });
});
