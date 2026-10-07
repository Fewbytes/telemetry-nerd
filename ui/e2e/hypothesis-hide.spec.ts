import { expect, test } from "./fixtures.js";
import { mcpTool, seedPanel } from "./helpers.js";

// Hypothesis hide/dismiss (bead telemetry-nerd-fygk): put a hypothesis aside without a verdict,
// reversible, never deleted. A hidden hypothesis a finding still cites must stay visible (hiding
// must never break a citation) — only the uncited rest actually declutters the board.
test("hide: an uncited hypothesis disappears until 'show hidden' is checked, with its reason shown", async ({ page, request }) => {
  const { hypothesis } = await mcpTool(request, "hypothesis_create", { statement: "A decoy hypothesis nobody cites" });
  await page.goto("/");

  const card = page.locator(`#hypothesis-${hypothesis}`);
  await expect(card).toBeVisible();

  const hideReason = card.getByPlaceholder("Reason for hiding (optional)");
  await hideReason.fill("duplicate of h1");
  await card.getByRole("button", { name: `Hide ${hypothesis}` }).click();

  await expect(card).toBeHidden();
  const toggleLabel = page.locator("label.show-hidden");
  await expect(toggleLabel).toContainText("Show hidden");
  await expect(toggleLabel).toContainText("(1)");
  const toggle = toggleLabel.locator("input");

  await toggle.check();
  await expect(card).toBeVisible();
  await expect(card.locator(".hidden-chip")).toHaveText("hidden");
  await expect(card.locator(".hidden-reason")).toHaveText("duplicate of h1");

  // unhide brings it back under the normal (unchecked) view
  await card.getByRole("button", { name: `Unhide ${hypothesis}` }).click();
  await expect(card.locator(".hidden-chip")).toHaveCount(0);
  await toggle.uncheck();
  await expect(card).toBeVisible();
});

test("hide: a hypothesis a finding still cites stays visible, flagged hidden, even with 'show hidden' off", async ({ page, request }) => {
  const panel = await seedPanel(request, "Is p99 latency elevated?");
  const { hypothesis } = await mcpTool(request, "hypothesis_create", { statement: "Latency rose after the deploy" });
  await mcpTool(request, "finding_create", {
    claim: "p99 latency is elevated in the last 3 hours",
    scope: { source: "default", selector: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m", aggregation: "avg" },
    evidence: [{ kind: "panel", panel: panel.id }],
    hypothesis,
    stance: "for",
  });
  await page.goto("/");

  const card = page.locator(`#hypothesis-${hypothesis}`);
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: `Hide ${hypothesis}` }).click();

  const toggle = page.locator("label.show-hidden input");
  await expect(toggle).not.toBeChecked();
  await expect(card).toBeVisible(); // cited: stays despite "show hidden" being off
  await expect(card.locator(".hidden-chip")).toHaveText("hidden");
});
