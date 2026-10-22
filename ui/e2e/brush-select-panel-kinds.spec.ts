import { expect, importSeries, test } from "./fixtures.js";
import { brushMiddleThird, FIXTURE, mcpTool } from "./helpers.js";

// Minimal fleet-shaped series (bead a8u9: brush-select was only wired for "time" and
// "heatmap"; fleet/seasonal/spc/littles had cursor.drag disabled entirely, so dragging just
// left a stray cursor dot, no menu). A handful of flat members is enough to draw a band + at
// least one outlier line; the test only needs the chart to exist and accept a drag.
const PREFIX = `tn_e2e_brush_${Date.now()}`;
const STEP = 60_000;
const N = 30;
const END = Math.floor((Date.now() - 5 * 60_000) / STEP) * STEP;
const START = END - (N - 1) * STEP;

test.beforeAll(async ({ request }) => {
  const lines: string[] = [];
  for (let m = 0; m < 6; m++) {
    for (let i = 0; i < N; i++) {
      const t = START + i * STEP;
      const v = 10 + m + (m === 2 && i > N / 2 ? 8 : 0); // member 2 shifts up partway through
      lines.push(`${PREFIX}{pod="p${m}"} ${v} ${t}`);
    }
  }
  await importSeries(request, lines.join("\n") + "\n");
});

test("brush-select → Mark region works on a fleet panel (bead a8u9)", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: `sum by (pod) (${PREFIX})`, start: String(START), end: String(END), step: "1m" },
  });
  expect(q.ok(), await q.text()).toBeTruthy();
  const { dataset } = await q.json();
  const shown = await mcpTool(request, "show", { dataset, question: "fleet brush e2e", mark: "fleet" });
  const id = shown.panel;

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  await brushMiddleThird(page, id);
  const menu = page.locator(".selection-menu");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: "Mark region" }).click();
  await menu.getByPlaceholder("Region label").fill("fleet e2e region");
  await menu.getByRole("button", { name: "Save", exact: true }).click();
  await expect(menu).toBeHidden();

  await expect(el).toHaveAttribute("data-annotation-count", "1");
  const ws = await (await request.get("/api/workspace")).json();
  const ann = ws.annotations.find((a: { label: string }) => a.label === "fleet e2e region");
  expect(ann).toMatchObject({ kind: "region", author: "user", panel: id });
});

test("brush-select → Mark event works on a seasonal panel (bead a8u9)", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: `sum(${PREFIX}{pod="p0"})`, start: String(START), end: String(END), step: "1m" },
  });
  expect(q.ok(), await q.text()).toBeTruthy();
  const { dataset } = await q.json();
  await mcpTool(request, "compare_seasonal", { dataset });
  const shown = await mcpTool(request, "show", { dataset, question: "seasonal brush e2e", mark: "seasonal" });
  const id = shown.panel;

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  await brushMiddleThird(page, id);
  const menu = page.locator(".selection-menu");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: "Mark event" }).click();
  await expect(menu).toBeHidden();

  await expect(el).toHaveAttribute("data-annotation-count", "1");
});

test("brush-select → Mark region works on a spectrogram panel and the region is drawn (bead 2psp)", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: 'tn_demo_latency_seconds{instance="b"}', start: "now-6h", end: "now-10m", step: "1m" },
  });
  const { dataset } = await q.json();
  const shown = await request.post("/api/show", { data: { dataset, question: "spectrogram brush e2e", mark: "spectrogram", segment: "1h" } });
  expect(shown.ok(), await shown.text()).toBeTruthy();
  const id = (await shown.json()).panel.id;

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${id}"]`);
  const canvas = el.locator("canvas.spectrogram");
  await expect(canvas).toBeVisible();
  const pixels = () => canvas.evaluate((c: HTMLCanvasElement) => c.toDataURL());
  const before = await pixels();

  await brushMiddleThird(page, id);
  const menu = page.locator(".selection-menu");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: "Mark region" }).click();
  await menu.getByPlaceholder("Region label").fill("spectrogram e2e region");
  await menu.getByRole("button", { name: "Save", exact: true }).click();
  await expect(menu).toBeHidden();

  const ws = await (await request.get("/api/workspace")).json();
  expect(ws.annotations.find((a: { label: string }) => a.label === "spectrogram e2e region")).toMatchObject({ kind: "region", author: "user", panel: id });
  await expect.poll(pixels).not.toBe(before);
});

test.afterAll(async ({ request }) => {
  await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]={__name__="${PREFIX}"}`);
});
