import { expect, test } from "vitest";
import { colormap } from "./colormap";
import { matrixCellColor, matrixCellTip } from "./matrixHeat";

test("min maps to the dark end, max to the light end, a missing cell has no colour", () => {
  const viridis = colormap("viridis");
  expect(matrixCellColor(0, 0, 99)).toBe(viridis(0));
  expect(matrixCellColor(99, 0, 99)).toBe(viridis(1));
  expect(matrixCellColor(49.5, 0, 99)).toBe(viridis(0.5));
  expect(matrixCellColor(null, 0, 99)).toBeNull();
});

test("a constant matrix does not divide by zero", () => {
  expect(matrixCellColor(5, 5, 5)).toBe(colormap("viridis")(0));
});

test("the tooltip names row, column and value, or says missing", () => {
  expect(matrixCellTip("r1", "c2", 12)).toBe("r1 · c2 · 12");
  expect(matrixCellTip("r1", "c2", null)).toBe("r1 · c2 · missing");
});
