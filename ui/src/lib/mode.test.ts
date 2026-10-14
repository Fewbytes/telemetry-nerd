import { flushSync } from "svelte";
import { afterEach, describe, expect, it } from "vitest";
import { ModeStore } from "./mode.svelte";

// node env: stub the DOM surfaces the store touches (same approach as theme.test.ts).
interface Fake {
  local: Map<string, string>;
  session: Map<string, string>;
  doc: { dataset: Record<string, string | undefined> };
}
let fake: Fake | null = null;

function storage(m: Map<string, string>) {
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => m.set(k, v),
    removeItem: (k: string) => m.delete(k),
  };
}
function install(opts: { local?: Record<string, string>; session?: Record<string, string> } = {}) {
  const local = new Map(Object.entries(opts.local ?? {}));
  const session = new Map(Object.entries(opts.session ?? {}));
  const doc = { dataset: {} as Record<string, string | undefined> };
  globalThis.window = { localStorage: storage(local), sessionStorage: storage(session) } as unknown as Window & typeof globalThis;
  globalThis.document = { documentElement: doc } as unknown as Document;
  fake = { local, session, doc };
  return fake;
}
afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
  delete (globalThis as { document?: unknown }).document;
  fake = null;
});

describe("ModeStore", () => {
  it("defaults to simple and applies data-mode", () => {
    install();
    const m = new ModeStore();
    flushSync();
    expect(m.effective).toBe("simple");
    expect(fake?.doc.dataset.mode).toBe("simple");
  });

  it("uses the saved default", () => {
    install({ local: { "tn-mode-default": "nerd" } });
    const m = new ModeStore();
    expect(m.effective).toBe("nerd");
    expect(fake?.doc.dataset.mode).toBe("nerd");
  });

  it("session override beats the default", () => {
    install({ local: { "tn-mode-default": "nerd" }, session: { "tn-mode": "simple" } });
    const m = new ModeStore();
    expect(m.effective).toBe("simple");
  });

  it("ignores corrupted stored values", () => {
    install({ local: { "tn-mode-default": "hotdog" }, session: { "tn-mode": "x" } });
    const m = new ModeStore();
    expect(m.default).toBe("simple");
    expect(m.override).toBeNull();
  });

  it("setOverride writes sessionStorage only and applies immediately", () => {
    install();
    const m = new ModeStore();
    m.setOverride("nerd");
    flushSync();
    expect(m.effective).toBe("nerd");
    expect(fake?.session.get("tn-mode")).toBe("nerd");
    expect(fake?.local.has("tn-mode-default")).toBe(false);
    expect(fake?.doc.dataset.mode).toBe("nerd");
  });

  it("setDefault persists, clears the override, and applies", () => {
    install({ session: { "tn-mode": "nerd" } });
    const m = new ModeStore();
    m.setDefault("simple");
    flushSync();
    expect(fake?.local.get("tn-mode-default")).toBe("simple");
    expect(fake?.session.has("tn-mode")).toBe(false);
    expect(m.override).toBeNull();
    expect(m.effective).toBe("simple");
    expect(fake?.doc.dataset.mode).toBe("simple");
  });

  it("keeps working when storage rejects writes", () => {
    const f = install();
    f.local.set = () => { throw new Error("quota"); };
    f.session.set = () => { throw new Error("quota"); };
    const m = new ModeStore();
    m.setOverride("nerd");
    m.setDefault("nerd");
    flushSync();
    expect(m.effective).toBe("nerd");
  });

  it("is importable without a window (node)", () => {
    const m = new ModeStore();
    expect(m.effective).toBe("simple");
  });
});
