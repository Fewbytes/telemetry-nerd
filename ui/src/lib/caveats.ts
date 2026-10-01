import type { Finding, Hypothesis } from "./api";

/**
 * Caveats on the findings that support a hypothesis. A "for" finding that carries a caveat
 * (e.g. "synthetic, not an incident") is a weakened support: show it on the hypothesis
 * itself, not only on the finding card.
 */
export function supportingCaveats(h: Hypothesis, findings: Finding[]): { finding: string; caveat: string }[] {
  const supporting = new Set(h.evidence_for);
  return findings
    .filter((f) => supporting.has(f.id))
    .flatMap((f) => f.caveats.map((caveat) => ({ finding: f.id, caveat })));
}
