import type { jsPDF } from "jspdf";

/** Shared page-layout helpers for jsPDF documents (workspace export and per-panel export both
 * lay out wrapped text and images the same way). */
export const MARGIN = 36;
export const LINE_H = 14;

/** Start a new page if `needed` points of vertical space don't fit before the bottom margin. */
export function ensureSpace(pdf: jsPDF, y: number, needed: number): number {
  const pageH = pdf.internal.pageSize.getHeight();
  if (y + needed > pageH - MARGIN) {
    pdf.addPage();
    return MARGIN;
  }
  return y;
}

/** Write `text` word-wrapped to the page width, paginating as needed; returns the y after it. */
export function writeWrapped(pdf: jsPDF, text: string, y: number): number {
  const pageW = pdf.internal.pageSize.getWidth();
  const lines = pdf.splitTextToSize(text, pageW - MARGIN * 2) as string[];
  for (const line of lines) {
    y = ensureSpace(pdf, y, LINE_H);
    pdf.text(line, MARGIN, y);
    y += LINE_H;
  }
  return y;
}

/** A titled list of bullet lines ("(none)" when empty); returns the y after it. */
export function writeSection(pdf: jsPDF, title: string, lines: string[], y: number): number {
  y = ensureSpace(pdf, y, 24);
  pdf.setFontSize(13);
  pdf.text(title, MARGIN, y);
  y += 18;
  pdf.setFontSize(10);
  if (lines.length === 0) {
    y = writeWrapped(pdf, "(none)", y);
  } else {
    for (const line of lines) y = writeWrapped(pdf, `- ${line}`, y);
  }
  return y + 10;
}

/** Place a raster image at `y`, scaled down to fit the remaining width/height of the current
 * page (never upscaled); returns the y after it. */
export function writeImage(pdf: jsPDF, y: number, dataUrl: string, width: number, height: number): number {
  const pageW = pdf.internal.pageSize.getWidth();
  const maxW = pageW - MARGIN * 2;
  const maxH = pdf.internal.pageSize.getHeight() - y - MARGIN;
  const scale = Math.min(maxW / width, maxH / height, 1);
  const w = width * scale;
  const h = height * scale;
  pdf.addImage(dataUrl, "PNG", MARGIN, y, w, h);
  return y + h + 10;
}
