import { test as base, expect, type APIRequestContext, type Page } from "@playwright/test";
import { basename } from "node:path";
import { FIXTURE } from "./helpers.js";

export { expect };

/** Title prefix of the per-test workspaces; the next test archives them (zek0.2). */
const PREFIX = "e2e·";

/** E2E_CPU_THROTTLE=<rate> (e.g. 4 = 4x slower) for stress runs (zek0.1); off by default. */
const THROTTLE = Number(process.env.E2E_CPU_THROTTLE ?? 0);

async function throttle(page: Page): Promise<void> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: THROTTLE });
}

export type Isolation = {
  /** Unique per test attempt (repeat, retry, time, nonce), [a-z0-9] only: a safe metric-name fragment. */
  uniq: string;
  /** The fresh workspace this test runs in (active before the test body starts). */
  workspace: { id: string; title: string };
};

/**
 * Every test runs isolated on the one shared daemon (bead zek0.2): an auto fixture creates and
 * opens a fresh workspace, so panels, threads, findings, annotations and highlights start empty.
 * Global state (the catalog, lessons, proposals, channel cursors) is not workspace-scoped:
 * specs that write it use `uniq` names (their own metrics, imported with `importSeries`), and
 * never assert global counts.
 */
export const test = base.extend<Isolation>({
  // The throttle is in place before newPage returns, so it covers the first document's boot too
  // (a context "page" listener applies it asynchronously: the first navigation runs at full speed).
  // `page` comes from context.newPage, so it is covered, as are pages a spec opens itself.
  context: async ({ context }, use) => {
    if (THROTTLE > 1) {
      const newPage = context.newPage.bind(context);
      context.newPage = async () => {
        const page = await newPage();
        await throttle(page);
        return page;
      };
    }
    await use(context);
  },
  uniq: async ({}, use, testInfo) => {
    const nonce = Math.random().toString(36).slice(2, 8);
    await use(`r${testInfo.repeatEachIndex}t${testInfo.retry}x${Date.now().toString(36)}${nonce}`);
  },
  workspace: [
    async ({ request, uniq }, use, testInfo) => {
      // file:line, not the test title: a title would match the specs' own role/name lookups
      const title = `${PREFIX} ${basename(testInfo.file)}:${testInfo.line} ${uniq}`;
      const r = await request.post("/api/workspaces", { data: { title } });
      expect(r.ok(), await r.text()).toBeTruthy();
      const ws: { id: string; title: string } = (await r.json()).workspace;
      // archive earlier tests' workspaces (inactive now): the switcher stays short
      const list = await (await request.get("/api/workspaces")).json();
      for (const w of list.workspaces as { id: string; title: string }[]) {
        if (w.id !== ws.id && w.title.startsWith(PREFIX)) {
          const a = await request.post(`/api/workspaces/${w.id}/update`, { data: { archived: true } });
          expect(a.ok(), await a.text()).toBeTruthy();
        }
      }
      await use(ws);
    },
    { auto: true },
  ],
});

/** Import Prometheus exposition text into the fixture source and flush it (queryable now). */
export async function importSeries(request: APIRequestContext, text: string): Promise<void> {
  const put = await request.post(`${FIXTURE}/api/v1/import/prometheus`, { data: text });
  expect(put.ok(), await put.text()).toBeTruthy();
  expect((await request.get(`${FIXTURE}/internal/force_flush`)).ok()).toBeTruthy();
}

/**
 * A test's own copy of the demo shapes under `${prefix}_…`: a latency gauge
 * (`${prefix}_latency_seconds`, instances a/b/c, hourly sine + noise) and a request counter
 * (`${prefix}_requests_total`), 15s samples over the last 6h (as the fixture's own demo history). For specs that write catalog
 * claims or relations: the shared tn_demo_* metrics stay untouched for every other test.
 */
export async function importDemo(request: APIRequestContext, prefix: string): Promise<void> {
  const step = 15_000;
  const end = Math.floor(Date.now() / step) * step;
  const lines: string[] = [];
  let seed = 7;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  for (const instance of ["a", "b", "c"]) {
    let total = 0;
    for (let t = end - 6 * 3_600_000; t <= end; t += step) {
      const phase = (2 * Math.PI * (t % 3_600_000)) / 3_600_000;
      const latency = 0.05 + 0.02 * Math.sin(phase) + 0.01 * rnd();
      total += 600 + Math.floor(300 * rnd());
      lines.push(`${prefix}_latency_seconds{instance="${instance}"} ${latency.toFixed(6)} ${t}`);
      lines.push(`${prefix}_requests_total{instance="${instance}"} ${total} ${t}`);
    }
  }
  await importSeries(request, lines.join("\n") + "\n");
}

/** query + show `expr` over the demo window; returns the panel. */
export async function showExpr(
  request: APIRequestContext,
  expr: string,
  question: string,
  extra: Record<string, unknown> = {},
): Promise<{ id: string; question: string }> {
  const q = await request.post("/api/query", { data: { expr, start: "now-3h", end: "now-10m", step: "1m" } });
  expect(q.ok(), await q.text()).toBeTruthy();
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question, ...extra } });
  expect(s.ok(), await s.text()).toBeTruthy();
  return (await s.json()).panel;
}
