// Theme store: explicit user preference (system/light/dark) persisted in
// localStorage, applied as data-theme on <html>. "system" follows
// prefers-color-scheme live via the matchMedia change event.
//
// DOM guards keep the module importable in node-environment vitest tests
// (no matchMedia/localStorage/document there); tests stub them.

export type Theme = "system" | "light" | "dark";
export type Resolved = "light" | "dark";

const KEY = "tn-theme";

function readSaved(): Theme {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system";
  }
}

function systemPrefers(): Resolved {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

export class ThemeStore {
  setting = $state<Theme>(typeof window === "undefined" ? "system" : readSaved());
  system = $state<Resolved>(typeof window === "undefined" ? "light" : systemPrefers());

  effective: Resolved = $derived(this.setting === "system" ? this.system : this.setting);

  constructor() {
    if (typeof window === "undefined") return;
    try {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", (e) => {
        this.system = e.matches ? "dark" : "light";
        this.apply();
      });
    } catch {
      /* no matchMedia (tests, old engines): system stays at the initial guess */
    }
    this.apply();
  }

  set(t: Theme): void {
    this.setting = t;
    this.apply();
    try {
      window.localStorage.setItem(KEY, t);
    } catch {
      /* private mode etc. — preference still applies for this session */
    }
  }

  // Imperative (not an $effect): keeps the DOM write deterministic and
  // testable outside a component — every mutation point calls this.
  private apply(): void {
    document.documentElement.dataset.theme = this.effective;
  }
}

export const theme = new ThemeStore();

// uPlot bakes colors into the canvas at draw time, so the plot rebuilds on
// theme change; Panel.svelte reads the CSS tokens via this (mode is a tracked
// dependency so the effect re-runs when the theme flips).
export function plotColors(
  el: HTMLElement,
  mode: Resolved,
): { stroke: string; grid: string } {
  void mode; // tracked: rebuild plot when theme changes
  const cs = getComputedStyle(el);
  const token = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return { stroke: token("--muted", "#666"), grid: token("--grid", "#e8e8e8") };
}