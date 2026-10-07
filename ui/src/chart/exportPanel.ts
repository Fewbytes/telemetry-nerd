// Per-panel export (bead telemetry-nerd-ulh9): "save this panel's chart as PNG/PDF" for sharing
// a single finding outside the app.
//
// How it captures a panel: every chart kind (time, littles, fleet, heatmap, ...) renders one or
// more <canvas> elements into the panel's `.plot` container — some are uPlot's own canvas (one
// per facet/series), some are our own overlay canvases (the coverage rug, the marginal
// histogram), all positioned by CSS (static flow or `position: absolute`). Rather than special-
// case each panel kind, we walk `.plot`'s subtree, read every canvas's rendered position via
// getBoundingClientRect (which is correct for both flow and absolutely-positioned elements), and
// redraw each one at that same relative position onto a single flat composite canvas. This is
// kind-agnostic: a panel with one chart (time) or several (littles' strip + main chart, fleet's
// text + chart) is captured the same way, as long as it draws into <canvas> elements under
// `.plot`.
//
// Theme: the export uses the CURRENT theme's colors (the --bg/--fg tokens already baked into the
// canvases by the chart code), not a forced light default. Rationale: this is a "screenshot of
// what I'm looking at" feature — matching what's on screen avoids a confusing mismatch (axis/grid
// colors picked for dark mode against a white export background), and the chart code already
// computes per-theme colors with a 3:1 contrast guarantee (theme.svelte.ts), so a light-only
// export would need a second render pass. If dark exports turn out to print badly, that's a
// follow-up, not a reason to add render-pass complexity here.
//
// PDF content (bead 71qs): the PDF additionally carries the panel's question, provenance
// ("shown"/"where") line, query expression and full notes/caveats list, scraped from the same
// rendered DOM Panel.svelte shows them in (see panelText.ts) rather than recomputed, so the
// export can never diverge from what the viewer saw. The PNG stays chart-only by design (its
// button is explicitly "save this panel's chart as a PNG image"); only the PDF claims to be a
// shareable, self-contained record of the panel.

import { jsPDF } from "jspdf";
import { ensureSpace, MARGIN, writeImage, writeSection, writeWrapped } from "../lib/pdfLayout";
import { extractPanelText, type PanelText } from "./panelText";

export interface CapturedPanel {
  canvas: HTMLCanvasElement;
  width: number;
  height: number;
}

/** Composite every <canvas> under `root` (a panel's `.plot` element) into one flat canvas,
 * preserving each one's rendered position. Returns null if there is nothing to capture
 * (e.g. the panel hasn't drawn yet). */
export function capturePlot(root: HTMLElement): CapturedPanel | null {
  const sources = Array.from(root.querySelectorAll("canvas")).filter((c) => c.width > 0 && c.height > 0);
  if (sources.length === 0) return null;

  const rootRect = root.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, Math.ceil(rootRect.width));
  const cssH = Math.max(1, Math.ceil(rootRect.height));

  const out = document.createElement("canvas");
  out.width = Math.round(cssW * dpr);
  out.height = Math.round(cssH * dpr);
  const ctx = out.getContext("2d");
  if (!ctx) return null;

  const bg = getComputedStyle(root).getPropertyValue("--bg").trim() || "#ffffff";
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, out.width, out.height);

  for (const c of sources) {
    const r = c.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const x = (r.left - rootRect.left) * dpr;
    const y = (r.top - rootRect.top) * dpr;
    const w = r.width * dpr;
    const h = r.height * dpr;
    ctx.drawImage(c, x, y, w, h);
  }
  return { canvas: out, width: out.width, height: out.height };
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // revoke once the browser's had a chance to start the download
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** panel id -> a filesystem-safe base name (no extension). */
export function exportBaseName(panelId: string): string {
  return `telemetry-nerd-${panelId}`;
}

export function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (b) => (b ? resolve(b) : reject(new Error(`canvas.toBlob(${type}) produced no blob`))),
      type,
      quality,
    );
  });
}

/** Capture `root` and download it as a PNG. Throws if there's nothing to capture yet. */
export async function exportPanelPng(panelId: string, root: HTMLElement): Promise<void> {
  const cap = capturePlot(root);
  if (!cap) throw new Error("nothing to export yet — the chart hasn't drawn");
  const blob = await canvasToBlob(cap.canvas, "image/png");
  triggerDownload(blob, `${exportBaseName(panelId)}.png`);
}

/** A captured chart, as a data URL (not a live canvas): what buildPanelPdf lays out, so it stays
 * DOM/canvas-free and unit-testable, mirroring exportPdf.ts's CapturedPanel. */
export interface CapturedImage { dataUrl: string; width: number; height: number }

/** Lay out one panel's PDF: title/question, the chart image, then its provenance line, query and
 * notes/caveats — pure (no DOM), so this is the part unit tests exercise directly. */
export function buildPanelPdf(panelId: string, image: CapturedImage, info: PanelText): jsPDF {
  const pdf = new jsPDF({ unit: "pt", format: "a4" });
  let y = MARGIN;
  pdf.setFontSize(14);
  y = writeWrapped(pdf, `${panelId}: ${info.question}`, y);
  y += 4;
  pdf.setFontSize(10);
  if (info.shown) y = writeWrapped(pdf, info.shown, y);
  if (info.where) y = writeWrapped(pdf, info.where, y);
  y += 6;

  y = ensureSpace(pdf, y, 60);
  y = writeImage(pdf, y, image.dataUrl, image.width, image.height);

  if (info.query) {
    pdf.setFontSize(9);
    y = writeWrapped(pdf, `Query: ${info.query}`, y);
    y += 10;
  }
  writeSection(pdf, "Notes & caveats", info.notes.map((n) => `[${n.kind}] ${n.text}`), y);
  return pdf;
}

/** Capture `plotEl` and `sectionEl`'s rendered notes/question/provenance and download a PDF.
 * Throws if there's nothing to capture yet. */
export async function exportPanelPdf(panelId: string, plotEl: HTMLElement, sectionEl: HTMLElement): Promise<void> {
  const cap = capturePlot(plotEl);
  if (!cap) throw new Error("nothing to export yet — the chart hasn't drawn");
  const info = extractPanelText(sectionEl);
  const image: CapturedImage = { dataUrl: cap.canvas.toDataURL("image/png"), width: cap.width, height: cap.height };
  const pdf = buildPanelPdf(panelId, image, info);
  triggerDownload(pdf.output("blob"), `${exportBaseName(panelId)}.pdf`);
}
