// Collapsed-panel state (telemetry-nerd-y5j4): a per-viewer UI preference ("fold this panel's
// chart away"), not workspace data — nobody else's view of the workspace should change because
// one viewer folded a panel, and it has no bearing on exports or findings. So, like theme
// (theme.svelte.ts), it lives in localStorage rather than round-tripping through the server.
//
// DOM guards keep the module importable in node-environment vitest tests (no localStorage
// there); tests stub it.

const KEY = "tn-collapsed-panels";

function readSaved(): Set<string> {
  try {
    const raw = window.localStorage.getItem(KEY);
    const ids = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(ids) ? ids.filter((v) => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

export class CollapsedStore {
  ids = $state<Set<string>>(typeof window === "undefined" ? new Set() : readSaved());

  has(id: string): boolean {
    return this.ids.has(id);
  }

  toggle(id: string): void {
    const next = new Set(this.ids);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.ids = next;
    try {
      window.localStorage.setItem(KEY, JSON.stringify([...next]));
    } catch {
      /* private mode etc. — preference still applies for this session */
    }
  }
}

export const collapsedPanels = new CollapsedStore();
