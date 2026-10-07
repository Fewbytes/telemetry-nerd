// A panel's chart height: a per-viewer UI preference, not server state (there is no
// workspace/panel-spec field for it), so it is persisted the same way the theme choice is
// (lib/theme.svelte.ts) — one key per panel in localStorage, read/write guarded against a
// private-mode/quota throw so the picked height still applies for the current session.

const KEY_PREFIX = "tn-panel-height-";

export const DEFAULT_PANEL_HEIGHT = 420;
export const MIN_PANEL_HEIGHT = 160;
export const MAX_PANEL_HEIGHT = 1000;

export function clampPanelHeight(h: number): number {
  return Math.min(MAX_PANEL_HEIGHT, Math.max(MIN_PANEL_HEIGHT, Math.round(h)));
}

/** The height while dragging the bottom-edge handle: where it started, plus how far the pointer moved. */
export function resizedHeight(startHeight: number, deltaY: number): number {
  return clampPanelHeight(startHeight + deltaY);
}

function storageKey(panelId: string): string {
  return `${KEY_PREFIX}${panelId}`;
}

export function readPanelHeight(panelId: string): number | null {
  try {
    const v = window.localStorage.getItem(storageKey(panelId));
    if (!v) return null;
    const n = Number(v);
    return Number.isFinite(n) ? clampPanelHeight(n) : null;
  } catch {
    return null;
  }
}

export function writePanelHeight(panelId: string, height: number): void {
  try {
    window.localStorage.setItem(storageKey(panelId), String(clampPanelHeight(height)));
  } catch {
    /* private mode / quota — the choice still applies for this session */
  }
}
