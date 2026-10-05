import type { Panel, Snapshot, WorkspaceInfo } from "./api";

/** True when `next` belongs to a different workspace than `prev` (the first snapshot is not a switch). */
export const workspaceChanged = (prev: Snapshot | null, next: Snapshot): boolean =>
  prev !== null && prev.workspace.id !== next.workspace.id;

const panelEqual = (a: Panel, b: Panel): boolean =>
  a === b || (
    a.question === b.question && a.status === b.status && a.answered_by === b.answered_by &&
    a.closed === b.closed && a.created_at_ms === b.created_at_ms &&
    JSON.stringify(a.spec) === JSON.stringify(b.spec) &&
    JSON.stringify(a.dataset_ids) === JSON.stringify(b.dataset_ids)
  );

/**
 * `next`, with any panel that is byte-for-byte unchanged from `prev` (same id, same spec/dataset
 * and every other field) replaced by `prev`'s own object. A reload re-parses the whole snapshot
 * from JSON, so every panel gets a fresh reference even when nothing about it moved; panel
 * components key their data fetch off their `panel` prop's identity (bead rhe2), so a reload
 * otherwise refetches all of them. Reusing the old reference for untouched panels leaves their
 * components' props unchanged and so skips that refetch; a panel whose spec, dataset or any
 * other field changed gets `next`'s own (new) object, so its component still refetches as normal.
 * Does not mutate either array.
 */
export const reconcilePanels = (prev: Panel[], next: Panel[]): Panel[] => {
  if (prev.length === 0) return next;
  const byId = new Map(prev.map((p) => [p.id, p]));
  let anyReused = false;
  const out = next.map((p) => {
    const old = byId.get(p.id);
    if (old && old !== p && panelEqual(old, p)) { anyReused = true; return old; }
    return p;
  });
  return anyReused ? out : next;
};

const pad = (n: number) => String(n).padStart(2, "0");

/** "Investigation 2026-10-03 14:05" in local time. */
export const defaultTitle = (now: Date): string =>
  `Investigation ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ` +
  `${pad(now.getHours())}:${pad(now.getMinutes())}`;

/** Active workspace first, then the rest by most recent activity. Does not mutate `list`. */
export const sortForSwitcher = (list: WorkspaceInfo[], activeId: string): WorkspaceInfo[] =>
  [...list].sort(
    (a, b) => Number(b.id === activeId) - Number(a.id === activeId) || b.last_activity_ms - a.last_activity_ms,
  );

/** `list` with a saved `info` in it: replaced in place or appended; an archived one leaves a live
 * list (`archived` false) and stays in a full one. Does not mutate `list`. */
export const withWorkspace = (list: WorkspaceInfo[], info: WorkspaceInfo, archived = false): WorkspaceInfo[] => {
  if (info.archived && !archived) return list.filter((w) => w.id !== info.id);
  return list.some((w) => w.id === info.id) ? list.map((w) => (w.id === info.id ? info : w)) : [...list, info];
};
