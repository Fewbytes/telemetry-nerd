import type { WorkspaceEvent } from "./api";

export interface Highlight {
  id: string;
  author: string;
  note: string | null;
  /** client clock ms; null = until cleared */
  expiresAt: number | null;
}
export type Highlights = ReadonlyMap<string, Highlight>;

/**
 * Fold one workspace event into the active-highlight set. Expiry is receipt time + ttl, so
 * daemon/browser clock skew cannot hide a highlight. Returns the same map when nothing changed.
 */
export function applyHighlightEvent(state: Highlights, e: WorkspaceEvent, now: number): Highlights {
  const id = e.object_id;
  if (id === null) return state;
  if (e.type === "object.highlighted") {
    const ttl = e.payload.ttl_ms;
    const note = typeof e.payload.note === "string" && e.payload.note ? e.payload.note : null;
    const next = new Map(state);
    next.set(id, {
      id, author: e.actor, note, expiresAt: typeof ttl === "number" ? now + ttl : null,
    });
    return next;
  }
  if ((e.type === "object.unhighlighted" || e.type === "panel.closed") && state.has(id)) {
    const next = new Map(state);
    next.delete(id);
    return next;
  }
  return state;
}

export function expire(state: Highlights, now: number): Highlights {
  const live = [...state].filter(([, h]) => h.expiresAt === null || h.expiresAt > now);
  return live.length === state.size ? state : new Map(live);
}

export function nextExpiry(state: Highlights): number | null {
  let next: number | null = null;
  for (const h of state.values()) {
    if (h.expiresAt !== null && (next === null || h.expiresAt < next)) next = h.expiresAt;
  }
  return next;
}
