import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

test("sidebar chat: first message creates an anchor-less thread; panel refs highlight on hover", async ({
  page,
  request,
}) => {
  const panel = await seedPanel(request, "chat ref target");
  await page.goto("/");

  const chat = page.locator("#chat");
  await chat.getByLabel("Chat with Claude").fill(`please look at ${panel.id} and p9999`);
  await chat.getByRole("button", { name: "Send" }).click();

  const message = chat.locator(".message .text").last();
  await expect(message).toContainText("please look at");
  await expect(chat.locator(".message .badge.user")).toHaveCount(1);
  await expect(chat.locator('[data-testid^="delivery-"]')).toBeVisible();

  // anchor-less thread: not rendered under any panel
  await expect(page.locator(`#panel-${panel.id} .thread`)).toHaveCount(0);

  // existing id becomes a chip, unknown id stays plain text
  const chip = message.locator(`.ref-chip[data-ref="${panel.id}"]`);
  await expect(chip).toBeVisible();
  await expect(message.locator('.ref-chip[data-ref="p9999"]')).toHaveCount(0);

  const target = page.locator(`#panel-${panel.id}`);
  await expect(target).not.toHaveClass(/ref-hover/);
  await chip.hover();
  await expect(target).toHaveClass(/ref-hover/);
  await page.mouse.move(0, 0);
  await expect(target).not.toHaveClass(/ref-hover/);

  // a second message appends to the same thread
  await chat.getByLabel("Chat with Claude").fill("and again");
  await chat.getByRole("button", { name: "Send" }).click();
  await expect(chat.locator(".message .badge.user")).toHaveCount(2);
});
