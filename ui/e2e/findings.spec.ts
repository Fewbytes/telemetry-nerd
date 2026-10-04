import { expect, test } from "@playwright/test";
import { mcpTool, seedPanel } from "./helpers.js";

const SHOTS = "test-results/m6-ux";

// Findings / hypotheses UX (bead d77.4): flagged evidence is visible per item, scope is a
// labelled block, links reach the panel, and a hypothesis status change flows through the
// status buttons by keyboard.
test("finding with flagged evidence; hypothesis status change by keyboard", async ({ page, request }) => {
  const panel = await seedPanel(request, "Is p99 latency elevated?");
  const ws = await (await request.get("/api/workspace")).json();
  const dataset: string = ws.panels.find((p: { id: string }) => p.id === panel.id).dataset_ids[0];

  const { hypothesis } = await mcpTool(request, "hypothesis_create", { statement: "Latency rose after the deploy" });
  const { finding } = await mcpTool(request, "finding_create", {
    claim: "p99 latency is elevated in the last 3 hours",
    scope: { source: "default", selector: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m", aggregation: "avg" },
    evidence: [
      { kind: "statistic", dataset, name: "peak_ratio", value: 0.42, interval: [0.4, 0.45], method: "bootstrap", source: "special_cause" },
      { kind: "statistic", dataset, name: "mean", value: 0.2, uncertainty_unknown: true, method: "mean" },
      { kind: "panel", panel: panel.id },
    ],
    hypothesis,
    stance: "for",
  });

  await page.goto("/");
  const card = page.locator(`#finding-${finding}`);
  await expect(card).toBeVisible();

  // scope: labelled block
  const scope = card.getByRole("term").filter({ hasText: "Time range" });
  await expect(scope).toBeVisible();
  await expect(card.locator(".scope")).toContainText("tn_demo_latency_seconds");

  // evidence: interval + source chip on the first; unknown uncertainty + flag on the second
  const items = card.locator("ul.evidence > li");
  await expect(items.nth(0)).toContainText("peak_ratio");
  await expect(items.nth(0)).toContainText("[0.4, 0.45]");
  await expect(items.nth(0).locator(".chip.source")).toHaveText("special cause");
  await expect(items.nth(1).locator(".stat-unc")).toHaveText("uncertainty unknown");
  await expect(items.nth(1).locator(".uncertainty-flag").first()).toBeVisible();
  await expect(card.locator(".verdict-state")).toContainText("1 of 3 evidence flagged");

  // link to the panel flashes it
  await items.nth(1).getByRole("button", { name: `Show panel ${panel.id}` }).click();
  await expect(page.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();

  // hypothesis: finding grouped under "For"
  const hyp = page.locator(`#hypothesis-${hypothesis}`);
  await expect(hyp.getByRole("list", { name: `For ${hypothesis}` })).toContainText(finding);
  await expect(hyp.locator(".status-chip")).toContainText("proposed");

  // status change by keyboard: focus "supported" and press Enter
  const supported = hyp.getByRole("button", { name: `Mark ${hypothesis} supported` });
  await supported.focus();
  await page.keyboard.press("Enter");
  await expect(hyp.locator(".status-chip")).toContainText("supported");
  await expect(supported).toHaveAttribute("aria-pressed", "true");

  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Theme").selectOption(theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await page.locator(".sidebar").screenshot({ path: `${SHOTS}/findings-${theme}.png` });
  }
});
