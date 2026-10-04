import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures.js";
import { mkdirSync } from "node:fs";
import { FIXTURE } from "./helpers.js";

// 100-member synthetic fleet, 3 planted outliers (persistent, transient, drifting), imported into the
// fixture source under a unique metric name per test attempt: stale samples at shifted timestamps
// would add outliers.
const SHOTS = "e2e-shots";
const PLANTED = { persistent: 7, transient: 23, drifting: 61 };
const pod = (i: number) => `pod=api-${String(i).padStart(3, "0")}`;

function rng(seed: number) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}
const gauss = (r: () => number) => Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r());

// Each test queries exactly the grid it imported (absolute start/end, bead ax1s): the same
// buckets every run, whatever minute the suite reaches the test in.
function exposition(metric: string, start: number): string {
  const r = rng(42);
  const T = 300, step = 60_000;
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
      lines.push(`${metric}{pod="api-${String(m).padStart(3, "0")}",job="api"} ${v.toFixed(4)} ${start + t * step}`);
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

test("fleet panel: band, 3 planted outliers labelled, heatmap and small multiples within budget, both themes", async ({ page, request, uniq }) => {
  const metric = `tn_e2e_lkn_fleet_cpu_${uniq}`;
  mkdirSync(SHOTS, { recursive: true });
  const end = Math.floor((Date.now() - 6 * 60_000) / 60_000) * 60_000, start = end - 299 * 60_000;
  const put = await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: exposition(metric, start) });
  expect(put.ok()).toBeTruthy();
  await request.get(`${FIXTURE}/internal/force_flush`);
  const q = await request.post("/api/query", {
    data: { expr: metric, start: String(start), end: String(end), step: "1m" },
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
    // SPC band (nq6): the reference the tests use, the flag threshold, distinct marks per mode
    await expect(el.locator("[data-fleet-encoding]")).toContainText("median ± 2σ/3σ (robust, pooled ±6 steps, log scale: multiplicative) across 100 members");
    for (const id of ["z3", "z2", "median", "flag", "outlier", "transient"]) await expect(el.locator(`[data-fleet-key-id=${id}]`)).toHaveCount(1);
    await expect(el.locator("[data-fleet-legend]")).toContainText(/\d+ member-steps beyond 3σ unflagged \([\d.]+%; 0\.27% if normal\)/);
    await expect(el.locator("[data-fleet-key-id=flag]")).toHaveAttribute("title", /approximate.*leave-one-out/);
    await expect(el.locator("[data-fleet-legend]")).toContainText("member measurement error not propagated");
    await expect(el.locator("[data-fleet-band]")).toHaveAttribute("data-fleet-band", "spc");
    await expect(el.locator(`.outliers li[data-fleet-kind=transient]`)).toContainText(/(spike|episode) [\d.]+σ/);
    await expect(el.locator(`.outliers li[data-fleet-kind=persistent]`)).toContainText(/[+×][\d.]+%? since/);
    expect(Number(await el.locator("[data-fleet-brackets]").getAttribute("data-fleet-brackets"))).toBeGreaterThanOrEqual(1);
    for (const rgb of OUTLIER_RGB) {
      await expect.poll(() => pixels(page, `[data-panel-id="${panel.id}"] canvas`, rgb)).toBeGreaterThan(20);
    }
    await expect(el).toHaveAttribute("data-budget-exceeded", "false");
    timings[`${mode}/band`] = await el.getAttribute("data-render-ms");
    // end labels (cis): one per drawn outlier, no two boxes overlap
    const boxes = JSON.parse((await el.locator("[data-fleet-labels]").getAttribute("data-fleet-labels")) ?? "[]") as number[][];
    expect(boxes).toHaveLength(3);
    for (const [i, a] of boxes.entries())
      for (const b of boxes.slice(i + 1))
        expect(a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3], JSON.stringify(boxes)).toBe(false);
    await el.screenshot({ path: `${SHOTS}/fleet-band-${mode}.png` });

    // quantile toggle: the descriptive spread, with missing-member bounds where pod 40 was silent
    await fleet.locator("[data-fleet-view-btn=quantiles]").click();
    await expect(el.locator("[data-fleet-encoding]")).toContainText("spread across 100 members (bands: 25–75, 10–90, min–max)");
    await expect(el.locator("[data-fleet-key-id=bounds]")).toContainText("missing-member bounds");
    await expect(el.locator("[data-fleet-band]")).toHaveAttribute("data-fleet-band", "quantiles");
    await expect(el).toHaveAttribute("data-budget-exceeded", "false");
    await el.screenshot({ path: `${SHOTS}/fleet-quantiles-${mode}.png` });

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

test("fleet panel with 6 members: band view screenshot", async ({ page, request, uniq }) => {
  mkdirSync(SHOTS, { recursive: true });
  const metric = `tn_e2e_2ju_fleet6_${uniq}`;
  const r = rng(7);
  const T = 240, step = 60_000, end = Math.floor((Date.now() - 6 * 60_000) / step) * step, start = end - (T - 1) * step;
  const lines: string[] = [];
  for (let m = 0; m < 6; m++)
    for (let t = 0; t < T; t++) {
      const v = 40 + 15 * Math.sin((2 * Math.PI * t) / T) + gauss(r) * 3 + (m === 2 && t > 150 ? 30 : 0);
      lines.push(`${metric}{core="${m}",job="cpu"} ${v.toFixed(3)} ${start + t * step}`);
    }
  expect((await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: lines.join("\n") + "\n" })).ok()).toBeTruthy();
  await request.get(`${FIXTURE}/internal/force_flush`);
  const q = await request.post("/api/query", { data: { expr: metric, start: String(start), end: String(end), step: "1m" } });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "Which core is off?", mark: "fleet" } });
  expect(s.ok(), await s.text()).toBeTruthy();
  const { panel } = await s.json();
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-fleet-encoding]")).toContainText("across 6 members");
  for (const mode of ["light", "dark"] as const) {
    await page.getByLabel("Theme").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    await expect(el.locator(".u-over")).toBeVisible();
    await el.screenshot({ path: `${SHOTS}/fleet6-band-${mode}.png` });
  }
});

