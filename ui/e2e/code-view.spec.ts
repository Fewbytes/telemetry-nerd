import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures.js";
import { mcpTool, seedPanel } from "./helpers.js";

// Tier-2 code nodes in the UI (bead b98.5): panel provenance -> read-only code view, the code-run
// list in the sidebar, failed runs with their traceback, and re-run as a new node.
// Run: cd ui && E2E_PORT=<free port> npx playwright test e2e/code-view.spec.ts

const SHOT_DIR = process.env.E2E_SHOTS ?? "test-results/code-view";

const BAND = (d: string) => `
import polars as pl
import telemetry_nerd.tn as tn

# smooth the latency and attach a declared 95% band
df = tn.dataset("${d}").select("ts_ms", "series_id", "avg")
df = df.with_columns(lo=pl.col("avg") * 0.9, hi=pl.col("avg") * 1.1)
name = tn.put(df, like="${d}", description="demo band", uncertainty={"method": "bootstrap", "level": 0.95})
print("stored", name)
`;

/** WCAG contrast of every syntax colour in the open code view against its block background. */
async function syntaxContrast(page: Page): Promise<number> {
  return page.evaluate(() => {
    const lum = (rgb: string) => {
      const [r, g, b] = (rgb.match(/[\d.]+/g) ?? ["0", "0", "0"]).slice(0, 3).map((v) => {
        const c = Number(v) / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    const pre = document.querySelector("dialog.code-view pre.src")!;
    const bg = lum(getComputedStyle(pre).backgroundColor);
    let worst = 21;
    for (const el of pre.querySelectorAll("span")) {
      const fg = lum(getComputedStyle(el).color);
      worst = Math.min(worst, (Math.max(fg, bg) + 0.05) / (Math.min(fg, bg) + 0.05));
    }
    return worst;
  });
}

test("panel provenance opens the code view; failed run shows its traceback; re-run is a new node", async ({ page, request }) => {
  const base = await seedPanel(request, "Latency to feed the code run?");
  const input = (await (await request.get("/api/panels")).json()).find((p: { id: string }) => p.id === base.id).dataset_ids[0];

  const run = await mcpTool(request, "run_code", { code: BAND(input), inputs: [input] });
  expect(run.status).toBe("ok");
  const out = run.outputs[0].dataset;
  const shown = await request.post("/api/show", { data: { dataset: out, question: "Latency with its declared band?" } });
  expect(shown.ok()).toBeTruthy();
  const panelId = (await shown.json()).panel.id;

  const bad = await mcpTool(request, "run_code", { code: "def f():\n    return {}['missing']\nf()" });
  expect(bad.status).toBe("failed");

  await page.goto("/");
  const panel = page.locator(`[data-panel-id="${panelId}"]`);
  await expect(panel.locator("canvas").first()).toBeVisible();

  // provenance: "produced by code node cN (output ...) from dM" with the id as a button
  const link = panel.locator("[data-provenance] [data-code-link]");
  await expect(link).toHaveText(run.code_node);
  await link.click();
  const dialog = page.locator("dialog.code-view");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("[data-code-status]")).toContainText("ok");
  await expect(dialog.locator("pre.src")).toContainText('tn.put(df, like=');
  await expect(dialog.locator("pre.src .tok-kw").first()).toBeVisible();
  await expect(dialog.locator("pre.src .tok-com").first()).toContainText("# smooth the latency");
  await expect(dialog.locator("[data-code-stdout]")).toContainText("stored");
  await expect(dialog.getByText(out, { exact: true }).first()).toBeVisible();
  await expect(dialog.getByRole("link", { name: `shown in ${panelId}` })).toBeVisible();
  await expect(dialog.getByRole("link", { name: `shown in ${base.id}` })).toBeVisible(); // the input

  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    expect(await syntaxContrast(page)).toBeGreaterThanOrEqual(4.5);
    await dialog.screenshot({ path: `${SHOT_DIR}/code-ok-${theme}.png` });
  }

  // re-run: a new node, shown with its status; the original stays
  await dialog.locator("[data-code-rerun]").click();
  const result = dialog.locator("[data-code-rerun-result]");
  await expect(result).toContainText("ok");
  const newId = (await result.locator("button").textContent())!.trim();
  expect(newId).not.toBe(run.code_node);
  await result.locator("button").click();
  await expect(dialog.getByText("re-run of", { exact: false })).toContainText(run.code_node);
  await dialog.getByRole("button", { name: "Close code view" }).click();
  await expect(dialog).toBeHidden();

  // the activity list: every run, newest first; the failed one opens with its traceback
  const list = page.locator(".code-runs");
  await expect(list.locator(`[data-code-run="${run.code_node}"]`)).toBeVisible();
  await expect(list.locator(`[data-code-run="${newId}"]`)).toBeVisible();
  const failed = list.locator(`[data-code-run="${bad.code_node}"]`);
  await expect(failed).toContainText("failed");
  await failed.getByRole("button").click();
  await expect(dialog.locator("[data-code-status]")).toContainText("failed");
  await expect(dialog.locator("[data-code-error]")).toContainText("KeyError");
  await expect(dialog.locator("[data-code-traceback]")).toContainText("KeyError");
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    expect(await syntaxContrast(page)).toBeGreaterThanOrEqual(4.5);
    await dialog.screenshot({ path: `${SHOT_DIR}/code-failed-${theme}.png` });
  }
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});
