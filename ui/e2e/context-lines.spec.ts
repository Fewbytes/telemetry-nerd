import { execSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { DAEMON } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// a host whose memory the node_exporter pack knows: total, free (alarming), available (honest)
const seedHost = () =>
  execSync(
    `uv run python -c "
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
end = now_ms() // 15000 * 15000
ts = [end - 3 * 3600000 + i * 15000 for i in range(720)]
parts = []
for name, v in [('node_memory_MemTotal_bytes', 16e9), ('node_memory_MemFree_bytes', 2e9), ('node_memory_MemAvailable_bytes', 9e9)]:
    parts.append(exposition(name, {'instance': 'ctx-host'}, [(t, v + (i % 7) * 1e7) for i, t in enumerate(ts)]))
push('http://127.0.0.1:8428', ''.join(parts))"`,
    { cwd: "..", encoding: "utf8" },
  );

test("context lines: the memory limit carries its provenance; a reframing opens a new panel", async ({ page, request }) => {
  seedHost();
  mcp("source_learn", { source: "default" });
  const q = await request.post("/api/query", { data: { expr: 'node_memory_MemFree_bytes{instance="ctx-host"}', start: "now-3h", end: "now-10m", step: "1m" } });
  const { dataset } = await q.json();
  const shown = await request.post("/api/show", { data: { dataset, question: "Is this host short of memory?", raw: true } });
  const id = (await shown.json()).panel.id;
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${id}"]`);

  // the limit line is the MemTotal bound, and says who put it there
  const chip = el.getByRole("group", { name: "Reference layers" }).locator('[data-overlay="limit"]');
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  await expect(chip).toHaveAttribute("title", /node_memory_MemTotal_bytes \(limit\): origin: pack \(confidence 0\.85\); pack node_exporter@/);
  await expect(el).toHaveAttribute("data-overlays", /lines/);
  await expect(el).toContainText("physical limit node_memory_MemTotal_bytes");

  // reframings are offered, with the reason; the original is untouched until one is accepted
  const reframes = el.getByRole("group", { name: "Reframings" });
  const substitute = reframes.locator('[data-reframe="0"]');
  await expect(substitute).toHaveText("show available memory instead");
  await expect(substitute).toHaveAttribute("title", /page cache/);
  await expect(reframes.locator('[data-reframe="1"]')).toContainText("% of");
  const before = await page.locator("[data-panel-id]").count();
  await substitute.click();
  await expect(page.locator("[data-panel-id]")).toHaveCount(before + 1);
  const fresh = page.locator(`[data-panel-id]:not([data-panel-id="${id}"])`).first(); // newest first
  await expect(fresh).toContainText("Reframed, not the metric as asked");
  await expect(fresh).toContainText(`reframed from ${id}`);
  await expect(el).not.toContainText("Reframed, not the metric as asked");
});
