import { execSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { DAEMON, FIXTURE, seedPanel } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// samples for the same series one week back, so last week's window is not empty
const seedLastWeek = () =>
  execSync(
    `uv run python -c "
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
end = now_ms() // 15000 * 15000 - 7 * 86400000
text = exposition('tn_demo_latency_seconds', {'instance': 'a'}, [(end - 4 * 3600000 + i * 15000, 0.1) for i in range(960)])
push('${FIXTURE}', text)"`,
    { cwd: "..", encoding: "utf8" },
  );

test("reference layers: limit line and normal band on by default, last week on demand, persisted", async ({ page, request }) => {
  mcp("source_learn", { source: "default" });
  mcp("catalog_relate", {
    source: "default",
    claims: [{
      subject: "tn_demo_latency_seconds", kind: "bounded_by", object: "tn_demo_requests_total",
      confidence: 0.8, basis: "e2e: stand-in for a physical limit",
    }],
  });
  seedLastWeek();
  const panel = await seedPanel(request, "Does latency stay inside its normal band?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const layers = el.getByRole("group", { name: "Reference layers" });

  // the limit line is on and drawn from the first paint; the band joins once the profile exists
  await expect(layers.locator('[data-overlay="limit"]')).toHaveAttribute("aria-pressed", "true");
  await expect(el).toHaveAttribute("data-overlays", /lines/);
  // provenance (bead 2as.15): the limit's origin and confidence are on the chip, not a magic number
  await expect(layers.locator('[data-overlay="limit"]')).toHaveAttribute("title", /origin: claude \(confidence 0\.80\)/);
  // a resolved bound comes with reframings (proposals, never applied silently)
  const reframes = el.getByRole("group", { name: "Reframings" });
  await expect(reframes.getByRole("button", { name: /headroom/ })).toBeVisible();
  await expect(reframes.getByRole("button", { name: /% of/ })).toBeVisible();
  await expect(layers.locator('[data-overlay="normal"]')).toBeEnabled({ timeout: 20_000 });
  await expect(el).toHaveAttribute("data-overlays", /normal/, { timeout: 20_000 });
  await expect(layers.locator('[data-overlay="ghost"]')).toHaveAttribute("aria-pressed", "false");

  // last week is opt-in: switching it on fetches it and draws it
  await layers.locator('[data-overlay="ghost"]').click();
  await expect(el).toHaveAttribute("data-overlays", /ghost/);
  await expect(layers.locator('[data-overlay="ghost"]')).toHaveAttribute("aria-pressed", "true");

  // switching the limit off removes only that layer, and it survives a reload
  await layers.locator('[data-overlay="limit"]').click();
  await expect(el).not.toHaveAttribute("data-overlays", /lines/);
  await expect(el).toHaveAttribute("data-overlays", /normal/);
  await page.reload();
  const again = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(again.locator('[data-overlay="limit"]')).toHaveAttribute("aria-pressed", "false");
  await expect(again.locator('[data-overlay="ghost"]')).toHaveAttribute("aria-pressed", "true");
  await expect(again).not.toHaveAttribute("data-overlays", /lines/);
});

test("a metric with no bounded_by relation has a disabled limit chip that says why", async ({ page, request }) => {
  const q = await request.post("/api/query", { data: { expr: "tn_demo_requests_total", start: "now-3h", end: "now-10m", step: "1m" } });
  const { dataset } = await q.json();
  const shown = await request.post("/api/show", { data: { dataset, question: "Is there a limit here?" } });
  const panel = (await shown.json()).panel;
  await page.goto("/");
  const chip = page.locator(`[data-panel-id="${panel.id}"]`).locator('[data-overlay="limit"]');
  await expect(chip).toBeDisabled();
  await expect(chip).toHaveAttribute("title", /no bounded_by, threshold_by or same_quantity relation/);
});
