import { expect, test } from "./fixtures.js";

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
