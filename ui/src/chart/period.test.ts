import { expect, test } from "vitest";
import { fmtPeriod, periodTicks } from "./period";

test("human periods", () => {
  expect([30, 90, 300, 3600, 86400, 604800].map(fmtPeriod)).toEqual(["30s", "1.5m", "5m", "1h", "1d", "1w"]);
  expect(periodTicks(240, 172800)).toEqual([300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800]);
});
