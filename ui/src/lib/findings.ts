import type { CodeBrief, Finding, Hypothesis, HypothesisScope, Panel, Scope, SourceFlag } from "./api";
import { fmtRange, flagLabel } from "./format";
import { sourceText } from "./sources";

/** View models for the findings and hypotheses panes (spec §3.3, §5.3, §5.4). Pure; no DOM. */

export interface ObjectLink {
  kind: "panel" | "code" | "annotation" | "catalog" | "dataset";
  id: string;
  label: string;
  /** element to scroll to and flash; null when nothing is visible for it */
  domId: string | null;
}

export type UncertaintyKind = "interval" | "exact" | "unknown";

export interface EvidenceView {
  index: number;
  kind: "statistic" | "panel" | "annotation" | "claim";
  /** one-line text for non-statistic kinds; the statistic name otherwise */
  label: string;
  stat: {
    value: string;
    uncertainty: UncertaintyKind;
    /** "[lo, hi]", "exact" or "uncertainty unknown": never blank, never mistaken for exact */
    uncertaintyText: string;
    method: string;
  } | null;
  source: { code: string; text: string } | null;
  flags: { flag: string; label: string; message: string }[];
  /** source derived from the op that emitted the statistic, or undetermined (spec §5.4) */
  sourceFlag: { flag: string; label: string; message: string } | null;
  links: ObjectLink[];
  note: string | null;
}

export const fmtValue = (v: number): string => String(Number(v.toPrecision(6)));

export interface LinkContext {
  panels: Panel[];
  code: CodeBrief[];
  annotations: { id: string; panel: string | null }[];
}

const panelLink = (p: Panel): ObjectLink => ({
  kind: "panel",
  id: p.id,
  label: p.closed ? `${p.id} (closed)` : p.id,
  domId: p.closed ? null : `panel-${p.id}`,
});

const SOURCE_FLAG_LABEL: Record<SourceFlag["flag"], string> = {
  source_derived: "source from op",
  source_undetermined: "source undetermined",
  source_downgraded: "op label downgraded",
  source_unverified: "source unverified",
};

function describeUncertainty(ref: { exact?: boolean; uncertainty_unknown?: boolean; interval: [number, number] | null }): { kind: UncertaintyKind; text: string } {
  if (ref.exact) return { kind: "exact", text: "exact" };
  if (ref.uncertainty_unknown === true || ref.interval === null) return { kind: "unknown", text: "uncertainty unknown" };
  return { kind: "interval", text: `[${fmtValue(ref.interval[0])}, ${fmtValue(ref.interval[1])}]` };
}

export function evidenceViews(finding: Finding, ctx: LinkContext): EvidenceView[] {
  const flagsOf = (i: number) =>
    (finding.evidence_flags ?? [])
      .filter((f) => f.evidence === i)
      .map((f) => ({ flag: f.flag, label: flagLabel(f.flag), message: f.message }));
  const sourceFlagOf = (i: number) => {
    const f = (finding.source_flags ?? []).find((x) => x.evidence === i);
    return f ? { flag: f.flag, label: SOURCE_FLAG_LABEL[f.flag], message: f.message } : null;
  };
  const panelsById = new Map(ctx.panels.map((p) => [p.id, p]));
  return finding.evidence.map((ref, index): EvidenceView => {
    const base = { index, flags: flagsOf(index), sourceFlag: sourceFlagOf(index), note: null as string | null, source: null, stat: null };
    switch (ref.kind) {
      case "panel": {
        const p = panelsById.get(ref.panel);
        return { ...base, kind: "panel", label: p?.question ?? `panel ${ref.panel}`, links: p ? [panelLink(p)] : [] };
      }
      case "annotation": {
        const panel = ctx.annotations.find((a) => a.id === ref.annotation)?.panel ?? null;
        const p = panel ? panelsById.get(panel) : undefined;
        const links: ObjectLink[] = [{ kind: "annotation", id: ref.annotation, label: ref.annotation, domId: p && !p.closed ? `panel-${p.id}` : `annotation-${ref.annotation}` }];
        return { ...base, kind: "annotation", label: `annotation ${ref.annotation}`, links };
      }
      case "claim":
        return {
          ...base, kind: "claim", label: `${ref.metric} ${ref.field}: ${ref.origins.join(" vs ")} disagree`,
          note: ref.note ?? null, links: [{ kind: "catalog", id: ref.metric, label: "catalog", domId: null }],
        };
      case "statistic": {
        const { kind: uncertainty, text: uncertaintyText } = describeUncertainty(ref);
        const links: ObjectLink[] = [
          ...ctx.panels.filter((p) => p.dataset_ids.includes(ref.dataset)).map(panelLink),
          ...ctx.code.filter((c) => c.outputs.includes(ref.dataset)).map((c): ObjectLink => ({ kind: "code", id: c.id, label: `${c.id} (code)`, domId: null })),
        ];
        if (links.length === 0) links.push({ kind: "dataset", id: ref.dataset, label: ref.dataset, domId: null });
        return {
          ...base, kind: "statistic", label: ref.name,
          stat: { value: fmtValue(ref.value), uncertainty, uncertaintyText, method: ref.method },
          source: ref.source ? { code: ref.source, text: sourceText(ref.source) } : null,
          links,
        };
      }
    }
  });
}

