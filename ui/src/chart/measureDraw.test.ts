import { describe, expect, it } from "vitest";
import { measureFirstDraw } from "./measureDraw";

describe("measureFirstDraw", () => {
  it("reports once, when the deferred draw fires, including its cost", async () => {
    let clock = 0;
    const reports: number[] = [];
    measureFirstDraw(
      (onDraw) => {
        clock += 5; // setup
        queueMicrotask(() => {
          clock += 50; // the draw itself
          onDraw();
          onDraw(); // later redraws must not re-report
        });
      },
      (ms) => reports.push(ms),
      () => clock,
    );
    expect(reports).toEqual([]); // nothing reported after setup alone
    await Promise.resolve();
    expect(reports).toEqual([55]);
  });
});
