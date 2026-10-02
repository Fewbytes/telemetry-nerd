import { execSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { DAEMON } from "./helpers";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// SPC panel (lkn.1/lkn.6): limit uncertainty strips, hover text per flagged point, toggle for
// supplementary run rules. The demo request rate drifts against its first-half baseline, so
// the judged half has flagged points.
test("spc panel: hover names a flagged point's rules; supplementary rules toggle", async ({ page, request }) => {
  const q = await request.post("/api/query", {
    data: { expr: "sum(rate(tn_demo_requests_total[5m]))", start: "now-5h", end: "now-10m", step: "1m" },
  });
  const { dataset } = await q.json();
  const out = JSON.parse(mcp("show", { dataset, question: "Is the request rate in control?", mark: "spc" }));
  const id = out.panel;
  await page.goto("/");
  const panel = page.locator(`[data-panel-id="${id}"]`);
  const spc = panel.locator(".spc").first();
  await expect(spc).toHaveAttribute("data-spc-mode", /individuals|ar1_residuals/);
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

  const toggle = spc.locator("[data-spc-supplementary]");
  if (await toggle.count()) {
    const before = Number(await spc.getAttribute("data-spc-violations"));
    await toggle.uncheck();
    await expect(spc.locator(".legend")).toContainText("supplementary run rules hidden");
    expect(Number(await spc.getAttribute("data-spc-violations"))).toBeLessThan(before);
  }
});
