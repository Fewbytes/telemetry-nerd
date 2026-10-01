import { flushSync } from "svelte";
import { afterEach, describe, expect, it } from "vitest";
import { ThemeStore } from "./theme.svelte";

// node test env: stub the DOM surfaces the store touches. The exported
// singleton `theme` was built before stubbing (node guards) — tests
// instantiate fresh ThemeStore objects against controlled environments.

interface FakeWindow {
  matches: boolean;
  listeners: Array<(e: { matches: boolean }) => void>;
  store: Map<string, string>;
  doc: { dataset: Record<string, string | undefined> };
}

let fake: FakeWindow | null = null;

function installWindow({ matches, saved }: { matches: boolean; saved?: string }) {
  const listeners: Array<(e: { matches: boolean }) => void> = [];
  const store = new Map<string, string>();
  if (saved !== undefined) store.set("tn-theme", saved);
  const doc = { dataset: {} as Record<string, string | undefined> };
  const w = {
    matchMedia: () => ({
      matches,
      addEventListener: (_: string, cb: (e: { matches: boolean }) => void) => listeners.push(cb),
    }),
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    },
  };
  globalThis.window = w as unknown as Window & typeof globalThis;
  globalThis.document = { documentElement: doc } as unknown as Document;
  fake = { matches, listeners, store, doc };
  return fake;
}

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  fake = null;
});

describe("ThemeStore", () => {
  it("follows the OS preference when no choice is saved", () => {
    installWindow({ matches: true });
    const t = new ThemeStore();
    flushSync(); // $effect.root effects flush on microtask; force deterministically
    expect(t.setting).toBe("system");
    expect(t.effective).toBe("dark");
    expect(fake?.doc.dataset.theme).toBe("dark");
  });

  it("uses the saved choice over the OS preference", () => {
    installWindow({ matches: true, saved: "light" });
    const t = new ThemeStore();
    flushSync();
    expect(t.effective).toBe("light");
    expect(fake?.doc.dataset.theme).toBe("light");
  });

  it("ignores a corrupted saved value", () => {
    installWindow({ matches: false, saved: "hotdog" });
    const t = new ThemeStore();
    flushSync();
    expect(t.effective).toBe("light");
  });

  it("set() persists and applies immediately", () => {
    installWindow({ matches: false });
    const t = new ThemeStore();
    t.set("dark");
    flushSync();
    expect(t.effective).toBe("dark");
    expect(fake?.doc.dataset.theme).toBe("dark");
    expect(fake?.store.get("tn-theme")).toBe("dark");
  });

  it("reacts to OS scheme changes while on system", () => {
    installWindow({ matches: true });
    const t = new ThemeStore();
    expect(t.effective).toBe("dark");
    fake!.listeners.forEach((cb) => cb({ matches: false }));
    flushSync();
    expect(t.effective).toBe("light");
    expect(fake?.doc.dataset.theme).toBe("light");
  });

  it("keeps working when localStorage rejects writes", () => {
    installWindow({ matches: false });
    fake!.store.set = () => {
      throw new Error("quota");
    };
    const t = new ThemeStore();
    t.set("dark");
    flushSync();
    expect(t.setting).toBe("dark");
    expect(fake?.doc.dataset.theme).toBe("dark");
  });
});