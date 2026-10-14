// Mode store: "simple" (minimal panels) or "nerd" (everything). Two layers:
//  - default: a user setting, persisted in localStorage, "simple" unless changed;
//  - override: the header toggle, per browser session (sessionStorage).
// effective = override ?? default, mirrored to data-mode on <html>.
//
// DOM guards keep the module importable in node-environment vitest tests; tests stub
// window/document (see mode.test.ts), like theme.svelte.ts.

export type Mode = "simple" | "nerd";

const DEFAULT_KEY = "tn-mode-default";
const SESSION_KEY = "tn-mode";

function read(area: "localStorage" | "sessionStorage", key: string): Mode | null {
  try {
    const v = window[area].getItem(key);
    return v === "simple" || v === "nerd" ? v : null;
  } catch {
    return null;
  }
}

function write(area: "localStorage" | "sessionStorage", key: string, v: Mode): void {
  try {
    window[area].setItem(key, v);
  } catch {
    /* private mode etc. — the choice still applies for this page load */
  }
}

export class ModeStore {
  default = $state<Mode>(typeof window === "undefined" ? "simple" : (read("localStorage", DEFAULT_KEY) ?? "simple"));
  override = $state<Mode | null>(typeof window === "undefined" ? null : read("sessionStorage", SESSION_KEY));

  effective: Mode = $derived(this.override ?? this.default);

  constructor() {
    if (typeof document !== "undefined") this.apply();
  }

  setOverride(m: Mode): void {
    this.override = m;
    write("sessionStorage", SESSION_KEY, m);
    this.apply();
  }

  // Changing the default clears the override: otherwise the setting would appear to do nothing.
  setDefault(m: Mode): void {
    this.default = m;
    this.override = null;
    write("localStorage", DEFAULT_KEY, m);
    try {
      window.sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* as above */
    }
    this.apply();
  }

  // Imperative (not an $effect): deterministic and testable outside a component.
  private apply(): void {
    document.documentElement.dataset.mode = this.effective;
  }
}

export const mode = new ModeStore();
