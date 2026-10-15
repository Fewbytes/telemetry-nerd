import { expect, test } from "./fixtures.js";
import { mcpTool, seedPanel } from "./helpers";

test("simple mode: one toolbar above the plot replaces the control rows", async ({ page }) => {
  const panel = await seedPanel(page.request, "simple: where are the controls?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();

  const bar = el.locator("[data-simple-toolbar]");
  await expect(bar).toBeVisible();
  // toolbar precedes the plot
  const barY = (await bar.boundingBox())!.y;
  const plotY = (await el.locator(".plot").boundingBox())!.y;
  expect(barY).toBeLessThan(plotY);
  // nerd control rows are gone
  await expect(el.locator(".legend.time-range, .legend.y-views, .legend.reframes, .legend.overlays")).toHaveCount(0);

  // y-view dropdown applies a view
  const y = bar.getByLabel("Y-axis view");
  await expect(y.locator("option:checked")).toHaveText("auto");
  await y.selectOption({ label: "from zero" });
  await expect(el.locator("[data-y-origin]")).toHaveCount(0); // from-zero view has no y≠0 badge
  await expect(y.locator("option:checked")).toHaveText("from zero");
  await expect(el.locator('[data-note="y_view"]')).toContainText("from zero");

  // reference layers sit behind one disclosure
  await bar.getByLabel("Reference layers").click();
  const normal = bar.getByRole("checkbox", { name: "normal band" });
  await expect(normal).toBeChecked();
  await normal.uncheck();
  await expect(normal).not.toBeChecked();
  await expect(el).not.toHaveAttribute("data-overlays", /normal/);

  // a range preset reuses the nerd handler: the preview badge, "keep this range" and the
  // preview chart are kept in simple mode (a preview is not this panel's evidence)
  await bar.getByRole("button", { name: "6h", exact: true }).click();
  await expect(el.locator("[data-preview-badge]")).toContainText("not this panel's evidence");
  await expect(el.getByRole("button", { name: "keep this range" })).toBeVisible();
  await expect(el.locator("[data-preview-render] .u-over")).toBeVisible();

  // the preview chart follows the mode toggle (previewEl is bound by whichever controls render)
  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(bar).toHaveCount(0);
  await expect(el.locator("[data-preview-render] .u-over")).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(el.locator("[data-preview-render] .u-over")).toBeVisible();
  await expect(el.locator("[data-preview-render]")).toHaveCount(1);
});

test("simple mode: Claude's y-view suggestions are in the same dropdown", async ({ page, request }) => {
  const panel = await seedPanel(request, "simple: does Claude's y view show?");
  await mcpTool(request, "suggest_y_view", {
    panel: panel.id, mode: "data", label: "fit the wiggles", reason: "the normal range flattens the series",
  });
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();
  await page.locator("[data-mode-toggle=simple]").click();

  const y = el.locator("[data-simple-toolbar]").getByLabel("Y-axis view");
  const suggested = y.locator("optgroup[label='Claude suggests'] option[data-y-suggestion]");
  await expect(suggested).toHaveText("fit the wiggles");
  await y.selectOption({ label: "fit the wiggles" });
  await expect(y.locator("option:checked")).toHaveText("fit the wiggles");
  await expect(el.locator('[data-note="y_view"]')).toContainText("fit the wiggles");
});
