import { expect, test } from "./fixtures.js";
import { seedPanel } from "./helpers";

async function holesPanel(request: import("@playwright/test").APIRequestContext) {
  const q = await request.post("/api/query", {
    data: { expr: "tn_demo_holes_seconds", start: "now-6h", end: "now-10m", step: "1m" },
  });
  const { dataset } = await q.json();
  const s = await request.post("/api/show", { data: { dataset, question: "simple notes: caveats" } });
  return (await s.json()).panel as { id: string };
}

test("simple mode: a note is one line, full text on title, click toggles it open", async ({ page, request }) => {
  const panel = await seedPanel(request, "simple notes: one line");
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("canvas").first()).toBeVisible();

  // nerd: tag boxes and plain text (identity guard)
  const nerdNote = el.locator('[data-note="y_scaled_to_data"], [data-note="y_reference"]').first();
  await expect(nerdNote.locator(".tag")).toHaveText("Note");
  await expect(nerdNote.locator(".note-text")).toBeVisible();
  const full = (await nerdNote.locator(".note-text").textContent())!.trim();

  await page.locator("[data-mode-toggle=simple]").click();
  const note = el.locator('[data-note="y_scaled_to_data"], [data-note="y_reference"]').first();
  const row = note.getByRole("button");
  await expect(note.locator(".tag")).toHaveCount(0);
  await expect(note.locator(".sr-only")).toHaveText("Note");
  await expect(row).toHaveAttribute("title", new RegExp(full.slice(0, 20).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  await expect(row).toHaveAttribute("aria-expanded", "false");
  const text = note.locator(".note-text");
  expect(await text.evaluate((n) => getComputedStyle(n).whiteSpace)).toBe("nowrap");
  expect(await text.evaluate((n) => getComputedStyle(n).textOverflow)).toBe("ellipsis");

  await row.click();
  await expect(row).toHaveAttribute("aria-expanded", "true");
  expect(await text.evaluate((n) => getComputedStyle(n).whiteSpace)).not.toBe("nowrap");
  await row.click();
  await expect(row).toHaveAttribute("aria-expanded", "false");
});

test("simple mode: caveat keeps its styling and plot focus; nerd keeps the tag", async ({ page, request }) => {
  const panel = await holesPanel(request);
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator("[data-rug]")).toBeVisible();
  const sel = '[data-note^="missing_data:"]';
  await expect(el.locator(sel).first().locator(".tag").first()).toHaveText("Caveat");

  await page.locator("[data-mode-toggle=simple]").click();
  // a located note (series d's hole) drives the focus band; the first note need not be located
  const note = el.locator(sel).filter({ hasText: 'instance="d"' }).filter({ hasText: "no samples" }).first();
  await expect(note).toHaveClass(/caveat/);
  await expect(note.locator(".tag")).toHaveCount(0);
  await expect(note.locator(".sr-only")).toHaveText("Caveat");
  // the located note is a keyboard stop; its focus/hover handlers drive the plot band (canvas, not
  // asserted here: nerd's band wiring is unchanged and shared via setFocus)
  await note.getByRole("button").focus();
  await expect(note.getByRole("button")).toBeFocused();
  // the plot-to-note direction still lights the note up (rug cell under the pointer -> active)
  const rug = await el.locator("[data-rug]").boundingBox();
  const over = await el.locator(".u-over").first().boundingBox();
  if (!rug || !over) throw new Error("rug or plot not laid out");
  await expect(note).not.toHaveClass(/active/);
  let lit = false;
  for (let i = 1; i < 40 && !lit; i++) {
    await page.mouse.move(over.x + (over.width * i) / 40, rug.y + 4);
    lit = (await el.locator(`${sel}.active`).count()) > 0;
  }
  expect(lit).toBe(true);
});

test("simple mode: an anchored thread is one summary line until expanded", async ({ page, request }) => {
  const panel = await seedPanel(request, "simple notes: threads");
  const t = await request.post("/api/threads", { data: { anchor: panel.id, text: "why the dip?" } });
  expect(t.ok()).toBeTruthy();
  const { id } = await t.json();
  const long = "It is the deploy at noon, " + "x".repeat(80);
  expect((await request.post(`/api/threads/${id}/messages`, { data: { text: long } })).ok()).toBeTruthy();
  await page.goto("/");
  const el = page.locator(`[data-panel-id="${panel.id}"]`);
  await expect(el.locator(".thread")).toBeVisible(); // nerd: full thread

  await page.locator("[data-mode-toggle=simple]").click();
  await expect(el.locator(".thread")).toHaveCount(0);
  const sum = el.locator(".threads").getByRole("button", { name: /2 messages/ });
  await expect(sum).toHaveAttribute("aria-expanded", "false");
  await expect(sum).toContainText("It is the deploy at noon,");
  await expect(sum).toContainText("…");
  await expect(el.getByRole("textbox")).toHaveCount(0);
  await sum.click();
  await expect(sum).toHaveAttribute("aria-expanded", "true");
  await expect(el.locator(".thread")).toBeVisible();
  await expect(el.locator(".thread").getByRole("button", { name: "Send" })).toBeVisible();
});
