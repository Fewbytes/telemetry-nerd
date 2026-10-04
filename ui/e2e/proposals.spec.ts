import type { APIRequestContext } from "@playwright/test";
import { expect, importDemo, test } from "./fixtures.js";
import { mcpTool } from "./helpers.js";

const SHOTS = "test-results/retro";

// Session retrospective (bead 3fs.4): Claude proposes a catalog update and scoped lessons; the
// user approves, edits, rejects and refutes them by keyboard in the Proposals view; an approved
// lesson surfaces through lessons_for only when its scope matches.
// Lessons and proposals are global (not per workspace): this test proposes on a metric of its own,
// counts relative to what was there before, asserts on its own ids only, and rejects whatever it
// left undecided (zek0.2).
const created: string[] = [];

async function proposalCounts(request: APIRequestContext) {
  const all = await (await request.get("/api/proposals")).json();
  const rows = [...all.catalog, ...all.lessons] as { status?: string; state?: string }[];
  const decided = rows.filter((r) => (r.status ?? r.state) !== "proposed").length;
  const lessons = (all.lessons as { state: string }[]).filter((l) => l.state === "proposed").length;
  return { pending: all.pending as number, decided, lessons };
}

test.afterEach(async ({ request }) => {
  const open = (await (await request.get("/api/proposals?status=proposed")).json()) as Record<string, { id: string }[]>;
  const left = [...open.catalog, ...open.lessons].map((r) => r.id).filter((id) => created.includes(id));
  for (const id of left) await request.post(`/api/proposals/${id}/decide`, { data: { decision: "reject" } });
  created.length = 0;
});

