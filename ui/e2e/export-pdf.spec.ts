import { readFile } from "node:fs/promises";
import { expect, test } from "./fixtures.js";
import { mcpTool, seedPanel } from "./helpers.js";

// Workspace-to-PDF export (bead w7ht): one document holding every open panel's chart plus the
// workspace's question, hypotheses and findings, for sharing/archiving outside the app.
test("exports the whole workspace to a multi-page PDF", async ({ page, request }) => {
  const panelA = await seedPanel(request, "Is p99 latency elevated?");
  const panelB = await seedPanel(request, "Did error rate change?");

  const { hypothesis } = await mcpTool(request, "hypothesis_create", { statement: "Latency rose after the deploy" });
  await mcpTool(request, "finding_create", {
    claim: "p99 latency is elevated in the last 3 hours",
    scope: { source: "default", selector: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m", aggregation: "avg" },
    evidence: [{ kind: "panel", panel: panelA.id }],
    hypothesis,
    stance: "for",
  });

  await page.goto("/");
  await expect(page.locator(`[data-panel-id="${panelA.id}"]`)).toBeVisible();
  await expect(page.locator(`[data-panel-id="${panelB.id}"]`)).toBeVisible();
  // let both uPlot charts finish their first draw before capturing them
  await expect(page.locator(`[data-panel-id="${panelA.id}"] canvas`).first()).toBeVisible();
  await expect(page.locator(`[data-panel-id="${panelB.id}"] canvas`).first()).toBeVisible();

  const button = page.getByRole("button", { name: "Export workspace to PDF" });
  await expect(button).toBeEnabled();
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    button.click(),
  ]);

  expect(download.suggestedFilename()).toMatch(/\.pdf$/);
  const path = await download.path();
  expect(path).toBeTruthy();
  const bytes = await readFile(path!);
  expect(bytes.subarray(0, 5).toString("latin1")).toBe("%PDF-");
  // a summary page + 2 panel pages, each with a captured chart, adds up to a real document
  expect(bytes.length).toBeGreaterThan(5_000);

  const text = bytes.toString("latin1");
  const pageCount = (text.match(/\/Type\s*\/Page[^s]/g) ?? []).length;
  expect(pageCount).toBeGreaterThanOrEqual(3);
});
