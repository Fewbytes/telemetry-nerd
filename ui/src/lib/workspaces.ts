import type { Snapshot, WorkspaceInfo } from "./api";

/** True when `next` belongs to a different workspace than `prev` (the first snapshot is not a switch). */
export const workspaceChanged = (prev: Snapshot | null, next: Snapshot): boolean =>
  prev !== null && prev.workspace.id !== next.workspace.id;

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
