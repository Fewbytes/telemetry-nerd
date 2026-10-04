import { expect, test } from "@playwright/test";
import { mcpTool } from "./helpers.js";

const SHOTS = "test-results/retro";

// Session retrospective (bead 3fs.4): Claude proposes a catalog update and scoped lessons; the
// user approves, edits, rejects and refutes them by keyboard in the Proposals view; an approved
// lesson surfaces through lessons_for only when its scope matches.
test("proposals: approve, edit, reject, refute; lessons surface by scope", async ({ page, request }) => {
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
    scope: { source: "default", service: "node-exporter" },
    evidence: [finding, panel.id],
  });
  const drop = await mcpTool(request, "lesson_propose", {
    text: "node-exporter never restarts",
    scope: { source: "default", service: "node-exporter" },
    evidence: [finding],
  });
  const { results } = await mcpTool(request, "catalog_propose", {
    claims: [{ metric: "up", field: "bounds", value: "[0,1]", confidence: 0.8, basis: "up is 0 or 1", evidence: [panel.id] }],
  });
  const cp: string = results[0].proposal;
  expect(results[0].status).toBe("proposed");

  await page.goto("/");
  const link = page.locator("[data-proposals-link]");
  await expect(link.locator(".count-badge")).toHaveAttribute("data-pending", "3");
  await link.click();
  const view = page.locator("[data-proposals-view]");
  await expect(view.getByRole("heading", { name: "Proposals", level: 2 })).toBeVisible();

  const kept = page.locator(`[data-proposal="${keep.lesson}"]`);
  await expect(kept.locator("[data-scope]")).toHaveText("source default · service node-exporter");
  await expect(kept.getByRole("link", { name: finding })).toHaveAttribute("href", `#/finding/${finding}`);

  // approve by keyboard: focus moves to the next item awaiting review (lessons are newest first)
  await expect(view.locator("[data-proposal]").first()).toHaveAttribute("data-proposal", drop.lesson);
  await kept.getByRole("button", { name: "Approve" }).focus();
  await page.keyboard.press("Enter");
  await expect(view.getByRole("heading", { name: "Lessons awaiting review (1)" })).toBeVisible();
  // newest first: the next item in reading order is the catalog proposal
  await expect(page.locator(`#proposal-title-${cp}`)).toBeFocused();

  // reject with a comment
  const dropped = page.locator(`[data-proposal="${drop.lesson}"]`);
  await dropped.getByLabel(`Comment on ${drop.lesson}`).fill("it restarts on every deploy");
  await dropped.getByRole("button", { name: "Reject" }).click();
  await expect(view.getByRole("heading", { name: "Lessons awaiting review (0)" })).toBeVisible();

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
  await expect(link.locator(".count-badge")).toHaveCount(0);
  const metric = await (await request.get("/api/catalog/default/up")).json();
  expect(JSON.stringify(metric)).toContain("≥0");

  // decided items are listed on request
  const toggle = view.locator(".toggle-decided");
  await expect(toggle).toHaveText(/show decided \(3\)/);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator(`[data-proposal="${drop.lesson}"]`)).toHaveAttribute("data-state", "rejected");
  await expect(page.locator(`[data-proposal="${cp}"]`)).toContainText("(your value)");

  // scoped surfacing: only with the service named
  const none = await mcpTool(request, "lessons_for", { source: "default" });
  expect(none.lessons).toHaveLength(0);
  expect(none.held).toBe(1);
  const hit = await mcpTool(request, "lessons_for", { source: "default", services: ["node-exporter"] });
  expect(hit.lessons.map((l: { id: string }) => l.id)).toEqual([keep.lesson]);

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
  const after = await mcpTool(request, "lessons_for", { source: "default", services: ["node-exporter"] });
  expect(after.lessons).toHaveLength(0);
});
