import { execSync } from "node:child_process";
import type { APIRequestContext, Locator, Page } from "@playwright/test";
import { expect, importSeries, test } from "./fixtures.js";
import { FIXTURE, mcpTool } from "./helpers.js";

// Simple mode for the non-time kinds and binding groups (bead 2knd.8): view switches and encoding
// keys stay, appearance/analysis pickers go (the per-panel peek still has them), and dense caption
// paragraphs become one ellipsised line that opens in place, the full text in its title. Each test
// first checks the nerd rendering (the fixtures pin nerd) so nerd mode keeps its pickers and full
// captions, then switches to simple.
const P = `tn_e2e_simplekinds_${Date.now()}`;
const STEP = 60_000;
const N = 180;
const END = Math.floor((Date.now() - 6 * 60_000) / STEP) * STEP;
const START = END - (N - 1) * STEP;
const LSTEP = 15_000;
const LN = 240;
const LEND = Math.floor((Date.now() - 5 * 60_000) / LSTEP) * LSTEP;
const LSTART = LEND - (LN - 1) * LSTEP;
const DSTEP = 300_000;
const DEND = Math.floor((Date.now() - 10 * 60_000) / DSTEP) * DSTEP;

test.beforeAll(async ({ request }) => {
  const lines: string[] = [];
  for (let i = 0; i < N; i++) {
    const t = START + i * STEP;
    for (let m = 0; m < 8; m++) {
      lines.push(`${P}_fleet{pod="p${m}"} ${(10 + m * 0.3 + Math.sin(i / 12) + (m === 2 && i > 90 ? 8 : 0)).toFixed(3)} ${t}`);
    }
  }
  let arrivals = 0, latSum = 0, latCount = 0;
  for (let i = 0; i < LN; i++) {
    const t = LSTART + i * LSTEP;
    const peak = i >= 160 && i < 190;
    const rate = 20 + 8 * Math.sin(i / 25) + (peak ? 25 : 0);
    const w = 0.2 + (peak ? 0.35 : 0);
    const reqs = (rate * LSTEP) / 1000;
    arrivals += reqs; latSum += reqs * w; latCount += reqs;
    lines.push(`${P}_arrivals_total ${arrivals.toFixed(3)} ${t}`);
    lines.push(`${P}_lat_seconds_sum ${latSum.toFixed(3)} ${t}`);
    lines.push(`${P}_lat_seconds_count ${latCount.toFixed(3)} ${t}`);
    lines.push(`${P}_concurrency ${(rate * w).toFixed(3)} ${t}`);
  }
  for (let t = DEND - 8 * 86_400_000, i = 0; t <= DEND; t += DSTEP, i++) {
    const day = ((t - DEND) % 86_400_000 + 86_400_000) % 86_400_000 / 86_400_000;
    lines.push(`${P}_daily ${(100 + 40 * Math.sin(2 * Math.PI * day) + (t > DEND - 3 * 3_600_000 ? 60 : 0)).toFixed(3)} ${t}`);
  }
  await importSeries(request, lines.join("\n") + "\n");
  execSync(
    `uv run python -c "
from telemetry_nerd.devtools.synthetic import push, spc_demo_text
push('${FIXTURE}', spc_demo_text('${P}_spc', ${END}))"`,
    { cwd: "..", encoding: "utf8" },
  );
  await mcpTool(request, "source_learn", { source: "default" });
});

