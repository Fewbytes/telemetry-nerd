import { describe, expect, it } from "vitest";
import type { CodeBrief, Finding, Hypothesis, Panel } from "./api";
import { evidenceViews, hypothesisScopeText, hypothesisView, scopeFields, scopeNotice, verdictText } from "./findings";

const scope = {
  source: "default", selector: "tn_demo_latency_seconds",
  time_range: { start_ms: Date.UTC(2026, 0, 1, 14), end_ms: Date.UTC(2026, 0, 1, 14, 30) },
  step: "1m", aggregation: "avg", baseline_range: null,
};
const panels = [
  { id: "p1", question: "Is it slow?", dataset_ids: ["d1"], closed: false },
  { id: "p2", question: "old", dataset_ids: ["d2"], closed: true },
] as Panel[];
const code = [{ id: "c1", outputs: ["d9"] }] as CodeBrief[];
const ctx = { panels, code, annotations: [{ id: "a1", panel: "p1" }] };
const stat = (over: object) => ({
  kind: "statistic", dataset: "d1", name: "p99", value: 1.23456789, interval: null, exact: false,
  method: "bootstrap", params: {}, ...over,
});
const fnd = (over: object): Finding => ({
  id: "f1", claim: "c", scope, evidence: [], caveats: [], hypotheses: [],
  answers_panel: null, author: "claude", created_at_ms: 0, verdict: null, verdict_comment: null, ...over,
}) as Finding;

describe("evidenceViews", () => {
  it("shows interval, exact and unknown uncertainty distinctly", () => {
    const f = fnd({ evidence: [stat({ interval: [1, 2] }), stat({ exact: true, value: 4 }), stat({})] });
    const v = evidenceViews(f, ctx);
    expect(v.map((e) => e.stat?.uncertainty)).toEqual(["interval", "exact", "unknown"]);
    expect(v[0].stat?.uncertaintyText).toBe("[1, 2]");
    expect(v[1].stat?.uncertaintyText).toBe("exact");
    expect(v[2].stat?.uncertaintyText).toBe("uncertainty unknown");
    expect(v[0].stat?.value).toBe("1.23457");
  });
  it("treats an explicit uncertainty_unknown as unknown", () => {
    const [e] = evidenceViews(fnd({ evidence: [stat({ uncertainty_unknown: true })] }), ctx);
    expect(e.stat?.uncertainty).toBe("unknown");
  });
  it("carries the source chip and server flags per item", () => {
    const f = fnd({
      evidence: [stat({ source: "special_cause" }), stat({})],
      evidence_flags: [{ evidence: 1, flag: "uncertainty_unknown", message: "m" }],
    });
    const v = evidenceViews(f, ctx);
    expect(v[0].source).toEqual({ code: "special_cause", text: "special cause" });
    expect(v[0].flags).toEqual([]);
    expect(v[1].source).toBeNull();
    expect(v[1].flags[0]).toMatchObject({ flag: "uncertainty_unknown", label: "uncertainty unknown" });
  });
  it("links a statistic to the panel drawing its dataset, or the code node producing it", () => {
    const v = evidenceViews(fnd({ evidence: [stat({}), stat({ dataset: "d9" }), stat({ dataset: "d2" }), stat({ dataset: "zz" })] }), ctx);
    expect(v[0].links).toEqual([{ kind: "panel", id: "p1", label: "p1", domId: "panel-p1" }]);
    expect(v[1].links).toEqual([{ kind: "code", id: "c1", label: "c1 (code)", domId: null }]);
    expect(v[2].links[0]).toMatchObject({ kind: "panel", label: "p2 (closed)", domId: null });
    expect(v[3].links[0]).toMatchObject({ kind: "dataset", id: "zz" });
  });
  it("labels panel and annotation evidence", () => {
    const v = evidenceViews(fnd({ evidence: [{ kind: "panel", panel: "p1" }, { kind: "annotation", annotation: "a1" }] }), ctx);
    expect(v[0].label).toBe("Is it slow?");
    expect(v[1].links[0].domId).toBe("panel-p1");
  });
});

describe("scopeFields", () => {
  it("lists series, window and resolution; baseline when present", () => {
    expect(scopeFields(scope as never).map((f) => f.label)).toEqual(["Series", "Time range", "Query step", "Source"]);
    const withBase = scopeFields({ ...scope, baseline_range: { start_ms: 0, end_ms: 3_600_000 } } as never);
    expect(withBase.map((f) => f.label)).toContain("Baseline");
    expect(withBase[1].value).toBe("14:00–14:30 UTC");
  });
});

