import { execSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import { DAEMON, seedPanel } from "./helpers";

const mcp = (tool: string, args: object) =>
  execSync(`uv run python scripts/mcp_call.py --url ${DAEMON}/mcp ${tool} '${JSON.stringify(args)}'`, {
    cwd: "..",
    encoding: "utf8",
  });

test("Claude highlight shows in the strip and on the panel; × clears it", async ({ page, request }) => {
  const panel = await seedPanel(request, "highlight target");
  await page.goto("/");
  const target = page.locator(`#panel-${panel.id}`);
  await expect(target).toBeVisible();

  mcp("highlight", { object: panel.id, note: "check this dip", seconds: 60 });
  const item = page.locator(`.highlight-item[data-id="${panel.id}"]`);
  await expect(item).toContainText("check this dip");
  await expect(item).toHaveAttribute("data-highlight-author", "claude");
  await expect(target).toHaveClass(/highlighted/);

  await item.getByRole("button", { name: /Clear highlight/ }).click();
  await expect(item).toHaveCount(0);
  await expect(target).not.toHaveClass(/highlighted/);
});

test("Claude can clear its own highlight", async ({ page, request }) => {
  const panel = await seedPanel(request, "claude clears");
  await page.goto("/");
  mcp("highlight", { object: panel.id, seconds: 0 });
  await expect(page.locator(`.highlight-item[data-id="${panel.id}"]`)).toBeVisible();
  mcp("unhighlight", { object: panel.id });
  await expect(page.locator(`.highlight-item[data-id="${panel.id}"]`)).toHaveCount(0);
});

test("user pin with a note reaches Claude through the channel", async ({ page, request }) => {
  const panel = await seedPanel(request, "pin target");
  await page.goto("/");
  const target = page.locator(`#panel-${panel.id}`);
  await target.getByRole("button", { name: `Highlight ${panel.id}` }).click();
  await target.getByLabel(`Highlight note for ${panel.id}`).fill("why is this spiky?");
  await target.getByRole("button", { name: "Pin" }).click();

  await expect(target).toHaveClass(/highlighted/);
  await expect(target).toHaveAttribute("data-highlight-author", "user");

  const claim = await (await request.post("/api/channel/claim", { data: { consumer: "e2e-pin" } })).json();
  expect(claim.content).toContain(`user highlighted ${panel.id}: "why is this spiky?"`);

  // pinning an already pinned object clears it
  await target.getByRole("button", { name: `Clear highlight on ${panel.id}` }).click();
  await expect(target).not.toHaveClass(/highlighted/);
});
