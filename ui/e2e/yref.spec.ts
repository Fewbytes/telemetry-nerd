import { execSync } from "node:child_process";
import { expect, importDemo, showExpr, test } from "./fixtures.js";
import { DAEMON } from "./helpers.js";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

test("reference y range: physical limit from bounded_by, labelled zoom against it", async ({ page, request, uniq }) => {
  // its own metrics: the relation is global catalog state that every demo-latency panel would show
  const P = `tn_e2e_yref${uniq}`;
  await importDemo(request, P);
  // the catalog says: latency never exceeds request duration (a stand-in for avail <= size)
  mcp("source_learn", { source: "default" });
  mcp("catalog_relate", {
    source: "default",
    claims: [{
      subject: `${P}_latency_seconds`, kind: "bounded_by", object: `${P}_requests_total`,
      confidence: 0.8, basis: "e2e: stand-in for a physical limit",
    }],
  });
  const panel = await showExpr(request, `${P}_latency_seconds`, "Is latency far from its physical limit?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  const row = el.getByRole("group", { name: "Y-axis view" });

  // the catalog's contribution is stated in plain words, and the old "no reference range" note is gone
  await expect(el.locator('[data-note="y_reference"]')).toContainText(`physical limit ${P}_requests_total`);
  await expect(el.locator('[data-note="y_scaled_to_data"]')).toHaveCount(0);
  await expect(el.locator("[data-y-badge]")).toHaveCount(0); // the default range needs no warning

  await row.getByRole("button", { name: "reference range" }).click();
  await expect(el.locator("[data-y-badge]")).toContainText("includes");
  await expect(el.locator("[data-y-badge]")).toContainText(`limit ${P}_requests_total`);

  // zooming to the data is labelled and the strip is measured against the reference range
  await row.getByRole("button", { name: "data range" }).click();
  await expect(el.locator("[data-y-badge]")).toContainText("y zoomed");
  await expect(el.locator("[data-y-badge]")).toContainText("reference range");
  await expect(el.locator(".y-strip")).toBeVisible();

  // natural bounds come from the catalog (a _seconds metric is >= 0)
  await expect(row.getByRole("button", { name: "natural bounds" })).toBeEnabled();
  await row.getByRole("button", { name: "natural bounds" }).click();
  await expect(row.getByRole("button", { name: "natural bounds" })).toHaveClass(/on/);
});
