import { execSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { DAEMON, seedPanel } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

test("catalog view: browse, filter, edit in place, persists and reaches Claude", async ({ page, request }) => {
  mcp("source_learn", { source: "default" });
  // Claude disagrees with the name rule about the unit: a conflict the view must surface
  mcp("catalog_write", {
    source: "default",
    claims: [{ metric: "tn_demo_requests_total", field: "unit", value: "requests", confidence: 0.8, basis: "e2e: a more specific unit than the count the name rule gives" }],
  });

  await page.goto("/#/catalog");
  const view = page.locator("[data-catalog-view]");
  await expect(view).toBeVisible();
  await expect(view.locator("[data-catalog-summary]")).toContainText("metrics");
  await expect(page.getByRole("link", { name: "Catalog" })).toHaveAttribute("aria-current", "page");

  await view.getByLabel("Search").fill("tn_demo_requests");
  const row = view.locator('[data-catalog-row="tn_demo_requests_total"]');
  await expect(row).toBeVisible();
  await expect(row.locator('[data-cell="unit"]')).toContainText("requests");
  await expect(row.locator('[data-cell="unit"] .chip')).toHaveText("Claude");
  await expect(row).toHaveClass(/conflict/);
  await expect(row).toContainText("conflict: unit");

  // the conflicts filter narrows the whole catalog to disputed metrics
  await view.getByLabel("Search").fill("");
  const total = async () => Number(await view.locator("[data-catalog-total]").getAttribute("data-catalog-total"));
  await expect.poll(total).toBeGreaterThan(1);
  const all = await total();
  await view.getByRole("checkbox", { name: "conflicts" }).check();
  await expect.poll(total).toBeLessThan(all);
  await expect(row).toBeVisible();
  await view.getByRole("button", { name: /clear 1 filter/ }).click();
  await expect.poll(total).toBe(all);

  // expand the row and edit the unit in place
  await view.getByLabel("Search").fill("tn_demo_requests");
  await row.getByRole("button", { name: "tn_demo_requests_total" }).click();
  const unit = view.locator('[data-field="unit"]');
  await expect(unit).toBeVisible();
  await unit.locator('[data-edit="unit"]').click();
  await unit.getByLabel("Edit unit").fill("calls");
  await unit.getByRole("button", { name: "Save" }).click();
  await expect(row.locator('[data-cell="unit"] .chip')).toHaveText("you");
  await expect(row.locator('[data-cell="unit"]')).toContainText("calls");

  // it persists, and Claude hears about it
  await page.reload();
  await view.getByLabel("Search").fill("tn_demo_requests");
  await expect(row.locator('[data-cell="unit"] .chip')).toHaveText("you");
  const claim = await (await request.post("/api/channel/claim", { data: { consumer: "e2e-catalog" } })).json();
  expect(claim.content).toContain('user set unit of tn_demo_requests_total on default to "calls"');

  // and back to the panels
  await page.getByRole("link", { name: "Panels" }).click();
  await expect(view).toHaveCount(0);
  await expect(page.locator(".layout")).toBeVisible();
});

test("catalog view: the panels route still works next to it", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does the panels view still work?");
  await page.goto("/#/catalog");
  await expect(page.locator("[data-catalog-view]")).toBeVisible();
  await page.goto(`/#/panel/${panel.id}`);
  await expect(page.locator(`[data-panel-id="${panel.id}"]`)).toBeVisible();
});
