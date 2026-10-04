import type { Locator } from "@playwright/test";
import { expect, test } from "./fixtures.js";
import { mkdirSync } from "node:fs";
import { FIXTURE, mcpTool } from "./helpers.js";

// show_binding (bead czt.3): a RED and a USE binding drawn as linked panel groups. Synthetic data
// under unique metric names (per test attempt: the catalog bindings are global), imported into the
// fixture source.
const SHOTS = "e2e-shots";
const STEP = 30_000;

function exposition(P: string): string {
  const end = Math.floor((Date.now() - 5 * 60_000) / STEP) * STEP;
  const n = (3 * 3600_000) / STEP;
  const start = end - (n - 1) * STEP;
  const les = ["0.05", "0.1", "0.25", "0.5", "1", "+Inf"];
  const share = [0.2, 0.5, 0.8, 0.95, 0.99, 1]; // cumulative share of requests under each le
  // declared types: the fixture source keeps # TYPE metadata (as VictoriaMetrics does), which source_learn reads
  const lines: string[] = [
    ["http_requests_total", "counter"], ["http_request_duration_seconds", "histogram"],
    ["http_requests_in_flight", "gauge"], ["disk_utilization_ratio", "gauge"],
    ["disk_queue_depth", "gauge"], ["disk_queue_limit", "gauge"],
  ].flatMap(([m, t]) => [`# HELP ${P}_${m} e2e fixture`, `# TYPE ${P}_${m} ${t}`]);
  const total: Record<string, Record<string, number>> = {};
  for (let i = 0; i < n; i++) {
    const t = start + i * STEP;
    const burst = i > n / 2 && i < n / 2 + 40; // checkout fails for 20 minutes
    for (const [svc, rps] of [["checkout", 20], ["cart", 8]] as const) {
      const ok = rps * (1 + 0.2 * Math.sin(i / 30));
      const bad = svc === "checkout" ? (burst ? 4 : 0.1) : 0.05;
      const c = (total[svc] ??= { ok: 0, bad: 0, n: 0, sum: 0 });
      c.ok += ok * 30; c.bad += bad * 30; c.n += (ok + bad) * 30; c.sum += (ok + bad) * 30 * (burst ? 0.3 : 0.12);
      lines.push(`${P}_http_requests_total{service="${svc}",code="200"} ${c.ok.toFixed(1)} ${t}`);
      lines.push(`${P}_http_requests_total{service="${svc}",code="500"} ${c.bad.toFixed(1)} ${t}`);
      les.forEach((le, k) => {
        const s = burst && k < 4 ? share[k] * 0.6 : share[k];
        lines.push(`${P}_http_request_duration_seconds_bucket{service="${svc}",le="${le}"} ${(c.n * s).toFixed(1)} ${t}`);
      });
      lines.push(`${P}_http_request_duration_seconds_count{service="${svc}"} ${c.n.toFixed(1)} ${t}`);
      lines.push(`${P}_http_request_duration_seconds_sum{service="${svc}"} ${c.sum.toFixed(2)} ${t}`);
      lines.push(`${P}_http_requests_in_flight{service="${svc}"} ${(rps * 0.15 * (burst ? 3 : 1)).toFixed(2)} ${t}`);
    }
    for (const host of ["db-1", "db-2"]) {
      const u = 0.45 + 0.25 * Math.sin(i / 50 + (host === "db-2" ? 1 : 0)) + (burst ? 0.2 : 0);
      lines.push(`${P}_disk_utilization_ratio{instance="${host}"} ${Math.min(u, 0.99).toFixed(3)} ${t}`);
      lines.push(`${P}_disk_queue_depth{instance="${host}"} ${(2 + 6 * Math.max(0, u - 0.5)).toFixed(2)} ${t}`);
      lines.push(`${P}_disk_queue_limit{instance="${host}"} 8 ${t}`);
    }
  }
  return lines.join("\n") + "\n";
}

const xhairLeft = async (role: Locator): Promise<number> => (await role.locator("[data-xhair]").first().boundingBox())!.x;

