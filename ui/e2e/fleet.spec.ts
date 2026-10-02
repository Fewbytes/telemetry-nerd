import { expect, test, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

// 100-member synthetic fleet, 3 planted outliers (persistent, transient, drifting), written under a
// unique metric name so the shared dev VictoriaMetrics keeps its other data untouched.
const METRIC = `tn_e2e_lkn_fleet_cpu_${Date.now()}`; // fresh per run: stale samples at shifted timestamps would add outliers
const VM = "http://127.0.0.1:8428";
const SHOTS = "e2e-shots";
const PLANTED = { persistent: 7, transient: 23, drifting: 61 };
const pod = (i: number) => `pod=api-${String(i).padStart(3, "0")}`;

function rng(seed: number) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}
const gauss = (r: () => number) => Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r());

function exposition(): string {
  const r = rng(42);
  const T = 300, step = 60_000, end = Math.floor((Date.now() - 6 * 60_000) / step) * step, start = end - (T - 1) * step;
  const lines: string[] = [];
  for (let m = 0; m < 100; m++) {
    const level = gauss(r) * 0.05;
    let e = 0;
    for (let t = 0; t < T; t++) {
      e = 0.6 * e + 0.8 * 0.1 * gauss(r);
      let lv = level + e;
      if (m === PLANTED.persistent) lv += 0.6;
      if (m === PLANTED.transient && t >= T / 2 && t < T / 2 + 14) lv += 1.0;
      if (m === PLANTED.drifting) lv += (0.8 * t) / (T - 1);
      if (m === 40 && t >= 100 && t < 130) continue; // a real hole: silent while alive
      const v = (50 + 20 * Math.sin((2 * Math.PI * t) / T)) * Math.exp(lv);
      lines.push(`${METRIC}{pod="api-${String(m).padStart(3, "0")}",job="api"} ${v.toFixed(4)} ${start + t * step}`);
    }
  }
  return lines.join("\n") + "\n";
}

async function pixels(page: Page, sel: string, rgb: [number, number, number]): Promise<number> {
  return page.evaluate(([s, c]) => {
    const cv = document.querySelector(s) as HTMLCanvasElement;
    const d = cv.getContext("2d")!.getImageData(0, 0, cv.width, cv.height).data;
    let n = 0;
    for (let i = 0; i < d.length; i += 4)
      if (d[i + 3] > 200 && Math.abs(d[i] - c[0]) < 24 && Math.abs(d[i + 1] - c[1]) < 24 && Math.abs(d[i + 2] - c[2]) < 24) n++;
    return n;
  }, [sel, rgb] as const);
}

const OUTLIER_RGB: [number, number, number][] = [[0xd5, 0x5e, 0x00], [0x00, 0x72, 0xb2], [0xcc, 0x79, 0xa7]];

