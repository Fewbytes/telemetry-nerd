import { expect, test } from "./fixtures.js";

// Spectrum, spectrogram and filtered/raw data views. Needs `just dev-up && just seed 8` and the daemon.
test("spectrum finds the hourly period; filtered panel switches views and persists", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: 'tn_demo_latency_seconds{instance="b"}', start: "now-6h", end: "now-10m", step: "1m" },
  });
  const { dataset } = await q.json();
  const sp = await request.post("/api/show", { data: { dataset, question: "Which periods does demo latency contain?", mark: "spectrum" } });
  expect(sp.ok()).toBeTruthy();
  const spec = (await sp.json()).panel;
  await page.goto("/");
  const spectrum = page.locator(`[data-panel-id="${spec.id}"]`);
  await expect(spectrum.locator("[data-spectrum-peaks]")).not.toHaveAttribute("data-spectrum-peaks", "0");
  await expect(spectrum).toContainText("false-alarm");
});