test("show_binding: RED and USE groups, linked crosshair and brush, gap card, both themes", async ({ page, request, uniq }) => {
  const P = `tn_e2e_czt3${uniq}`;
  test.setTimeout(120_000);
  mkdirSync(SHOTS, { recursive: true });
  const put = await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: exposition(P) });
  expect(put.ok()).toBeTruthy();
  await request.get(`${FIXTURE}/internal/force_flush`);
  await mcpTool(request, "source_learn", { source: "default" });
  const types: [string, string][] = [
    ["http_requests_total", "counter"], ["http_requests_in_flight", "gauge"],
    ["disk_utilization_ratio", "gauge"], ["disk_queue_depth", "gauge"], ["disk_queue_limit", "gauge"],
  ];
  await mcpTool(request, "catalog_write", {
    source: "default",
    claims: types.map(([m, t]) => ({ metric: `${P}_${m}`, field: "type", value: t, confidence: 0.9, basis: "e2e fixture" })),
  });
  await mcpTool(request, "catalog_relate", {
    source: "default",
    claims: [{ subject: `${P}_disk_queue_depth`, kind: "bounded_by", object: `${P}_disk_queue_limit`, confidence: 0.8, basis: "e2e fixture" }],
  });
  // a classic histogram's base name is no series, so the catalog holds its members: bind _bucket
  await mcpTool(request, "catalog_bind", {
    source: "default", kind: "RED", key: `${P}:checkout`, confidence: 0.8, basis: "e2e fixture", join_on: ["service"],
    roles: { rate: `${P}_http_requests_total`, errors: `${P}_http_requests_total`, duration: `${P}_http_request_duration_seconds_bucket` },
  });
  await mcpTool(request, "catalog_bind", {
    source: "default", kind: "USE", key: `${P}:disk`, confidence: 0.8, basis: "e2e fixture", join_on: ["instance"],
    roles: { utilization: `${P}_disk_utilization_ratio`, saturation: `${P}_disk_queue_depth`, errors: null },
  });
  const red = await mcpTool(request, "show_binding", {
    kind: "RED", key: `${P}:checkout`, range: "3h", error_matcher: 'code=~"5.."',
  });
  expect(Object.values(red.roles).every((r: any) => r.panel), JSON.stringify(red)).toBeTruthy();
  expect(red.roles.errors.form).toBe("error_ratio");
  const use = await mcpTool(request, "show_binding", { kind: "USE", key: `${P}:disk`, range: "3h" });
  expect(use.gaps).toEqual(["errors"]);

  await page.setViewportSize({ width: 1200, height: 1600 });
  await page.goto(`/#/group/${red.group}`);
  const g = page.locator(`[data-group-id="${red.group}"]`);
  await expect(g.locator("[data-group-kind-badge]")).toHaveText("RED");
  await expect(g.locator("[data-group-key]")).toHaveText(`${P}:checkout`);
  await expect(g.locator(".roles-list")).toHaveText("rate · errors · duration");
  for (const role of ["rate", "errors", "duration"]) {
    await expect(g.locator(`[data-role="${role}"] [data-panel-id="${red.roles[role].panel}"]`)).toBeVisible();
  }
  const rate = g.locator('[data-role="rate"]');
  const errors = g.locator('[data-role="errors"]');
  const duration = g.locator('[data-role="duration"]');
  await expect(errors.locator("[data-interval-legend]")).toContainText("Wilson");
  await expect(errors.locator(".what")).toContainText("Share of requests that failed");
  await expect(duration.locator(".panel")).toHaveAttribute("data-heatmap-cells", /\d+/);
  await expect(rate.locator(".u-over")).toBeVisible();
  await expect(errors.locator(".u-over")).toBeVisible();

  // linked crosshair: the pointer on one panel draws the same instant on every role, same x
  await rate.locator(".u-over").scrollIntoViewIfNeeded();
  const over = (await rate.locator(".u-over").boundingBox())!;
  await page.mouse.move(over.x + over.width * 0.6, over.y + over.height / 2);
  for (const r of [rate, errors, duration]) await expect(r.locator("[data-xhair]")).toHaveCount(1);
  const xs = [await xhairLeft(rate), await xhairLeft(errors), await xhairLeft(duration)];

  expect(Math.max(...xs) - Math.min(...xs), JSON.stringify(xs)).toBeLessThan(2); // aligned x axes
  // linked selection: a brush on the rate panel shows on the others and offers the group over it
  await page.mouse.move(over.x + over.width * 0.4, over.y + over.height / 2);
  await page.mouse.down();
  await page.mouse.move(over.x + over.width * 0.7, over.y + over.height / 2, { steps: 6 });
  await page.mouse.up();
  await expect(errors.locator("[data-linked-brush]")).toHaveCount(1);
  await expect(duration.locator("[data-linked-brush]")).toHaveCount(1);
  await expect(g.locator("[data-group-zoom]")).toBeVisible();
  await page.keyboard.press("Escape");

  const u = page.locator(`[data-group-id="${use.group}"]`);
  await expect(u.locator("[data-group-kind-badge]")).toHaveText("USE");
  const gap = u.locator('[data-gap-card="errors"]');
  await expect(gap).toContainText("No errors signal");
  await expect(gap).toContainText("_errors_total");
  await expect(gap.locator("a.obj-id")).toHaveText(/^g\d+$/);
  await expect(u.locator('[data-role="utilization"] .u-over')).toBeVisible();
  await expect(u.locator('[data-role="saturation"] .panel')).toHaveAttribute("data-overlays", /lines/); // limit line

  for (const mode of ["light", "dark"] as const) {
    await page.getByLabel("Theme").selectOption(mode);
    await expect(page.locator("html")).toHaveAttribute("data-theme", mode);
    await rate.locator(".u-over").hover({ position: { x: over.width * 0.5, y: over.height / 2 } });
    await expect(duration.locator("[data-xhair]")).toHaveCount(1);
    // legible: header text has contrast against the page background in both themes
    const [fg, bg] = await g.locator("[data-group-key]").evaluate((el) => [getComputedStyle(el).color, getComputedStyle(document.body).backgroundColor]);
    expect(fg).not.toBe(bg);
    await g.screenshot({ path: `${SHOTS}/binding-red-${mode}.png` });
    await u.screenshot({ path: `${SHOTS}/binding-use-${mode}.png` });
  }

  // closing the group closes every panel in it
  await g.getByRole("button", { name: "Close group" }).click();
  await expect(g).toHaveCount(0);
  for (const role of ["rate", "errors", "duration"]) {
    await expect(page.locator(`[data-panel-id="${red.roles[role].panel}"]`)).toHaveCount(0);
  }
});