test("proposals: approve, edit, reject, refute; lessons surface by scope", async ({ page, request, uniq }) => {
  const service = "node-exporter"; // what the evidence covers (lesson_beyond_evidence)
  const P = `tn_e2e_prop${uniq}`;
  await importDemo(request, P);
  const metricName = `${P}_latency_seconds`;
  const before = await proposalCounts(request);
  const heldBefore = (await mcpTool(request, "lessons_for", { source: "default" })).held as number;
  const selector = 'up{job="node-exporter"}';
  const q = await request.post("/api/query", { data: { expr: selector, start: "now-1h", end: "now", step: "1m" } });
  expect(q.ok()).toBeTruthy();
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "Is node-exporter scraped continuously?" } });
  expect(s.ok()).toBeTruthy();
  const { panel } = await s.json();
  const { finding } = await mcpTool(request, "finding_create", {
    claim: "node-exporter was up for the whole hour",
    scope: { source: "default", selector, start: "now-1h", end: "now", step: "1m", aggregation: "min" },
    evidence: [{ kind: "panel", panel: panel.id }],
  });
  await mcpTool(request, "source_learn", { source: "default" });

  const keep = await mcpTool(request, "lesson_propose", {
    text: "check up{job} before reading node-exporter gauges: gaps read as drops",
    scope: { source: "default", service },
    evidence: [finding, panel.id],
  });
  const drop = await mcpTool(request, "lesson_propose", {
    text: "node-exporter never restarts",
    scope: { source: "default", service },
    evidence: [finding],
  });
  const { results } = await mcpTool(request, "catalog_propose", {
    claims: [{ metric: metricName, field: "bounds", value: "[0,1]", confidence: 0.8, basis: "e2e: a bounded latency", evidence: [panel.id] }],
  });
  const cp: string = results[0].proposal;
  expect(results[0].status, JSON.stringify(results)).toBe("proposed");
  created.push(keep.lesson, drop.lesson, cp);

  await page.goto("/");
  const link = page.locator("[data-proposals-link]");
  await expect(link.locator(".count-badge")).toHaveAttribute("data-pending", String(before.pending + 3));
  await link.click();
  const view = page.locator("[data-proposals-view]");
  await expect(view.getByRole("heading", { name: "Proposals", level: 2 })).toBeVisible();

  const kept = page.locator(`[data-proposal="${keep.lesson}"]`);
  await expect(kept.locator("[data-scope]")).toHaveText(`source default · service ${service}`);
  await expect(kept.getByRole("link", { name: finding })).toHaveAttribute("href", `#/finding/${finding}`);

  // approve by keyboard: focus moves to the next item awaiting review (lessons are newest first)
  await expect(view.locator("[data-proposal]").first()).toHaveAttribute("data-proposal", drop.lesson);
  await kept.getByRole("button", { name: "Approve" }).focus();
  await page.keyboard.press("Enter");
  await expect(view.getByRole("heading", { name: `Lessons awaiting review (${before.lessons + 1})` })).toBeVisible();
  // newest first: the next item in reading order is the catalog proposal
  await expect(page.locator(`#proposal-title-${cp}`)).toBeFocused();

  // reject with a comment
  const dropped = page.locator(`[data-proposal="${drop.lesson}"]`);
  await dropped.getByLabel(`Comment on ${drop.lesson}`).fill("it restarts on every deploy");
  await dropped.getByRole("button", { name: "Reject" }).click();
  await expect(view.getByRole("heading", { name: `Lessons awaiting review (${before.lessons})` })).toBeVisible();

  // edit the catalog value: Escape cancels back to Edit, Enter saves and approves
  const proposal = page.locator(`[data-proposal="${cp}"]`);
  const edit = proposal.getByRole("button", { name: "Edit" });
  await edit.click();
  await expect(proposal.getByLabel("Value")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(proposal.getByRole("button", { name: "Edit" })).toBeFocused();
  await proposal.getByRole("button", { name: "Edit" }).click();
  await proposal.getByLabel("Value").fill("≥0");
  await proposal.getByLabel("Value").press("Enter");
  if (before.pending === 0) await expect(link.locator(".count-badge")).toHaveCount(0);
  else await expect(link.locator(".count-badge")).toHaveAttribute("data-pending", String(before.pending));
  const metric = await (await request.get(`/api/catalog/default/${metricName}`)).json();
  expect(JSON.stringify(metric)).toContain("≥0");

  // decided items are listed on request
  const toggle = view.locator(".toggle-decided");
  await expect(toggle).toHaveText(new RegExp(`show decided \\(${before.decided + 3}\\)`));
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(`[data-proposal="${drop.lesson}"]`)).toHaveAttribute("data-state", "rejected");
  await expect(page.locator(`[data-proposal="${cp}"]`)).toContainText("(your value)");

  // scoped surfacing: only with the service named
  const none = await mcpTool(request, "lessons_for", { source: "default" });
  expect(none.lessons.map((l: { id: string }) => l.id)).not.toContain(keep.lesson);
  expect(none.held).toBe(heldBefore + 1);
  const hit = await mcpTool(request, "lessons_for", { source: "default", services: [service] });
  const hitIds = hit.lessons.map((l: { id: string }) => l.id);
  expect(hitIds).toContain(keep.lesson);
  expect(hitIds).not.toContain(drop.lesson);

  for (const theme of ["light", "dark"]) {
    await page.getByLabel("Theme").selectOption(theme);
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await view.screenshot({ path: `${SHOTS}/proposals-${theme}.png` });
  }

  // refute the approved lesson: it no longer surfaces
  const approved = page.locator(`[data-proposal="${keep.lesson}"]`);
  await approved.getByRole("button", { name: "Refute" }).click();
  await approved.getByLabel("Why it no longer holds").fill("scrapes moved to a new job");
  await approved.getByLabel("Why it no longer holds").press("Enter");
  await expect(approved).toHaveAttribute("data-state", "refuted");
  const after = await mcpTool(request, "lessons_for", { source: "default", services: [service] });
  expect(after.lessons.map((l: { id: string }) => l.id)).not.toContain(keep.lesson);
});
