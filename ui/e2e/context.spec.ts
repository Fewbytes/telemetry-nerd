import { execSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, importDemo, showExpr, test } from "./fixtures.js";
import { DAEMON } from "./helpers.js";

// the tool arguments go through a file: file contents are full of quotes and newlines
const mcp = (tool: string, args: object) => {
  const file = join(mkdtempSync(join(tmpdir(), "tn-ctx-")), "args.json");
  writeFileSync(file, JSON.stringify(args));
  return execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} @${file}`, { cwd: "..", encoding: "utf8" });
};

// The registration names this test's own counter: context claims land in the global catalog,
// so a repeat on the shared demo counter would change nothing (zek0.2).
const code = (counter: string) => `from prometheus_client import Counter

REQS = Counter("${counter}", "Requests handled by the e2e demo service.", ["code"])
OTHER = Counter("tn_not_in_this_source", "A metric this source does not have.")
`;

test("repo context: code registrations become cited catalog claims, shown on the card and in the catalog", async ({ page, request, uniq }) => {
  const P = `tn_e2e_ctx${uniq}`;
  await importDemo(request, P);
  mcp("source_learn", { source: "default" });
  const out = JSON.parse(mcp("catalog_context", { source: "default", files: [{ path: "demo/metrics.py", text: code(`${P}_requests`) }] }));
  expect(out.definitions).toBe(2);
  expect(out.unmatched.map((u: { name: string }) => u.name)).toEqual(["tn_not_in_this_source"]);
  expect(out.claims_changed).toBeGreaterThan(0);

  const p2 = await showExpr(request, `${P}_requests_total`, "Counter context?", { raw: true });
  await page.goto("/");
  const card2 = page.locator(`[data-panel-id="${p2.id}"] [data-metric-card]`);
  await card2.locator("summary").click();
  const desc = card2.locator('[data-field="description"]');
  await expect(desc).toContainText("Requests handled by the e2e demo service.");
  await expect(desc.locator("[data-origin]")).toContainText("repo/docs");
  await expect(desc.locator("[data-origin]")).toHaveAttribute("title", /code: demo\/metrics\.py:3 \(python prometheus_client Counter\)/);

  // the catalog view can filter by the new origin
  await page.goto("/#/catalog");
  const view = page.locator("[data-catalog-view]");
  await view.getByLabel("Winning origin").selectOption("context");
  await view.getByLabel("Search").fill(`${P}_requests`);
  await expect(view.locator(`[data-catalog-row="${P}_requests_total"]`)).toBeVisible();
});
