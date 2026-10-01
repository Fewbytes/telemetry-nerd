import { describe, expect, it } from "vitest";
import { isSendKey, sendHint } from "./keys";

type KeyInit = { key?: string; metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean };

// node env has no KeyboardEvent constructor; isSendKey only reads key/meta/ctrl
const key = (overrides: KeyInit): KeyboardEvent =>
  ({ key: "Enter", metaKey: false, ctrlKey: false, ...overrides }) as unknown as KeyboardEvent;

describe("isSendKey", () => {
  const cases: Array<[string, KeyInit, boolean]> = [
    ["meta+enter", { metaKey: true }, true],
    ["ctrl+enter", { ctrlKey: true }, true],
    ["plain enter", {}, false],
    ["shift+enter", { shiftKey: true }, false],
    ["meta+a", { key: "a", metaKey: true }, false],
    ["ctrl without enter", { key: "Escape", ctrlKey: true }, false],
  ];
  for (const [name, init, expected] of cases) {
    it(name, () => {
      expect(isSendKey(key(init))).toBe(expected);
    });
  }
});

describe("sendHint", () => {
  it("says ⌘⏎ on mac platforms", () => {
    expect(sendHint("MacIntel")).toBe("⌘⏎ to send");
    expect(sendHint("iPhone")).toBe("⌘⏎ to send");
    expect(sendHint("iPad")).toBe("⌘⏎ to send");
  });
  it("says Ctrl+⏎ elsewhere", () => {
    expect(sendHint("Win32")).toBe("Ctrl+⏎ to send");
    expect(sendHint("Linux x86_64")).toBe("Ctrl+⏎ to send");
    expect(sendHint("")).toBe("Ctrl+⏎ to send");
  });
});