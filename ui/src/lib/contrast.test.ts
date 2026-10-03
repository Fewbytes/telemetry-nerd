import { describe, expect, it } from "vitest";

// @ts-expect-error node typings are not a dependency; vitest runs under node
const { readFileSync } = (await import("node:fs")) as { readFileSync: (u: URL, enc: string) => string };
const css = readFileSync(new URL("../index.css", import.meta.url), "utf8");

// WCAG 2.x contrast of the colour pairs the findings / hypotheses panes use, in both themes,
// read from index.css so a token edit that breaks a pair fails here.

function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  const block = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  return Object.fromEntries([...block.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{3,6})/g)].map((m) => [m[1], m[2]]));
}
const light = tokens(":root {");
const dark = { ...light, ...tokens(':root[data-theme="dark"]') };

const lum = (hex: string): number => {
  const h = hex.length === 4 ? [...hex.slice(1)].map((c) => c + c).join("") : hex.slice(1);
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a: string, b: string): number => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

// [foreground token, background token, minimum]; text 4.5, non-text boundaries 3
const PAIRS: [string, string, number][] = [
  ["--fg", "--bg", 4.5], ["--muted", "--bg", 4.5], ["--muted", "--badge", 4.5], ["--fg", "--badge", 4.5],
  ["--warn", "--bg", 4.5], ["--warn", "--badge", 4.5], ["--error", "--bg", 4.5],
  ["--on-ok", "--ok", 4.5], ["--on-error", "--error", 4.5], ["--on-claude", "--ann-claude", 4.5],
  ["--ann-claude", "--bg", 4.5],
  ["--ok", "--bg", 3], ["--error", "--bg", 3], ["--warn", "--bg", 3], ["--border", "--bg", 1],
];

describe.each([["light", light], ["dark", dark]] as const)("%s theme contrast", (_n, t) => {
  it.each(PAIRS)("%s on %s >= %s", (fg, bg, min) => {
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(min);
  });
});
