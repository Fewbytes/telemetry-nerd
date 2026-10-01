import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const DAEMON = "http://127.0.0.1:7071";

/** query + show a demo panel, as the skeleton spec does. */
export async function seedPanel(
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
export async function brushMiddleThird(page: Page, panelId: string): Promise<void> {
  const el = page.locator(`[data-panel-id="${panelId}"]`);
  const box = await el.locator(".plot").boundingBox();
  if (box === null) throw new Error("plot is not visible");
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width / 3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + (2 * box.width) / 3, y, { steps: 5 });
  await page.mouse.up();
}