test.afterAll(async ({ request }) => {
  await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]={__name__=~"${P}.*"}`);
});

/** A CompactText: one ellipsised line holding the full text (also in its title); a click opens it in place. */
async function expectCompact(c: Locator, contains: string): Promise<void> {
  await expect(c).toBeVisible();
  await expect(c).toContainText(contains);
  await expect(c).toHaveAttribute("aria-expanded", "false");
  const s = await c.evaluate((e) => {
    const cs = getComputedStyle(e);
    return { ws: cs.whiteSpace, to: cs.textOverflow, ov: cs.overflowX, title: e.getAttribute("title"), text: e.textContent?.trim() };
  });
  expect(s).toMatchObject({ ws: "nowrap", to: "ellipsis", ov: "hidden" });
  expect(s.title).toBe(s.text);
  await c.click();
  await expect(c).toHaveAttribute("aria-expanded", "true");
  expect(await c.evaluate((e) => getComputedStyle(e).whiteSpace)).toBe("normal");
  await c.press("Enter");
  await expect(c).toHaveAttribute("aria-expanded", "false");
}

async function open(page: Page, id: string, hash = "/"): Promise<Locator> {
  await page.goto(hash);
  const el = page.locator(`[data-panel-id="${id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();
  return el;
}

const toSimple = (page: Page) => page.locator("[data-mode-toggle=simple]").click();

async function query(request: APIRequestContext, expr: string, start: number, end: number, step: string) {
  const q = await request.post("/api/query", { data: { expr, start: String(start), end: String(end), step } });
  expect(q.ok(), await q.text()).toBeTruthy();
  return (await q.json()).dataset;
}

async function distribution(request: APIRequestContext) {
  const q = await request.post("/api/query-distribution", {
    data: { selector: "tn_demo_request_duration_seconds_bucket", by: ["instance"], start: "now-3h", end: "now-10m" },
  });
  expect(q.ok()).toBeTruthy();
  return (await q.json()).dataset;
}

test("simple heatmap: view and colour switches and the colour key stay, colormap and outline pickers go", async ({ page, request }) => {
  const dataset = await distribution(request);
  const s = await request.post("/api/show", { data: { dataset, question: "simple kinds: heatmap", unit: "s" } });
  const { panel } = await s.json();
  const el = await open(page, panel.id);
  await expect(el.getByRole("button", { name: "viridis" })).toBeVisible();
  await expect(el.locator(".heat-controls").nth(1)).toContainText("outline bucket holding");
  await expect(el.locator("[data-compact-text]")).toHaveCount(0);

  await toSimple(page);
  await expect(el.getByRole("button", { name: "viridis" })).toHaveCount(0);
  await expect(el.getByRole("button", { name: "cividis" })).toHaveCount(0);
  await expect(el.locator(".heat-controls").nth(1)).not.toContainText("outline");
  await expect(el.getByRole("button", { name: "heatmap", exact: true })).toBeVisible();
  await expect(el.getByRole("button", { name: "share of column" })).toBeVisible();
  await expect(el.locator(".color-key")).toContainText("hatched: no data");
  await expectCompact(el.locator("[data-simple-what] [data-compact-text]"), "classic le buckets");

  await el.getByRole("button", { name: "percentile bands" }).click();
  await expect(el.getByRole("button", { name: "p99", exact: true })).toBeVisible();
  await expectCompact(el.locator("[data-heat-hint] [data-compact-text]"), "faded lane");

  // an outline set in the nerd peek keeps its basis on the way back to simple
  await el.getByRole("button", { name: "heatmap", exact: true }).click();
  await el.getByRole("button", { name: "Panel actions" }).click();
  await el.locator("[data-peek=nerd]").click();
  await el.getByRole("button", { name: "p95", exact: true }).click();
  await el.locator("[data-peek=simple]").click();
  await expect(el.locator("[data-heat-outline]")).toHaveText("· outline: bucket holding p95 (only where n ≥ 200)");
});

test("simple histogram: views and bar modes stay, the drag hint goes, |ΔF| is one line", async ({ page, request }) => {
  const dataset = await distribution(request);
  const now = Date.now();
  const windows = [
    { start: String(now - 3 * 3600_000), end: String(now - 2 * 3600_000), label: "earlier" },
    { start: String(now - 3600_000), end: String(now - 600_000), label: "recent" },
  ];
  const shown = await mcpTool(request, "show", { dataset, question: "simple kinds: histogram", unit: "s", mark: "histogram", windows });
  const el = await open(page, shown.panel);
  const facet = el.locator(".facet").first();
  await expect(facet.locator(".hint")).toContainText("drag on the plot");
  await expect(facet.locator(".legend")).toContainText("max |ΔF| at bucket edges");

  await toSimple(page);
  await expect(facet.locator(".hint")).toHaveCount(0);
  for (const name of ["histogram", "ECDF", "quantile", "CCDF", "share"]) {
    await expect(facet.getByRole("button", { name, exact: true })).toBeVisible();
  }
  await expect(facet.locator(".legend")).toContainText("n =");
  await expectCompact(facet.locator("[data-compact-text]").first(), "max |ΔF| at bucket edges");
});

test("simple spc: the supplementary run rules checkbox goes, the legend is one line", async ({ page, request }) => {
  const dataset = await query(request, `${P}_spc`, END - 239 * STEP, END, "1m");
  const shown = await mcpTool(request, "show", { dataset, question: "simple kinds: spc", mark: "spc" });
  const el = await open(page, shown.panel);
  const spc = el.locator(".spc").first();
  await expect(spc.locator("[data-spc-supplementary]")).toBeVisible();
  await expect(spc.locator(".legend")).toContainText("99% intervals");

  await toSimple(page);
  await expect(spc.locator("[data-spc-supplementary]")).toHaveCount(0);
  await expect(spc.locator(".verdict")).toBeVisible();
  await expectCompact(spc.locator(".legend [data-compact-text]"), "99% intervals");
});

test("simple fleet: view switch, encoding and key stay, the summary is one line", async ({ page, request }) => {
  const dataset = await query(request, `sum by (pod) (${P}_fleet)`, START, END, "1m");
  const shown = await mcpTool(request, "show", { dataset, question: "simple kinds: fleet", mark: "fleet" });
  const el = await open(page, shown.panel);
  await expect(el.locator("[data-fleet-legend]")).toContainText("members");
  await expect(el.locator("[data-fleet-legend] [data-compact-text]")).toHaveCount(0);

  await toSimple(page);
  await expect(el.locator("[data-fleet-view-btn=band]")).toBeVisible();
  await expect(el.locator("[data-fleet-encoding]")).toBeVisible();
  await expect(el.locator("[data-fleet-key]")).toBeVisible();
  await expectCompact(el.locator("[data-fleet-legend] [data-compact-text]"), "members");
});

test("simple littles: verdict/raw switch stays, the legend is one line", async ({ page, request }) => {
  const check = await mcpTool(request, "check_littles_law", {
    arrival_rate: `${P}_arrivals_total`, latency: `${P}_lat_seconds`, concurrency: `${P}_concurrency`,
    start: String(LSTART), end: String(LEND), window: "2m", detail: true,
  });
  const shown = await mcpTool(request, "show", { dataset: check.datasets.concurrency, question: "simple kinds: littles", mark: "littles" });
  const el = await open(page, shown.panel);
  const lt = el.locator(".littles").first();
  await expect(lt.locator(".legend")).toContainText("Whole range");
  await expect(lt.locator("[data-compact-text]")).toHaveCount(0);

  await toSimple(page);
  await expect(lt.locator("[data-littles-view-raw]")).toBeVisible();
  await expect(lt.locator(".key")).toBeVisible();
  await expectCompact(lt.locator(".legend [data-compact-text]"), "Whole range");
});

test("simple seasonal: cycles/ratio switch and reasons stay, the legend is one line", async ({ page, request }) => {
  const dataset = await query(request, `sum(${P}_daily)`, DEND - 12 * 3_600_000, DEND, "5m");
  const c = await request.post("/api/compare-seasonal", { data: { dataset, cycles: ["1d"] } });
  expect(c.ok(), await c.text()).toBeTruthy();
  const s = await request.post("/api/show", { data: { dataset, question: "simple kinds: seasonal", mark: "seasonal" } });
  const { panel } = await s.json();
  const el = await open(page, panel.id);
  const sz = el.locator(".seasonal").first();
  await expect(sz.locator(".legend")).toContainText("median");
  await expect(sz.locator("[data-compact-text]")).toHaveCount(0);

  await toSimple(page);
  await expect(sz.locator(".views button").first()).toBeVisible();
  await expectCompact(sz.locator(".legend [data-compact-text]"), "median");
});

test("simple binding group: id, kind, key and metrics stay, the binding line and role details are one line", async ({ page, request, uniq }) => {
  const G = `tn_e2e_skg${uniq}`;
  const STEP30 = 30_000;
  const n = 360;
  const end = Math.floor((Date.now() - 5 * 60_000) / STEP30) * STEP30;
  const lines = [`# TYPE ${G}_http_requests_total counter`];
  let ok = 0, bad = 0;
  for (let i = 0; i < n; i++) {
    const t = end - (n - 1 - i) * STEP30;
    ok += 600; bad += 3;
    for (const svc of ["checkout", "cart"]) {
      lines.push(`${G}_http_requests_total{service="${svc}",code="200"} ${ok} ${t}`);
      lines.push(`${G}_http_requests_total{service="${svc}",code="500"} ${bad} ${t}`);
    }
  }
  await importSeries(request, lines.join("\n") + "\n");
  await mcpTool(request, "source_learn", { source: "default" });
  await mcpTool(request, "catalog_write", {
    source: "default", claims: [{ metric: `${G}_http_requests_total`, field: "type", value: "counter", confidence: 0.9, basis: "e2e" }],
  });
  await mcpTool(request, "catalog_bind", {
    source: "default", kind: "RED", key: `${G}:checkout`, confidence: 0.8, basis: "e2e", join_on: ["service"],
    roles: { rate: `${G}_http_requests_total`, errors: `${G}_http_requests_total`, duration: null },
  });
  const red = await mcpTool(request, "show_binding", { kind: "RED", key: `${G}:checkout`, range: "3h", error_matcher: 'code=~"5.."' });

  await page.goto(`/#/group/${red.group}`);
  const g = page.locator(`[data-group-id="${red.group}"]`);
  const rate = g.locator('[data-role="rate"]');
  await expect(g.locator(".group-meta")).toContainText("one time axis");
  await expect(g.locator(".group-meta [data-compact-text]")).toHaveCount(0);
  await expect(rate.locator(".role-label .form")).toBeVisible();

  await toSimple(page);
  await expect(g.locator(".obj-id").first()).toHaveText(red.group);
  await expect(g.locator("[data-group-kind-badge]")).toHaveText("RED");
  await expect(g.locator("[data-group-key]")).toHaveText(`${G}:checkout`);
  await expect(rate.locator(".role-label code")).toHaveText(`${G}_http_requests_total`);
  await expectCompact(g.locator(".group-meta [data-compact-text]"), "one time axis");
  await expectCompact(rate.locator(".role-label [data-compact-text]"), "2 members");
  await expectCompact(g.locator('[data-role="errors"] .role-notes [data-compact-text]').first(), "label names are conventions");
  // members are ordinary panels and follow simple mode
  await expect(rate.locator("[data-simple-toolbar]")).toBeVisible();
});
