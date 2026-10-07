import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_PANEL_HEIGHT,
  MAX_PANEL_HEIGHT,
  MIN_PANEL_HEIGHT,
  clampPanelHeight,
  readPanelHeight,
  resizedHeight,
  writePanelHeight,
} from "./panelHeight";

describe("clampPanelHeight", () => {
  it("passes through a value inside the range", () => {
    expect(clampPanelHeight(DEFAULT_PANEL_HEIGHT)).toBe(DEFAULT_PANEL_HEIGHT);
  });
  it("floors at the minimum", () => {
    expect(clampPanelHeight(10)).toBe(MIN_PANEL_HEIGHT);
  });
  it("ceils at the maximum", () => {
    expect(clampPanelHeight(5000)).toBe(MAX_PANEL_HEIGHT);
  });
  it("rounds a fractional value", () => {
    expect(clampPanelHeight(420.6)).toBe(421);
  });
});

describe("resizedHeight", () => {
  it("adds the drag delta to the starting height", () => {
    expect(resizedHeight(400, 50)).toBe(450);
  });
  it("shrinks on a negative delta", () => {
    expect(resizedHeight(400, -50)).toBe(350);
  });
  it("never drags below the minimum", () => {
    expect(resizedHeight(200, -1000)).toBe(MIN_PANEL_HEIGHT);
  });
  it("never drags above the maximum", () => {
    expect(resizedHeight(900, 1000)).toBe(MAX_PANEL_HEIGHT);
  });
});

// node test env: stub window.localStorage like theme.test.ts does for ThemeStore.
interface FakeWindow {
  store: Map<string, string>;
}
let fake: FakeWindow | null = null;

function installWindow() {
  const store = new Map<string, string>();
  const w = {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    },
  };
  globalThis.window = w as unknown as Window & typeof globalThis;
  fake = { store };
  return fake;
}

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
  fake = null;
});

describe("readPanelHeight / writePanelHeight", () => {
  it("returns null when nothing was saved for this panel", () => {
    installWindow();
    expect(readPanelHeight("p1")).toBeNull();
  });

  it("round-trips a written height", () => {
    installWindow();
    writePanelHeight("p1", 500);
    expect(readPanelHeight("p1")).toBe(500);
  });

  it("clamps a written value before storing it", () => {
    installWindow();
    writePanelHeight("p1", 9999);
    expect(readPanelHeight("p1")).toBe(MAX_PANEL_HEIGHT);
  });

  it("keys storage per panel id", () => {
    installWindow();
    writePanelHeight("p1", 500);
    writePanelHeight("p2", 600);
    expect(readPanelHeight("p1")).toBe(500);
    expect(readPanelHeight("p2")).toBe(600);
  });

  it("ignores a corrupted saved value", () => {
    const f = installWindow();
    f.store.set("tn-panel-height-p1", "not-a-number");
    expect(readPanelHeight("p1")).toBeNull();
  });

  it("keeps working when localStorage rejects writes", () => {
    const f = installWindow();
    f.store.set = () => {
      throw new Error("quota");
    };
    expect(() => writePanelHeight("p1", 500)).not.toThrow();
    expect(readPanelHeight("p1")).toBeNull();
  });

  it("returns null when localStorage.getItem throws (Safari private mode)", () => {
    const f = installWindow();
    f.store.get = () => {
      throw new Error("blocked");
    };
    expect(() => readPanelHeight("p1")).not.toThrow();
    expect(readPanelHeight("p1")).toBeNull();
  });

  it("returns null when window/localStorage is unavailable", () => {
    delete (globalThis as { window?: unknown }).window;
    expect(readPanelHeight("p1")).toBeNull();
    expect(() => writePanelHeight("p1", 500)).not.toThrow();
  });
});
