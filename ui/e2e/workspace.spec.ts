import { execSync } from "node:child_process";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const DAEMON = "http://127.0.0.1:7071";

/** query + show a demo panel, as the skeleton spec does. */
async function seedPanel(
  request: APIRequestContext,
  question: string,
): Promise<{ id: string; question: string }> {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_latency_seconds", start: "now-3h", end: "now-10m", step: "1m" },
  });
  expect(q.ok()).toBeTruthy();
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question } });
  expect(s.ok()).toBeTruthy();
  const { panel } = await s.json();
  return panel;
}

/** Brush-select the middle third of a panel's plot (uPlot x-drag). */
async function brushMiddleThird(page: Page, panelId: string): Promise<void> {
  const el = page.locator(`[data-panel-id="${panelId}"]`);
  const box = await el.locator(".plot").boundingBox();
  if (box === null) throw new Error("plot is not visible");
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width / 3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + (2 * box.width) / 3, y, { steps: 5 });
  await page.mouse.up();
}

test("brush-select → Ask Claude → thread visible and claimable exactly once", async ({
  page,
  request,
}) => {
  const panel = await seedPanel(request, "Why does checkout latency dip before the deploy?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  await brushMiddleThird(page, panel.id);
  const menu = page.locator(".selection-menu");
  await expect(menu).toBeVisible();
  await menu.getByRole("button", { name: "Ask Claude…" }).click();
  await menu.getByPlaceholder("Ask Claude about this selection").fill("why the dip here?");
  await menu.getByRole("button", { name: "Send" }).click();
  await expect(menu).toBeHidden();

  // the thread renders under the panel with the user's message
  const thread = el.locator(".thread");
  await expect(thread).toBeVisible();
  await expect(thread.locator(".message .text")).toContainText("why the dip here?");
  await expect(thread.locator(".message .badge.user")).toHaveCount(1);

  // no bridge is attached to this daemon, so the question is still pending for the
  // UserPromptSubmit hook path: one claim returns it, the next finds nothing new.
  const claim = await (
    await request.post("/api/channel/claim", { data: { consumer: "claude" } })
  ).json();
  expect(claim.content).toContain("why the dip here?");
  const again = await (
    await request.post("/api/channel/claim", { data: { consumer: "claude" } })
  ).json();
  expect(again.content).toBeNull();
});

test("Claude finding answers the panel; user verdict rejects it", async ({ page, request }) => {
  const panel = await seedPanel(request, "Is the latency spike visible without peak erosion?");
  const args = JSON.stringify({
    claim: "instance c latency spike survives downsampling",
    scope: {
      source: "default",
      selector: "tn_demo_latency_seconds",
      start: "now-3h",
      end: "now-10m",
      step: "1m",
      aggregation: "avg",
    },
    evidence: [{ kind: "panel", panel: panel.id }],
    answers_panel: panel.id,
  });
  const out = execSync(
    `uv run python scripts/mcp_call.py --url ${DAEMON}/mcp finding_create '${args}'`,
    { cwd: "..", encoding: "utf8" },
  );
  const finding: string = JSON.parse(out).finding;

  await page.goto("/");
  const card = page.locator(`#finding-${finding}`);
  await expect(card).toBeVisible();
  await expect(card.locator(".scope")).toContainText("tn_demo_latency_seconds");
  await expect(page.locator(`[data-panel-id="${panel.id}"] .answered-by`)).toHaveText(
    `answered → ${finding}`,
  );

  await page.getByLabel(`Verdict comment for ${finding}`).fill("wrong window");
  await card.getByRole("button", { name: "Reject", exact: true }).click();

  // rejected findings stay retrievable: collapsed behind the toggle
  await expect(page.locator(".sidebar .toggle-rejected")).toHaveText("show rejected (1)");
  await expect(card).toBeHidden();

  const claim = await (
    await request.post("/api/channel/claim", { data: { consumer: "claude" } })
  ).json();
  expect(claim.content).toContain("rejected");
});

test("Mark region → overlay drawn and annotation listed", async ({ page, request }) => {
  const panel = await seedPanel(request, "What is the flat segment in instance b?");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  await brushMiddleThird(page, panel.id);
  const menu = page.locator(".selection-menu");
  await menu.getByRole("button", { name: "Mark region" }).click();
  await menu.getByPlaceholder("Region label").fill("odd flat segment");
  await menu.getByRole("button", { name: "Save", exact: true }).click();
  await expect(menu).toBeHidden();

  // one region annotation → one draw op on the panel
  await expect(el).toHaveAttribute("data-annotation-count", "1");

  const ws = await (await request.get("/api/workspace")).json();
  const ann = ws.annotations.find((a: { label: string }) => a.label === "odd flat segment");
  expect(ann).toMatchObject({ kind: "region", author: "user", panel: panel.id });
});