describe("hypothesisView", () => {
  const h = { id: "h1", evidence_for: ["f1"], evidence_against: ["f2"] } as Hypothesis;
  it("groups for / against / linked findings with flag counts", () => {
    const fs = [
      fnd({ id: "f1", evidence: [stat({})], evidence_flags: [{ evidence: 0, flag: "uncertainty_unknown", message: "" }, { evidence: 0, flag: "input_uncertainty_unknown", message: "" }], verdict: "accepted" }),
      fnd({ id: "f2", verdict: "rejected" }),
      fnd({ id: "f3", hypotheses: [{ id: "h9", stance: "for" }, { id: "h1", stance: "against" }] }),
      fnd({ id: "f4", hypotheses: [{ id: "h9", stance: "for" }] }),
    ];
    const v = hypothesisView(h, fs);
    expect(v.for.map((e) => [e.id, e.flagged, e.verdict])).toEqual([["f1", 1, "accepted"]]);
    expect(v.against[0].rejected).toBe(true);
    expect(v.linked.map((e) => e.id)).toEqual(["f3"]);
  });
  it("skips evidence ids that no longer resolve", () => {
    expect(hypothesisView(h, []).for).toEqual([]);
  });
});

describe("hypothesisScopeText", () => {
  it("shows prose as given and a structured scope as selector · range · source", () => {
    expect(hypothesisScopeText(null)).toBeNull();
    expect(hypothesisScopeText({ text: "payment, checkout; 14:30-15:00Z" })).toBe("payment, checkout; 14:30-15:00Z");
    const t = hypothesisScopeText({ selector: 'x{service_name="payment"}', time_range: { start_ms: 0, end_ms: 60_000 }, source: "default" });
    expect(t).toMatch(/^x\{service_name="payment"\} · .+ · source default$/);
  });
});

it("verdictText", () => {
  expect(verdictText(null)).toBe("awaiting verdict");
  expect(verdictText("needs-more")).toBe("needs more evidence");
});

describe("evidence discipline (qxp)", () => {
  it("shows a claim beyond its evidence with its note, and nothing when covered", () => {
    const check = { status: "beyond_evidence", named: [], not_covered: ['service_name="checkout"'], undetermined: [], message: "m" };
    const n = scopeNotice(fnd({ scope_check: check, scope_note: "read off d2" }));
    expect(n?.status).toBe("beyond_evidence");
    expect(n?.text).toContain('service_name="checkout"');
    expect(n?.note).toBe("read off d2");
    expect(scopeNotice(fnd({ scope_check: { ...check, status: "covered" } }))).toBeNull();
    expect(scopeNotice(fnd({}))).toBeNull();
    const u = scopeNotice(fnd({ scope_check: { ...check, status: "undetermined", message: "scope undetermined: x" } }));
    expect(u?.text).toBe("scope undetermined: x");
  });
  it("labels derived and undetermined sources per evidence item", () => {
    const f = fnd({
      evidence: [stat({ source: "special_cause" }), { kind: "panel", panel: "p1" }],
      source_flags: [
        { evidence: 0, flag: "source_derived", source: "special_cause", message: "from analyze" },
        { evidence: 1, flag: "source_undetermined", source: "undetermined", message: "none cited" },
      ],
    });
    const v = evidenceViews(f, ctx);
    expect(v[0].sourceFlag?.label).toBe("source from op");
    expect(v[1].sourceFlag?.label).toBe("source undetermined");
    expect(v[0].flags).toEqual([]);
  });
  it("labels a downgraded and an unverified source (i6y5)", () => {
    const f = fnd({
      evidence: [stat({ source: "undetermined" }), stat({ source: "special_cause" })],
      source_flags: [
        { evidence: 0, flag: "source_downgraded", source: "undetermined", message: "op said special_cause" },
        { evidence: 1, flag: "source_unverified", source: "special_cause", message: "no op" },
      ],
    });
    const v = evidenceViews(f, ctx);
    expect(v[0].sourceFlag?.label).toBe("op label downgraded");
    expect(v[1].sourceFlag?.label).toBe("source unverified");
  });
});
