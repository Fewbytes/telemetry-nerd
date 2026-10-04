import { expect, test } from "@playwright/test";
import { eventsSince, lastSeq, seedPanel } from "./helpers.js";

test("y-view: pick, badge, persist across reload, ambient event, band drag", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does demo latency drift within its normal band?");
  const since = await lastSeq(request);
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Y-axis view" });
  await row.getByRole("button", { name: "data range" }).click();
  await expect(el.locator("[data-y-badge]")).toContainText("y zoomed");
  await expect(el.locator('[data-note="y_view"]')).toContainText("y zoomed to data");
  await page.reload();
  await expect(row.getByRole("button", { name: "data range" })).toHaveClass(/on/);
  const events = await eventsSince(request, since);
  const sel = events.filter((e: { type: string }) => e.type === "panel.y_view_selected").at(-1);
  expect(sel).toMatchObject({ klass: "ambient", object_id: panel.id, payload: { mode: "data" } });

  await row.getByRole("button", { name: "band…" }).click();
  const box = (await el.locator(".plot").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + 40);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y + 120, { steps: 5 });
  await page.mouse.up();
  await expect(el.locator("[data-y-badge]")).toContainText("y band");
  await row.getByRole("button", { name: "auto" }).click();
  await expect(el.locator("[data-y-badge]")).toHaveCount(0);
});