export interface ScopeNotice { status: "beyond_evidence" | "undetermined"; text: string; note: string | null }

/** A claim reaching beyond its evidence, or whose coverage is not established: shown, never
 * hidden (principle 2; bead qxp). Null when the server found it covered or did not check. */
export function scopeNotice(f: Finding): ScopeNotice | null {
  const c = f.scope_check;
  if (!c || c.status === "covered") return null;
  const text = c.status === "beyond_evidence"
    ? `Claim names ${c.not_covered.join(", ")}, which its evidence does not cover`
    : c.message || "scope undetermined";
  return { status: c.status, text, note: f.scope_note ?? null };
}

export interface ScopeField { label: string; value: string }

/** Scope as labelled fields: what series, which time range, which population baseline (spec §2). */
export function scopeFields(scope: Scope): ScopeField[] {
  const out: ScopeField[] = [
    { label: "Series", value: scope.selector },
    { label: "Time range", value: `${fmtRange(scope.time_range.start_ms, scope.time_range.end_ms)} UTC` },
  ];
  if (scope.baseline_range) {
    out.push({ label: "Baseline", value: `${fmtRange(scope.baseline_range.start_ms, scope.baseline_range.end_ms)} UTC` });
  }
  out.push({ label: "Query step", value: `${scope.step} step, ${scope.aggregation}` });
  if (scope.source) out.push({ label: "Source", value: scope.source });
  return out;
}

const VERDICT_TEXT = { accepted: "accepted", rejected: "rejected", "needs-more": "needs more evidence" } as const;
export const verdictText = (v: Finding["verdict"]): string => (v ? VERDICT_TEXT[v] : "awaiting verdict");

export const STATUS_FLOW: Hypothesis["status"][] = ["proposed", "supported", "refuted", "inconclusive"];

export interface FindingEntry {
  id: string;
  claim: string;
  verdict: string;
  /** count of evidence items carrying an uncertainty flag */
  flagged: number;
  caveats: string[];
  /** finding was rejected by the user: no longer counts, shown struck */
  rejected: boolean;
}

export interface HypothesisView {
  for: FindingEntry[];
  against: FindingEntry[];
  /** findings that cite the hypothesis without taking a side */
  linked: FindingEntry[];
}

export function hypothesisView(h: Hypothesis, findings: Finding[]): HypothesisView {
  const byId = new Map(findings.map((f) => [f.id, f]));
  const entry = (f: Finding): FindingEntry => ({
    id: f.id,
    claim: f.claim,
    verdict: verdictText(f.verdict),
    flagged: new Set((f.evidence_flags ?? []).map((e) => e.evidence)).size,
    caveats: f.caveats,
    rejected: f.verdict === "rejected",
  });
  const pick = (ids: string[]) => ids.flatMap((id) => (byId.has(id) ? [entry(byId.get(id)!)] : []));
  const listed = new Set([...h.evidence_for, ...h.evidence_against]);
  return {
    for: pick(h.evidence_for),
    against: pick(h.evidence_against),
    linked: findings
      .filter((f) => (f.hypotheses ?? []).some((x) => x.id === h.id) && !listed.has(f.id))
      .map(entry),
  };
}

/** A hypothesis' scope as one line (qy7q): prose as given, else selector, range and source. */
export function hypothesisScopeText(scope: HypothesisScope | null | undefined): string | null {
  if (!scope) return null;
  if (scope.text) return scope.text;
  const parts: string[] = [];
  if (scope.selector) parts.push(scope.selector);
  if (scope.time_range) parts.push(fmtRange(scope.time_range.start_ms, scope.time_range.end_ms));
  if (scope.source) parts.push(`source ${scope.source}`);
  return parts.length ? parts.join(" · ") : null;
}
