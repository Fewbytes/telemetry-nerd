import { execSync } from "node:child_process";
import { expect, importDemo, test } from "./fixtures.js";
import { DAEMON, FIXTURE, seedPanel } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// The catalog is global (per source, not per workspace): each test claims on its own metrics, so
// a repeat or a later spec never meets an earlier test's edits (zek0.2).
test("catalog view: browse, filter, edit in place, persists and reaches Claude", async ({ page, request, uniq }) => {
  const P = `tn_e2e_cat${uniq}`;
  const M = `${P}_requests_total`;
  await importDemo(request, P);
  mcp("source_learn", { source: "default" });
  // Claude disagrees with the name rule about the unit: a conflict the view must surface
  mcp("catalog_write", {
    source: "default",
    claims: [{ metric: M, field: "unit", value: "requests", confidence: 0.8, basis: "e2e: a more specific unit than the count the name rule gives" }],
  });

  await page.goto("/#/catalog");
  const view = page.locator("[data-catalog-view]");
  await expect(view).toBeVisible();
  await expect(view.locator("[data-catalog-summary]")).toContainText("metrics");
  await expect(page.getByRole("link", { name: "Catalog" })).toHaveAttribute("aria-current", "page");

  await view.getByLabel("Search").fill(M);
  const row = view.locator(`[data-catalog-row="${M}"]`);
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
  await view.getByLabel("Search").fill(M);
  await row.getByRole("button", { name: M }).click();
  const unit = view.locator('[data-field="unit"]');
  await expect(unit).toBeVisible();
  await unit.locator('[data-edit="unit"]').click();
  await unit.getByLabel("Edit unit").fill("calls");
  await unit.getByRole("button", { name: "Save" }).click();
  await expect(row.locator('[data-cell="unit"] .chip')).toHaveText("you");
  await expect(row.locator('[data-cell="unit"]')).toContainText("calls");

  // it persists, and Claude hears about it
  await page.reload();
  await view.getByLabel("Search").fill(M);
  await expect(row.locator('[data-cell="unit"] .chip')).toHaveText("you");
  const claim = await (await request.post("/api/channel/claim", { data: { consumer: `e2e-catalog-${uniq}` } })).json();
  expect(claim.content).toContain(`user set unit of ${M} on default to "calls"`);

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

test("catalog view: names that encode a dimension collapse into a family; confirm and split", async ({ page, uniq }) => {
  const F = `tnfam${uniq}`; // a family of this test's own: split dissolves it for good
  // 40 metrics whose names differ only in an identifier: one family, not 40 rows
  execSync(
    `uv run python -c "
from telemetry_nerd.devtools.synthetic import exposition, push
from telemetry_nerd.model.time import now_ms
t = now_ms() // 15000 * 15000
push('${FIXTURE}', ''.join(exposition(f'${F}_job_w{i}_x{i}_done', {}, [(t - 60000, 1.0), (t, 2.0)]) for i in range(40)))"`,
    { cwd: "..", encoding: "utf8" },
  );
  mcp("source_learn", { source: "default" });

  await page.goto("/#/catalog");
  const view = page.locator("[data-catalog-view]");
  await view.getByLabel("Search").fill(F);
  const fam = view.locator(`[data-catalog-row="${F}_job_*_done"]`);
  await expect(fam).toBeVisible();
  await expect(fam).toContainText("family · 40 metrics");
  await expect(view.locator(`[data-catalog-row="${F}_job_w3_x3_done"]`)).toHaveCount(0); // hidden behind the family
  await expect(view.locator("[data-catalog-families]")).toContainText("families covering");

  // members are one click away, each with the dimension its name encodes
  await fam.locator("[data-family-members]").click();
  await expect(view.locator("[data-family-crumb]")).toContainText(`${F}_job_*_done`);
  await expect(view.locator("[data-catalog-total]")).toHaveAttribute("data-catalog-total", "40");
  await expect(view.locator(`[data-catalog-row="${F}_job_w3_x3_done"] [data-dimension]`)).toContainText("w3_x3");
  await view.getByRole("button", { name: "back to all metrics" }).click();

  // confirm pins it; it persists
  await fam.locator("[data-family-confirm]").click();
  await expect(fam).toContainText("confirmed");
  await page.reload();
  await view.getByLabel("Search").fill(F);
  await expect(view.locator(`[data-catalog-row="${F}_job_*_done"]`)).toContainText("confirmed");

  // split dissolves it for good: the members are ordinary metrics again
  await view.locator(`[data-catalog-row="${F}_job_*_done"] [data-family-split]`).click();
  await expect(view.locator(`[data-catalog-row="${F}_job_*_done"]`)).toHaveCount(0);
  await expect(view.locator(`[data-catalog-row="${F}_job_w3_x3_done"]`)).toBeVisible();
  mcp("source_learn", { source: "default" }); // re-learning does not bring the family back
  await page.reload();
  await view.getByLabel("Search").fill(F);
  await expect(view.locator(`[data-catalog-row="${F}_job_*_done"]`)).toHaveCount(0);
  await expect(view.locator(`[data-catalog-row="${F}_job_w3_x3_done"]`)).toBeVisible();
});
