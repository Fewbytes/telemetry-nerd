import { execFileSync } from "node:child_process";
import { expect, test } from "./fixtures.js";
import { DAEMON, seedPanel } from "./helpers.js";

/** A stand-in for the stdio bridge: holds /ws/bridge like `telemetry-nerd bridge` does. */
class FakeBridge {
  private ws: WebSocket;
  readonly deliveries: { up_to: number; content: string }[] = [];
  autoAck = true;

  private constructor(ws: WebSocket) {
    this.ws = ws;
    ws.onmessage = (m) => {
      const frame = JSON.parse(String(m.data));
      if (frame.type !== "deliver") return;
      this.deliveries.push(frame);
      if (this.autoAck) this.send({ type: "ack", up_to: frame.up_to });
    };
  }

  static async open(): Promise<FakeBridge> {
    const ws = new WebSocket(`${DAEMON.replace("http", "ws")}/ws/bridge?consumer=claude`);
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
    });
    return new FakeBridge(ws);
  }

  send(frame: Record<string, unknown>): void {
    this.ws.send(JSON.stringify(frame));
  }

  close(): void {
    this.ws.close();
  }
}

test("pill follows the bridge: offline → terminal only → live → offline", async ({ page }) => {
  await page.goto("/");
  const label = page.getByTestId("connection-label");
  await expect(label).toHaveText("Claude offline");

  await label.click();
  await expect(page.locator(".connection-detail code")).toContainText("claude ");

  const bridge = await FakeBridge.open();
  try {
    bridge.send({ type: "hello", mode: "hook" });
    bridge.send({ type: "ready" });
    await expect(label).toHaveText("Terminal only");
    bridge.send({ type: "mode", mode: "channel" });
    await expect(label).toHaveText(/^Claude live/); // plus the session id when one announces itself
  } finally {
    bridge.close();
  }
  await expect(label).toHaveText("Claude offline");
});

test("pill shows the daemon unreachable while the UI socket is down", async ({ page }) => {
  let refuse = false;
  const live: { close: () => Promise<void> }[] = [];
  await page.routeWebSocket(/\/ws(\?|$)/, (ws) => {
    if (refuse) {
      void ws.close();
      return;
    }
    ws.connectToServer();
    live.push(ws);
  });
  await page.goto("/");
  const label = page.getByTestId("connection-label");
  await expect(label).toHaveText("Claude offline");

  refuse = true;
  await Promise.all(live.map((ws) => ws.close())); // drop it; reconnects are refused
  await expect(label).toHaveText("Daemon unreachable");

  refuse = false; // the next 1 s reconnect gets through and presence comes back
  await expect(label).toHaveText("Claude offline", { timeout: 10_000 });
});

test("message status: queued → delivered → answered", async ({ page, request }) => {
  const panel = await seedPanel(request, "Does the message status track delivery?");
  const t = await (
    await request.post("/api/threads", { data: { text: "is this delivered?", anchor: panel.id } })
  ).json();
  const userMsg: string = t.messages[0].id;

  await page.goto("/");
  const status = page.getByTestId(`delivery-${userMsg}`);
  await expect(status).toHaveText("queued");

  const bridge = await FakeBridge.open();
  try {
    bridge.send({ type: "hello", mode: "channel" });
    bridge.send({ type: "ready" });
    await expect(status).toHaveText("delivered");
    expect(bridge.deliveries.some((d) => d.content.includes("is this delivered?"))).toBe(true);
  } finally {
    bridge.close();
  }

  const args = JSON.stringify({ thread: t.id, text: "yes, it arrived" });
  execFileSync(
    "uv",
    ["run", "python", "scripts/mcp_call.py", "--url", `${DAEMON}/mcp`, "reply", args],
    { cwd: "..", encoding: "utf8" },
  );
  await expect(status).toHaveText("answered");
});