test("fleet panel: band, 3 planted outliers labelled, heatmap and small multiples within budget, both themes", async ({ page, request }) => {
  mkdirSync(SHOTS, { recursive: true });
  const put = await request.post(`${VM}/api/v1/import/prometheus`, { data: exposition() });
  expect(put.ok()).toBeTruthy();
  await request.get(`${VM}/internal/force_flush`);
  const q = await request.post("/api/query", {
    data: { expr: METRIC, start: "now-310m", end: "now-5m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "Which api pods are off?", mark: "fleet" } });
  expect(s.ok(), await s.text()).toBeTruthy();
  const { panel } = await s.json();

  await page.setViewportSize({ width: 1200, height: 1400 });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const fleet = el.locator("[data-fleet-members]");
  await expect(fleet).toHaveAttribute("data-fleet-members", "100");
  await expect(fleet).toHaveAttribute("data-fleet-outliers", "3");
  await expect(el).toHaveAttribute("data-budget-exceeded", "false");
  const outlierList = el.locator(".outliers li");
  await expect(outlierList).toHaveCount(3);
  for (const p of Object.values(PLANTED)) await expect(outlierList.filter({ hasText: pod(p) })).toHaveCount(1);
  const timings: Record<string, string | null> = {};

  for (const mode of ["light", "dark"] as const) {
    await page.getByLabel("Theme").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    // band view: each outlier's colour (line and label) is drawn on the plot canvas
    await fleet.locator("[data-fleet-view-btn=band]").click();
    await expect(el.locator(".y-views")).toHaveCount(1);
    const plot = el.locator(".u-over");
    await expect(plot).toBeVisible();
    await expect(el.locator("[data-fleet-encoding]")).toContainText("spread across 100 members (bands: 25–75, 10–90, min–max)");
    await expect(el.locator("[data-fleet-key] li")).toHaveCount(5);
    for (const rgb of OUTLIER_RGB) {
      await expect.poll(() => pixels(page, `[data-panel-id="${panel.id}"] canvas`, rgb)).toBeGreaterThan(20);
    }
    await expect(el).toHaveAttribute("data-budget-exceeded", "false");
    timings[`${mode}/band`] = await el.getAttribute("data-render-ms");
    await el.screenshot({ path: `${SHOTS}/fleet-band-${mode}.png` });

    // member x time heatmap: all 100 rows, outlier rows labelled, textured gap, no budget breach
    await fleet.locator("[data-fleet-view-btn=heat]").click();
    const heat = el.locator("[data-fleet-heat]");
    await expect(heat).toHaveAttribute("data-fleet-heat-rows", "100");
    await expect.poll(() => el.getAttribute("data-render-ms")).not.toBe(timings[`${mode}/band`]);
    await expect(el).toHaveAttribute("data-budget-exceeded", "false");
    timings[`${mode}/heat`] = await el.getAttribute("data-render-ms");
    const cv = heat.locator("canvas");
    const box = (await cv.boundingBox())!;
    await page.mouse.move(box.x + 150 + 40, box.y + 6); // top row: a higher outlier
    await expect(heat.locator(".tip")).toContainText("z ");
    await expect(heat.locator("[data-fleet-heat-bar]")).toContainText("deviation from fleet median (σ)");
    await expect(el.locator(".what")).toContainText("one row per member");
    await expect(el.locator(".y-views")).toHaveCount(0);
    await expect(el.getByText("scaled to the data")).toHaveCount(0);
    await el.screenshot({ path: `${SHOTS}/fleet-heat-${mode}.png` });

    // small multiples of the three outliers: one panel each, identical y
    await fleet.locator("[data-fleet-view-btn=multiples]").click();
    await expect(el.locator("[data-fleet-multiples]")).toHaveAttribute("data-fleet-multiples", "3");
    await expect(el.locator("[data-fleet-multiples] canvas")).toHaveCount(3);
    await expect(el.locator("[data-fleet-multiples] figcaption")).toHaveCount(3);
    await expect(el).toHaveAttribute("data-budget-exceeded", "false");
    timings[`${mode}/multiples`] = await el.getAttribute("data-render-ms");
    await el.screenshot({ path: `${SHOTS}/fleet-multiples-${mode}.png` });
  }
  console.log("fleet render ms", JSON.stringify(timings));
  for (const v of Object.values(timings)) expect(Number(v)).toBeLessThan(100);
});

test("fleet panel with 6 members: band view screenshot", async ({ page, request }) => {
  mkdirSync(SHOTS, { recursive: true });
  const metric = "tn_e2e_2ju_fleet6";
  const r = rng(7);
  const T = 240, step = 60_000, end = Math.floor((Date.now() - 6 * 60_000) / step) * step, start = end - (T - 1) * step;
  const lines: string[] = [];
  for (let m = 0; m < 6; m++)
    for (let t = 0; t < T; t++) {
      const v = 40 + 15 * Math.sin((2 * Math.PI * t) / T) + gauss(r) * 3 + (m === 2 && t > 150 ? 30 : 0);
      lines.push(`${metric}{core="${m}",job="cpu"} ${v.toFixed(3)} ${start + t * step}`);
    }
  expect((await request.post(`${VM}/api/v1/import/prometheus`, { data: lines.join("\n") + "\n" })).ok()).toBeTruthy();
  await request.get(`${VM}/internal/force_flush`);
  const q = await request.post("/api/query", { data: { expr: metric, start: "now-250m", end: "now-5m", step: "1m" } });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "Which core is off?", mark: "fleet" } });
  expect(s.ok(), await s.text()).toBeTruthy();
  const { panel } = await s.json();
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-fleet-encoding]")).toContainText("spread across 6 members");
  for (const mode of ["light", "dark"] as const) {
    await page.getByLabel("Theme").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    await expect(el.locator(".u-over")).toBeVisible();
    await el.screenshot({ path: `${SHOTS}/fleet6-band-${mode}.png` });
  }
});
