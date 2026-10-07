import { expect, importSeries, test } from "./fixtures.js";
import { FIXTURE, mcpTool, seedPanel } from "./helpers";

// Per-panel export (bead telemetry-nerd-ulh9): PNG and PDF buttons in the panel toolbar, next to
// pin/close. Both trigger a real browser download — assert on the download event (filename,
// suggested type) and on the saved file's own magic bytes, not just that a click handler ran.
test("export: PNG and PDF buttons each save a real file for the panel's chart", async ({ page }) => {
  const question = "export: does this panel save as an image?";
  const panel = await seedPanel(page.request, question);
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  const [pngDownload] = await Promise.all([
    page.waitForEvent("download"),
    el.locator('[data-export="png"]').click(),
  ]);
  expect(pngDownload.suggestedFilename()).toBe(`telemetry-nerd-${panel.id}.png`);
  const pngPath = await pngDownload.path();
  expect(pngPath).not.toBeNull();
  const pngBytes = await (await import("node:fs/promises")).readFile(pngPath!);
  // PNG magic number
  expect(pngBytes.subarray(0, 8)).toEqual(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));

  const [pdfDownload] = await Promise.all([
    page.waitForEvent("download"),
    el.locator('[data-export="pdf"]').click(),
  ]);
  expect(pdfDownload.suggestedFilename()).toBe(`telemetry-nerd-${panel.id}.pdf`);
  const pdfPath = await pdfDownload.path();
  expect(pdfPath).not.toBeNull();
  const pdfBytes = await (await import("node:fs/promises")).readFile(pdfPath!, "latin1");
  expect(pdfBytes.startsWith("%PDF-")).toBe(true);
  expect(pdfBytes.trimEnd().endsWith("%%EOF")).toBe(true);
  // bead 71qs: the PDF carries the panel's question/provenance/notes, not just the bare chart
  expect(pdfBytes).toContain(panel.id);
  expect(pdfBytes).toContain(question);
  expect(pdfBytes).toContain("Notes & caveats");
});

// littles panels draw a discrepancy strip AND a main chart as separate <canvas> elements (two
// uPlot instances per series): the export must composite all of them, not just one, since the
// capture walks every canvas under `.plot` rather than special-casing a panel kind.
const PREFIX = `tn_e2e_export_littles_${Date.now()}`;
const STEP = 15_000;
const N = 60;
const END = Math.floor((Date.now() - 5 * 60_000) / STEP) * STEP;
const START = END - (N - 1) * STEP;

test.beforeAll(async ({ request }) => {
  const lines: string[] = [];
  let arrivals = 0, latSum = 0, latCount = 0;
  for (let i = 0; i < N; i++) {
    const t = START + i * STEP;
    const rate = 20 + 8 * Math.sin(i / 10);
    const w = 0.2;
    const reqs = (rate * STEP) / 1000;
    arrivals += reqs;
    latSum += reqs * w;
    latCount += reqs;
    const conc = Math.max(0, rate * w);
    lines.push(`${PREFIX}_arrivals_total ${arrivals.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_latency_seconds_sum ${latSum.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_latency_seconds_count ${latCount.toFixed(3)} ${t}`);
    lines.push(`${PREFIX}_concurrency ${conc.toFixed(3)} ${t}`);
  }
  await importSeries(request, lines.join("\n") + "\n");
  await mcpTool(request, "source_learn", { source: "default" });
});

test.afterAll(async ({ request }) => {
  await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]={__name__=~"${PREFIX}.*"}`);
});

test("export: a multi-canvas panel (littles) composites every canvas, not just one", async ({ page, request }) => {
  const check = await mcpTool(request, "check_littles_law", {
    arrival_rate: `${PREFIX}_arrivals_total`,
    latency: `${PREFIX}_latency_seconds`,
    concurrency: `${PREFIX}_concurrency`,
    start: String(START),
    end: String(END),
    window: "2m",
    detail: true,
  });
  const shown = await mcpTool(request, "show", {
    dataset: check.datasets.concurrency,
    question: "is L = lambda W?",
    mark: "littles",
  });
  const panelId = shown.panel;

  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panelId}"]`);
  const littles = el.locator(".littles").first();
  await expect(littles).toBeVisible();
  const plot = el.locator(":scope > .plot");
  const canvasCount = await plot.locator("canvas").count();
  expect(canvasCount).toBeGreaterThan(1); // discrepancy strip + main chart, at minimum

  const [pngDownload] = await Promise.all([
    page.waitForEvent("download"),
    el.locator('[data-export="png"]').click(),
  ]);
  const pngPath = await pngDownload.path();
  expect(pngPath).not.toBeNull();
  const fs = await import("node:fs/promises");
  const pngBytes = await fs.readFile(pngPath!);
  expect(pngBytes.subarray(0, 8)).toEqual(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));

  // the composite must be at least as tall as the strip+chart stack, not just one canvas's height
  const plotBox = (await plot.boundingBox())!;
  const firstCanvasBox = (await plot.locator("canvas").first().boundingBox())!;
  expect(plotBox.height).toBeGreaterThan(firstCanvasBox.height * 1.3);
  // a PNG that only captured one canvas would be a small, mostly-empty file; a composite of the
  // whole stack is comfortably larger — a cheap proxy for "more than one canvas got drawn"
  expect(pngBytes.length).toBeGreaterThan(5_000);
});
