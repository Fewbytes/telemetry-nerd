import type { Snapshot, Thread } from "./api";

export type RefKind = "panel" | "annotation" | "hypothesis" | "finding" | "gap" | "thread" | "code";

export interface RefTarget {
  id: string;
  kind: RefKind;
  /** element to highlight; null when nothing is visible for this object */
  domId: string | null;
  /** short description for the chip tooltip */
  label: string;
  closed: boolean;
}

export type Segment = { text: string } | ({ ref: string } & RefTarget);

const REF = /(?<![\w])([pafhgtc]\d+)(?![\w])/g;
const MAX_LABEL = 80;

const short = (s: string) => (s.length > MAX_LABEL ? `${s.slice(0, MAX_LABEL - 1)}…` : s);

/** Every object a message can mention, keyed by id. */
export function refTargets(snap: Snapshot): Map<string, RefTarget> {
  const out = new Map<string, RefTarget>();
  const add = (t: RefTarget) => out.set(t.id, t);
  const open = new Set(snap.panels.filter((p) => !p.closed).map((p) => p.id));
  for (const p of snap.panels) {
    add({ id: p.id, kind: "panel", domId: p.closed ? null : `panel-${p.id}`, label: short(p.question), closed: p.closed });
  }
  for (const a of snap.annotations) {
    if (a.deleted) continue;
    const domId = a.panel === null ? null : open.has(a.panel) ? `panel-${a.panel}` : `annotation-${a.id}`;
    add({ id: a.id, kind: "annotation", domId, label: short(a.label), closed: false });
  }
  // a hidden hypothesis that no finding cites has no card on the board (showHidden is off by
  // default); one a finding still cites always renders (fygk: hiding must never break a
  // citation), so only an uncited hidden one points nowhere
  const citedHyps = new Set(snap.findings.flatMap((f) => (f.hypotheses ?? []).map((x) => x.id)));
  for (const h of snap.hypotheses) {
    const uncitedHidden = h.hidden && !citedHyps.has(h.id);
    add({
      id: h.id, kind: "hypothesis", domId: uncitedHidden ? null : `hypothesis-${h.id}`,
      label: short(h.statement), closed: uncitedHidden,
    });
  }
  for (const f of snap.findings) {
    add({ id: f.id, kind: "finding", domId: `finding-${f.id}`, label: short(f.claim), closed: false });
  }
  for (const g of snap.gaps) {
    add({ id: g.id, kind: "gap", domId: `gap-${g.id}`, label: short(g.missing_signal), closed: false });
  }
  for (const t of snap.threads) {
    add({ id: t.id, kind: "thread", domId: `thread-${t.id}`, label: t.anchor ? `thread about ${t.anchor}` : "chat", closed: false });
  }
  for (const c of snap.code ?? []) {
    add({ id: c.id, kind: "code", domId: null, label: `code run: ${c.status}${c.rerun_of ? `, re-run of ${c.rerun_of}` : ""}`, closed: false });
  }
  return out;
}

/** Split message text into plain runs and references to objects that exist. */
export function splitRefs(text: string, targets: Map<string, RefTarget>): Segment[] {
  const out: Segment[] = [];
  let last = 0;
  for (const m of text.matchAll(REF)) {
    const target = targets.get(m[1]);
    if (!target) continue;
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ ref: m[1], ...target });
    last = m.index + m[0].length;
  }
  if (last < text.length || out.length === 0) out.push({ text: text.slice(last) });
  return out;
}

/** The workspace's general (anchor-less) chat thread, if one exists yet. */
export function chatThread(threads: Thread[]): Thread | null {
  return threads.find((t) => t.anchor === null) ?? null;
}
