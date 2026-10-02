import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const DAEMON = `http://127.0.0.1:${process.env.E2E_PORT ?? 7071}`;

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

/** Call an MCP tool over the daemon's streamable-HTTP endpoint (how Claude runs tier-2 code). */
export async function mcpTool(
  request: APIRequestContext,
  name: string,
  args: Record<string, unknown>,
): Promise<Record<string, any>> {
  const headers = { "content-type": "application/json", accept: "application/json, text/event-stream" };
  const init = await request.post("/mcp", {
    headers,
    data: {
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "e2e", version: "1" } },
    },
  });
  expect(init.ok()).toBeTruthy();
  const sid = init.headers()["mcp-session-id"];
  const call = await request.post("/mcp", {
    headers: { ...headers, "mcp-session-id": sid },
    data: { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name, arguments: args } },
  });
  expect(call.ok()).toBeTruthy();
  const frame = (await call.text()).split("\n").find((l) => l.startsWith("data:"));
  const res = JSON.parse(frame!.slice(5)).result;
  expect(res.isError, JSON.stringify(res.content)).toBeFalsy();
  return JSON.parse(res.content[0].text);
}
