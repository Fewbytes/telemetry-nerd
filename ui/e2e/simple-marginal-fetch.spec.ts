import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers.js";

// Simple mode never shows the marginal, so it must not ask the server to compute it (spec 2026-10-14).
test("simple mode does not request the marginal; nerd mode (toggle or peek) fetches it", async ({ page, request }) => {
  const panel = await seedPanel(request, "Did demo latency's level shift against the previous window?");
  const set = await request.post(`/api/panels/${panel.id}/marginal`, { data: { reference: "previous" } });
  expect(set.ok()).toBeTruthy();

  const dataUrls: string[] = [];
  page.on("request", (r) => {
    const u = r.url();
    if (u.includes(`/api/panels/${panel.id}/data?`)) dataUrls.push(u);
  });
  const el = page.locator(`[data-panel-id="${panel.id}"]`);

  await page.goto("/");
  await page.evaluate(() => localStorage.setItem("tn-mode-default", "simple"));
  dataUrls.length = 0;
  await page.reload();
  await expect(el.locator("[data-simple-toolbar]")).toBeVisible();
  await expect(el.locator("canvas").first()).toBeVisible();
  expect(dataUrls.length).toBeGreaterThan(0);
  for (const u of dataUrls) expect(u).toContain("marginal=0");
  await expect(el.locator("[data-marginal]")).toHaveCount(0);

  // per-panel peek: a fresh fetch with the marginal
  dataUrls.length = 0;
  await el.getByRole("button", { name: "Panel actions" }).click();
  await el.locator("[data-peek=nerd]").click();
  await expect(el.locator("[data-marginal]")).toBeVisible();
  expect(dataUrls.some((u) => !u.includes("marginal=0"))).toBe(true);
  await el.locator("[data-peek=simple]").click();
  await expect(el.locator("[data-simple-toolbar]")).toBeVisible();

  // global toggle
  dataUrls.length = 0;
  await page.locator("[data-mode-toggle=nerd]").click();
  await expect(el.locator("[data-marginal]")).toBeVisible();
  expect(dataUrls.some((u) => !u.includes("marginal=0"))).toBe(true);
});
