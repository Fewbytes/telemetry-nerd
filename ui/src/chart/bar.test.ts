import { expect, test } from "vitest";
import { BAR_LABEL_CHAR_PX, BAR_VALUE_ROOM_PX, barLabelGutterPx, layoutBarGroups, layoutBars, type BarInput } from "./bar";

const five: BarInput[] = [
  { label: "b", value: 3 }, { label: "a", value: 9 }, { label: "e", value: 1 },
  { label: "c", value: 7 }, { label: "d", value: 5 },
];

test("5 bars sort descending, sit on a zero baseline, and view-as-table has 5 rows in drawn order", () => {
  const l = layoutBars(five, "descending");
  expect(l.bars.map((b) => b.label)).toEqual(["a", "c", "d", "b", "e"]);
  expect(l.domain).toEqual([0, 9]);
  expect(l.baseline).toBe(0);
  expect(l.bars.every((b) => b.span![0] === 0)).toBe(true);
  expect(l.bars[0].span).toEqual([0, 1]);
  expect(l.orientation).toBe("vertical");
  expect(l.valueLabels).toBe(true);
  expect(l.table).toEqual([
    { label: "a", value: "9" }, { label: "c", value: "7" }, { label: "d", value: "5" },
    { label: "b", value: "3" }, { label: "e", value: "1" },
  ]);
});

test("order=given keeps the caller's order", () => {
  expect(layoutBars(five, "given").bars.map((b) => b.label)).toEqual(["b", "a", "e", "c", "d"]);
});

test("a negative value draws below the baseline and the domain still includes zero", () => {
  const l = layoutBars([{ label: "up", value: 6 }, { label: "down", value: -2 }], "descending");
  expect(l.domain).toEqual([-2, 6]);
  expect(l.baseline).toBe(0.25);
  expect(l.bars[1].span).toEqual([0, 0.25]);
  expect(l.bars[0].span).toEqual([0.25, 1]);
});

test("all-positive data is never truncated: a bar of 100 among 99..100 still starts at zero", () => {
  const l = layoutBars([{ label: "x", value: 100 }, { label: "y", value: 99 }], "descending");
  expect(l.domain[0]).toBe(0);
  expect(l.bars[1].span![0]).toBe(0);
});

test("a missing value is 'no value', not a zero-length bar, and sorts last", () => {
  const l = layoutBars([{ label: "gap", value: null }, { label: "a", value: 2 }], "descending");
  expect(l.bars.map((b) => b.label)).toEqual(["a", "gap"]);
  expect(l.bars[1].span).toBeNull();
  expect(l.table[1]).toEqual({ label: "gap", value: "no value" });
});

test("a label over 12 characters, or more than 12 bars, switches to horizontal", () => {
  expect(layoutBars([{ label: "x".repeat(12), value: 1 }], "given").orientation).toBe("vertical");
  expect(layoutBars([{ label: "x".repeat(13), value: 1 }], "given").orientation).toBe("horizontal");
  const many = Array.from({ length: 13 }, (_, i) => ({ label: `l${i}`, value: i }));
  expect(layoutBars(many, "given").orientation).toBe("horizontal");
  expect(layoutBars(many.slice(0, 12), "given").orientation).toBe("vertical");
});

test("value labels show with 12 bars and not with 13", () => {
  const many = Array.from({ length: 13 }, (_, i) => ({ label: `l${i}`, value: i }));
  expect(layoutBars(many.slice(0, 12), "given").valueLabels).toBe(true);
  expect(layoutBars(many, "given").valueLabels).toBe(false);
});

test("the label gutter fits the longest category label, so 'DomainNotConnected' is not clipped", () => {
  const l = layoutBars([{ label: "DomainNotConnected", value: 412 }, { label: "RateLimited", value: 90 }], "descending");
  expect(barLabelGutterPx(l.bars)).toBeGreaterThanOrEqual("DomainNotConnected".length * BAR_LABEL_CHAR_PX);
  expect(BAR_VALUE_ROOM_PX).toBeGreaterThan(String(412).length * BAR_LABEL_CHAR_PX); // the full-width bar's value label has room
});

test("grouped bars put each category's bars side by side on one shared domain and zero baseline", () => {
  const l = layoutBarGroups([
    { label: "incident", rows: [{ label: "a", value: 10 }, { label: "b", value: 2 }] },
    { label: "healthy day", rows: [{ label: "a", value: 5 }, { label: "b", value: 4 }] },
  ], "given");
  expect(l.seriesLabels).toEqual(["incident", "healthy day"]);
  expect(l.categories.map((c) => c.label)).toEqual(["a", "b"]);
  expect(l.categories[0].bars.map((b) => b.span)).toEqual([[0, 1], [0, 0.5]]); // same scale: 5 is half of 10
  expect(l.categories[1].bars.map((b) => b.span)).toEqual([[0, 0.2], [0, 0.4]]);
  expect(l.table).toEqual([{ label: "a", values: ["10", "5"] }, { label: "b", values: ["2", "4"] }]);
});
