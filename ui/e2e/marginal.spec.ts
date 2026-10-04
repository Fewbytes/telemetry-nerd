import { expect, test } from "@playwright/test";
import { eventsSince, lastSeq, seedPanel } from "./helpers.js";

test("marginal now vs previous, n shown, ambient event; indexed view labels 1", async ({ page, request }) => {
  const panel = await seedPanel(request, "Did demo latency's level shift against the previous window?");
  const since = await lastSeq(request);
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Marginal histogram" });
  await row.getByRole("button", { name: "vs previous time range" }).click();
  await expect(el.locator("[data-marginal]")).toHaveAttribute("data-marginal-basis", "samples");
  await expect(el.locator("[data-marginal-n]")).toContainText("now n=");
  await expect(el.locator('[data-note="marginal"]')).toContainText("not requests");
  const events = await eventsSince(request, since);
  expect(events.filter((e: { type: string }) => e.type === "panel.marginal_set").at(-1))
    .toMatchObject({ klass: "ambient", object_id: panel.id, payload: { reference: "previous" } });
  await page.reload();
  await expect(el.locator("[data-marginal]")).toBeVisible();

  const y = el.getByRole("group", { name: "Y-axis view" });
  await y.getByRole("button", { name: "÷ own mean" }).click();
  await expect(el.locator("[data-y-badge]")).toContainText("indexed · 1 = each series' mean over");
  await expect(el.locator("[data-marginal]")).toHaveCount(0); // marginal is off in indexed view
  await expect(row.getByRole("button", { name: "vs previous time range" })).toBeDisabled();
  await y.getByRole("button", { name: "auto" }).click();
  await expect(el.locator("[data-marginal]")).toBeVisible();
});
