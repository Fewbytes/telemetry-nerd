import { expect, test } from "./fixtures.js";
import { FIXTURE, mcpTool } from "./helpers.js";

// Simple mode keeps the band legend (a band's meaning changes the reading). The RED errors panel
// (error ratio with Wilson intervals) is the fixture panel that draws an interval band; setup as
// in binding-group.spec.ts, RED role only.
const STEP = 30_000;

function exposition(P: string): string {
  const end = Math.floor((Date.now() - 5 * 60_000) / STEP) * STEP;
  const n = (3 * 3600_000) / STEP;
  const start = end - (n - 1) * STEP;
  const les = ["0.05", "0.1", "0.25", "0.5", "1", "+Inf"];
  const share = [0.2, 0.5, 0.8, 0.95, 0.99, 1];
  const lines: string[] = [
    ["http_requests_total", "counter"], ["http_request_duration_seconds", "histogram"],
  ].flatMap(([m, t]) => [`# HELP ${P}_${m} e2e fixture`, `# TYPE ${P}_${m} ${t}`]);
  const total: Record<string, { ok: number; bad: number; n: number; sum: number }> = {};
  for (let i = 0; i < n; i++) {
    const t = start + i * STEP;
    const burst = i > n / 2 && i < n / 2 + 40;
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
    }
  }
  return lines.join("\n") + "\n";
}

test("simple mode keeps the band legend of an interval band", async ({ page, request, uniq }) => {
  const P = `tn_e2e_sbl${uniq}`;
  test.setTimeout(60_000);
  const put = await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: exposition(P) });
  expect(put.ok()).toBeTruthy();
  await request.get(`${FIXTURE}/internal/force_flush`);
  await mcpTool(request, "source_learn", { source: "default" });
  await mcpTool(request, "catalog_write", {
    source: "default",
    claims: [{ metric: `${P}_http_requests_total`, field: "type", value: "counter", confidence: 0.9, basis: "e2e fixture" }],
  });
  await mcpTool(request, "catalog_bind", {
    source: "default", kind: "RED", key: `${P}:checkout`, confidence: 0.8, basis: "e2e fixture", join_on: ["service"],
    roles: { rate: `${P}_http_requests_total`, errors: `${P}_http_requests_total`, duration: `${P}_http_request_duration_seconds_bucket` },
  });
  const red = await mcpTool(request, "show_binding", {
    kind: "RED", key: `${P}:checkout`, range: "3h", error_matcher: 'code=~"5.."',
  });

  await page.goto(`/#/group/${red.group}`);
  const errors = page.locator(`[data-group-id="${red.group}"] [data-role="errors"]`);
  await expect(errors.locator("[data-interval-legend]")).toContainText("Wilson"); // nerd (fixture pin)
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(errors.locator("[data-simple-what]")).toContainText("Share of requests that failed");
  await expect(errors.locator("[data-interval-legend]")).toBeVisible();
  await expect(errors.locator("[data-interval-legend]")).toContainText("Wilson");
});
