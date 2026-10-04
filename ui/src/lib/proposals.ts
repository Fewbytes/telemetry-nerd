import type { CatalogProposalItem, LessonItem, LessonScope } from "./api";

const DAY = 86_400_000;

/** "source prom · service checkout · metrics http_* · namespace=prod" */
export function scopeText(s: LessonScope): string {
  const parts = [`source ${s.source}`];
  if (s.service) parts.push(`service ${s.service}`);
  if (s.metric_family) parts.push(`metrics ${s.metric_family}`);
  for (const [k, v] of Object.entries(s.labels ?? {}).sort()) parts.push(`${k}=${v}`);
  return parts.join(" · ");
}

export interface EvidenceRef { id: string; href: string | null; note: string | null }

/** Evidence ids as links when they live in the workspace on screen, else "in wN". */
export function evidenceRefs(ids: string[], where: Record<string, string | null>, active: string | null): EvidenceRef[] {
  return ids.map((id) => {
    const ws = where[id] ?? null;
    if (ws === null) return { id, href: null, note: "missing" };
    if (ws !== active) return { id, href: null, note: `in ${ws}` };
    return { id, href: `#/${id.startsWith("f") ? "finding" : "panel"}/${id}`, note: null };
  });
}

export const valueText = (v: unknown): string => (typeof v === "string" ? v : JSON.stringify(v));

/** The user's edit of a proposed value: text stays text, anything else is read as JSON. */
export function parseEdit(original: unknown, text: string): unknown {
  if (typeof original === "string") return text.trim();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`enter the value as JSON, e.g. ${JSON.stringify(original)}`);
  }
}

export function expiresText(ms: number, now: number): string {
  const days = Math.round((ms - now) / DAY);
  if (ms <= now) return "expired";
  return days <= 1 ? "expires within a day" : `expires in ${days} days`;
}

export const lessonPending = (l: LessonItem) => l.state === "proposed";
export const proposalPending = (p: CatalogProposalItem) => p.status === "proposed";

export const STATE_TEXT: Record<string, string> = {
  proposed: "awaiting review", approved: "approved", rejected: "rejected", refuted: "refuted", expired: "expired",
};

/** Where focus goes after an item leaves the pending list: the next pending item, else the
 * previous one, else nothing (the caller focuses the list heading). */
export function nextFocus(pendingIds: string[], decided: string): string | null {
  const i = pendingIds.indexOf(decided);
  if (i < 0) return null;
  return pendingIds[i + 1] ?? pendingIds[i - 1] ?? null;
}
