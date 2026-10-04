import { execSync } from "node:child_process";
import { expect, test } from "./fixtures.js";
import { DAEMON, FIXTURE } from "./helpers";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// The spec's own series (bead ax1s): devtools.synthetic.spc_demo_text, a stationary gauge with a
// +15 sd spike and a +2.5 sd shift in its judged half, on a whole-minute grid, queried over
// exactly that grid. Same buckets and same verdicts every run; the unit tests
// (test_synthetic.py) pin the spike outside the limits in both chart modes. The demo request
// rate it used before is stationary noise: whether a point was flagged depended on the second
// the fixture started and on how far "now" had moved.
const METRIC = `tn_e2e_spc_${Date.now()}`;
const STEP = 60_000;
const END = Math.floor((Date.now() - 6 * 60_000) / STEP) * STEP;
const START = END - 239 * STEP; // SPC_DEMO_STEPS samples

test.beforeAll(() => {
  execSync(
    `uv run python -c "
from telemetry_nerd.devtools.synthetic import push, spc_demo_text
push('${FIXTURE}', spc_demo_text('${METRIC}', ${END}))"`,
    { cwd: "..", encoding: "utf8" },
  );
});

test.afterAll(async ({ request }) => {
  await request.post(`${FIXTURE}/api/v1/admin/tsdb/delete_series?match[]=${METRIC}`);
});

// SPC panel (lkn.1/lkn.6): limit uncertainty strips, hover text per flagged point, toggle for
// supplementary run rules.
test("spc panel: hover names a flagged point's rules; supplementary rules toggle", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: METRIC, start: String(START), end: String(END), step: "1m" },
  });
  expect(q.ok(), await q.text()).toBeTruthy();
  const { dataset } = await q.json();
  const out = JSON.parse(mcp("show", { dataset, question: "Is this gauge in control?", mark: "spc" }));
  const id = out.panel;
  await page.goto("/");
  const panel = page.locator(`[data-panel-id="${id}"]`);
  const spc = panel.locator(".spc").first();
  await expect(spc).toHaveAttribute("data-spc-mode", "individuals");
  await expect(spc).not.toHaveAttribute("data-spc-violations", "0");
  await expect(spc.locator(".legend")).toContainText("99% intervals");
  await expect(spc.locator(".legend")).toContainText("hover a point");

  // find a violation ring by its colour on the canvas, then hover it
  const canvas = spc.locator("canvas").first();
  const hit = await canvas.evaluate((c: HTMLCanvasElement) => {
    const ctx = c.getContext("2d")!;
    const { data, width, height } = ctx.getImageData(0, 0, c.width, c.height);
    const k = c.width / c.getBoundingClientRect().width;
    for (let y = 0; y < height; y++)
      for (let x = 0; x < width; x++) {
        const i = 4 * (y * width + x);
        if (Math.abs(data[i] - 213) < 12 && Math.abs(data[i + 1] - 94) < 12 && data[i + 2] < 30) return { x: x / k, y: y / k };
      }
    return null;
  });
  expect(hit).not.toBeNull();
  const box = (await canvas.boundingBox())!;
  await page.mouse.move(box.x + hit!.x + 2, box.y + hit!.y);
  const tip = spc.locator("[data-spc-tip]");
  await expect(tip).toBeVisible();
  await expect(tip).toContainText("UTC ·");
  await expect(tip).toContainText("•");

  // the shift is flagged by run rules alone at some points, so the toggle is always there
  const toggle = spc.locator("[data-spc-supplementary]");
  await expect(toggle).toHaveCount(1);
  const before = Number(await spc.getAttribute("data-spc-violations"));
  await toggle.uncheck();
  await expect(spc.locator(".legend")).toContainText("supplementary run rules hidden");
  expect(Number(await spc.getAttribute("data-spc-violations"))).toBeLessThan(before);
});
