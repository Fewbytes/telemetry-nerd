// Collapsed-panel state (telemetry-nerd-y5j4): a per-viewer UI preference ("fold this panel's
// chart away"), not workspace data — nobody else's view of the workspace should change because
// one viewer folded a panel, and it has no bearing on exports or findings. So, like theme
// (theme.svelte.ts), it lives in localStorage rather than round-tripping through the server.
//
// DOM guards keep the module importable in node-environment vitest tests (no localStorage
// there); tests stub it.

function readSaved(key: string): Set<string> {
  try {
    const raw = window.localStorage.getItem(key);
    const ids = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(ids) ? ids.filter((v) => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

export class CollapsedStore {
  #key: string;
  ids = $state<Set<string>>(new Set());

  constructor(key = "tn-collapsed-panels") {
    this.#key = key;
    this.ids = typeof window === "undefined" ? new Set() : readSaved(key);
  }

  has(id: string): boolean {
    return this.ids.has(id);
  }

  toggle(id: string): void {
    const next = new Set(this.ids);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.ids = next;
    try {
      window.localStorage.setItem(this.#key, JSON.stringify([...next]));
    } catch {
      /* private mode etc. — preference still applies for this session */
    }
  }
}

export const collapsedPanels = new CollapsedStore("tn-collapsed-panels");
export const collapsedSidebar = new CollapsedStore("tn-collapsed-sidebar");