test("fleet split into behaviour groups: per-group bands and medians, tagged outliers, hatched unknown data (oyi)", async ({ page, request, uniq }) => {
  mkdirSync(SHOTS, { recursive: true });
  const metric = `tn_e2e_oyi_fleet_groups_${uniq}`;
  const r = rng(11);
  const T = 240, step = 60_000, end = Math.floor((Date.now() - 6 * 60_000) / step) * step, start = end - (T - 1) * step;
  const lines: string[] = [];
  for (let m = 0; m < 24; m++) {
    const base = m < 12 ? 40 : 90; // two behaviour groups (e.g. two instance sizes)
    const own = gauss(r) * 1.5;
    for (let t = 0; t < T; t++) {
      let v = base + own + 8 * Math.sin((2 * Math.PI * t) / T) + gauss(r) * 1.2;
      if (m === 17 && t > 120) v += 22; // an outlier within the upper group: inside the whole-fleet band
      if (m === 5 && t > 160) v += 14; // and one within the lower group
      lines.push(`${metric}{node="n${String(m).padStart(2, "0")}",job="db"} ${v.toFixed(3)} ${start + t * step}`);
    }
  }
  expect((await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: lines.join("\n") + "\n" })).ok()).toBeTruthy();
  await request.get(`${FIXTURE}/internal/force_flush`);
  const q = await request.post("/api/query", { data: { expr: metric, start: String(start), end: String(end), step: "1m" } });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "Which db nodes are off?", mark: "fleet" } });
  expect(s.ok(), await s.text()).toBeTruthy();
  const { panel } = await s.json();
  // a fetch failure cannot be planted in the source data: add a located untrusted span to the payload
  let spanEnd = 0;
  await page.route(`**/api/panels/${panel.id}/data*`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    const ts: number[] = body.ts;
    spanEnd = ts[Math.floor(ts.length * 0.35)];
    body.located = [...(body.located ?? []), {
      code: "untrusted_data", severity: "warn", source: "bucket_state",
      message: "Data unknown for 20m (planted by the e2e test)", where: { spans: [[ts[Math.floor(ts.length * 0.27)], spanEnd]], series: null },
    }];
    await route.fulfill({ response: res, json: body });
  });
  await page.setViewportSize({ width: 1200, height: 1000 });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-fleet-encoding]")).toContainText("2 behaviour groups");
  await expect(el.locator("[data-fleet-key-id=group-c1]")).toContainText("group c1 (12)");
  await expect(el.locator("[data-fleet-key-id=group-c2]")).toContainText("group c2 (12)");
  await expect(el.locator("[data-fleet-key-id=unknown]")).toContainText("data unknown");
  await expect(el.locator(".what")).toContainText("Fleet in behaviour groups");
  const list = el.locator(".outliers li");
  await expect(list.filter({ hasText: "node=n17" })).toContainText("within group c");
  await expect(list.filter({ hasText: "node=n05" })).toContainText("within group c");
  expect(spanEnd).toBeGreaterThan(0);
  for (const mode of ["light", "dark"] as const) {
    await page.getByLabel("Theme").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    await expect(el.locator(".u-over")).toBeVisible();
    const boxes = JSON.parse((await el.locator("[data-fleet-labels]").getAttribute("data-fleet-labels")) ?? "[]") as number[][];
    expect(boxes.length).toBeGreaterThanOrEqual(4); // 2 outliers + 2 group medians
    for (const [i, a] of boxes.entries())
      for (const b of boxes.slice(i + 1))
        expect(a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3], JSON.stringify(boxes)).toBe(false);
    await el.screenshot({ path: `${SHOTS}/fleet-groups-${mode}.png` });
  }
});
