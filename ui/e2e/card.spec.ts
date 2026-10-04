import { execSync } from "node:child_process";
import { expect, importDemo, showExpr, test } from "./fixtures.js";
import { DAEMON } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

// The card writes user claims to the (global, not per-workspace) catalog: each test edits its own
// metric, so a repeat starts from the name rule again and no other spec sees the edit (zek0.2).
test("metric card: renders the catalog, confirm and edit write user claims that reach Claude", async ({ page, request, uniq }) => {
  const P = `tn_e2e_card${uniq}`;
  await importDemo(request, P);
  mcp("source_learn", { source: "default" });
  const panel = await showExpr(request, `${P}_latency_seconds`, "What does the catalog say about demo latency?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const card = el.locator("[data-metric-card]");
  await expect(card).not.toHaveAttribute("open", "");
  await card.locator("summary").click();

  const metric = card.locator(`[data-card-metric="${P}_latency_seconds"]`);
  await expect(metric).toBeVisible();
  const unit = metric.locator('[data-field="unit"]');
  await expect(unit.locator("[data-value]")).toHaveText("s");
  await expect(unit.locator("[data-origin]")).toContainText("name rule");
  await expect(card.locator("[data-card-summary]")).toContainText("unit s");

  // honest about what is not measured; the operating profile section is always there
  await expect(card.locator("[data-card-quality]")).toContainText("counter resets");
  await expect(card.locator("[data-card-quality]")).toContainText("not measured");
  await expect(card.locator("[data-card-profile]")).toBeVisible();

  // confirm pins the shown value as the user's
  await unit.locator('[data-confirm="unit"]').click();
  await expect(unit.locator("[data-origin]")).toHaveText("you");
  await expect(unit.locator('[data-confirm="unit"]')).toHaveCount(0);

  // edit changes the value, and the chart's axis follows with its provenance
  await unit.locator('[data-edit="unit"]').click();
  await unit.getByLabel("Edit unit").fill("ms");
  await unit.getByRole("button", { name: "Save" }).click();
  await expect(unit.locator("[data-value]")).toHaveText("ms");
  await expect(unit.locator("[data-origin]")).toHaveText("you");
  await expect
    .poll(async () => {
      const panels = await (await request.get("/api/panels")).json();
      const y = panels.find((p: { id: string }) => p.id === panel.id).spec.y;
      return `${y.unit}|${y.unit_provenance}`;
    })
    .toBe("ms|set by user");

  // the correction reaches Claude as an intentional event
  const claim = await (await request.post("/api/channel/claim", { data: { consumer: `e2e-card-${uniq}` } })).json();
  expect(claim.content).toContain(`user set unit of ${P}_latency_seconds on default to "ms"`);

  // an unclaimed field can be set in place
  const role = metric.locator('[data-field="role"]');
  await role.locator('[data-edit="role"]').click();
  await role.getByLabel("Edit role").fill("latency");
  await role.getByRole("button", { name: "Save" }).click();
  await expect(role.locator("[data-value]")).toHaveText("latency");
});

test("metric card: an empty edit cannot be saved", async ({ page, request, uniq }) => {
  const P = `tn_e2e_card${uniq}`;
  await importDemo(request, P);
  mcp("source_learn", { source: "default" });
  const panel = await showExpr(request, `${P}_latency_seconds`, "Is an empty edit refused?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await el.locator("[data-metric-card] summary").click();
  const description = el.locator(`[data-card-metric="${P}_latency_seconds"] [data-field="description"]`);
  await description.locator('[data-edit="description"]').click();
  await expect(description.getByRole("button", { name: "Save" })).toBeDisabled(); // empty text cannot be saved
});
