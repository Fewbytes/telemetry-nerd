import { expect, test } from "./fixtures.js";
import { execSync } from "node:child_process";
import { DAEMON, mcpTool, seedPanel } from "./helpers";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

test("simple mode: sidebar keeps chat and findings, folds hypotheses and gaps, hides code runs", async ({ page }) => {
  await page.goto("/");
  const side = page.locator("aside.sidebar");
  // nerd (fixture pin): every section present
  await expect(side.getByRole("heading", { name: "Hypotheses" })).toBeVisible();
  await expect(side.getByRole("heading", { name: "Gaps" })).toBeVisible();

  await page.locator("[data-mode-toggle=simple]").click();
  await expect(side.getByRole("heading", { name: "Chat" })).toBeVisible();
  await expect(side.getByRole("heading", { name: "Findings" })).toBeVisible();
  // hypotheses/gaps: header with a count, body hidden until expanded
  const hyp = side.getByRole("button", { name: /Hypotheses/ });
  await expect(hyp).toHaveAttribute("aria-expanded", "false");
  await expect(side.getByText("No hypotheses yet.")).toHaveCount(0);
  await hyp.click();
  await expect(hyp).toHaveAttribute("aria-expanded", "true");
  await expect(side.getByText("No hypotheses yet.")).toBeVisible();
  await expect(side.getByRole("heading", { name: "Code runs" })).toHaveCount(0);

  // switching back restores the persisted nerd layout
  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(side.getByRole("heading", { name: "Gaps" })).toBeVisible();
});

test("simple mode: gaps fold with their count and expand on click", async ({ page, request, uniq }) => {
  await mcpTool(request, "gap_create", { missing_signal: `${uniq} queue depth`, needed_for: "saturation" });
  await page.goto("/");
  const side = page.locator("aside.sidebar");
  await page.locator("[data-mode-toggle=simple]").click();
  const gaps = side.getByRole("button", { name: /^Expand Gaps \(\d+\)$/ });
  await expect(gaps).toHaveAttribute("aria-expanded", "false");
  await expect(side.getByRole("heading", { name: /^Gaps \(\d+\)$/ })).toBeVisible();
  await expect(side.getByText(`${uniq} queue depth`)).toHaveCount(0);
  await gaps.click();
  await expect(side.getByText(`${uniq} queue depth`).first()).toBeVisible();
});

test("highlight strip: author badge only in nerd mode", async ({ page, request }) => {
  const panel = await seedPanel(request, "badge target");
  await page.goto("/");
  mcp("highlight", { object: panel.id, note: "look here", seconds: 60 });
  const item = page.locator(`.highlight-item[data-id="${panel.id}"]`);
  await expect(item).toBeVisible();
  await expect(item.locator(".badge.author")).toBeVisible(); // fixture pin: nerd
  await page.locator("[data-mode-toggle=simple]").click();
  await expect(item).toBeVisible();
  await expect(item.locator(".badge.author")).toHaveCount(0);
});
