import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers.js";

test("preset range change previews, then keep-this-range creates a new panel", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does demo latency drift within its normal band?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Time range" });
  const panelCountBefore = await page.locator("[data-panel-id]").count();

  // the seeded panel covers now-3h..now-10m; a 6h preset needs older data than that, so this
  // exercises tier 2 (server-fetched preview), not the in-bounds client-side slice.
  await row.getByRole("button", { name: "6h" }).click();
  const badge = el.locator("[data-preview-badge]");
  await expect(badge).toBeVisible();
  await expect(badge).toContainText("previewing");
  await expect(badge).toContainText("not this panel's evidence");

  // bead geje: picking a range now visibly renders it — a separate, clearly-marked preview
  // chart, distinct from the panel's own (untouched) evidence above it.
  const preview = el.locator("[data-preview-render]");
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute("data-preview-render", "fetched");
  await expect(preview.locator(".preview-tag")).toContainText("preview");
  await expect(preview.locator(".u-over")).toBeVisible(); // uPlot actually drew something

  await row.getByRole("button", { name: "keep this range" }).click();
  await expect(page.locator("[data-panel-id]")).toHaveCount(panelCountBefore + 1);
  await expect(preview).toBeHidden(); // keeping the range clears the preview, no residue
});

test("a preset within what's already fetched previews locally, no server round trip", async ({
  page, request,
}) => {
  const panel = await seedPanel(request, "Does demo latency drift within its normal band?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Time range" });

  // the seeded panel covers now-3h..now-10m: a 15m preset anchored to that fetched end (not
  // wall-clock "now") lands fully inside it, so this is tier 1 — a client-side window, not a
  // fetch. Regression for isInBounds/nowAnchor being unreachable against Date.now().
  await row.getByRole("button", { name: "15m" }).click();
  const badge = el.locator("[data-preview-badge]");
  await expect(badge).toContainText("from data already loaded");

  const preview = el.locator("[data-preview-render]");
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute("data-preview-render", "local");
});
