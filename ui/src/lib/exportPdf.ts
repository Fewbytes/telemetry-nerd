import { jsPDF } from "jspdf";
import type { Finding, Hypothesis, Panel, Snapshot } from "./api";

/** One panel captured as a raster image, ready to drop into the PDF. */
export interface CapturedPanel { dataUrl: string; width: number; height: number }
/** How a panel's chart becomes an image; swappable so the layout logic below is DOM-free and testable. */
export type PanelCapture = (panel: Panel) => Promise<CapturedPanel | null>;

const MARGIN = 36;
const LINE_H = 14;

function ensureSpace(pdf: jsPDF, y: number, needed: number): number {
  const pageH = pdf.internal.pageSize.getHeight();
  if (y + needed > pageH - MARGIN) {
    pdf.addPage();
    return MARGIN;
  }
  return y;
}

function writeWrapped(pdf: jsPDF, text: string, y: number): number {
  const pageW = pdf.internal.pageSize.getWidth();
  const lines = pdf.splitTextToSize(text, pageW - MARGIN * 2) as string[];
  for (const line of lines) {
    y = ensureSpace(pdf, y, LINE_H);
    pdf.text(line, MARGIN, y);
    y += LINE_H;
  }
  return y;
}

export const hypothesisLine = (h: Hypothesis): string => `${h.id} [${h.status}] ${h.statement}`;
export const findingLine = (f: Finding): string => `${f.id} [${f.verdict ?? "open"}] ${f.claim}`;

function writeSection(pdf: jsPDF, title: string, lines: string[], y: number): number {
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

/**
 * One PDF for the whole workspace: a summary page (question, hypotheses, findings) followed by
 * one page per open panel holding its captured chart. `capture` is injected so this stays
 * DOM-free and unit-testable; the real entry point (downloadWorkspacePdf) wires in captureFromDom.
 */
export async function buildWorkspacePdf(snapshot: Snapshot, capture: PanelCapture): Promise<jsPDF> {
  const panels = (snapshot.panels ?? []).filter((p) => !p.closed);
  const pdf = new jsPDF({ unit: "pt", format: "a4" });
  const pageW = pdf.internal.pageSize.getWidth();

  let y = MARGIN;
  pdf.setFontSize(18);
  pdf.text(snapshot.workspace.title || "Workspace", MARGIN, y);
  y += 26;
  if (snapshot.workspace.question) {
    pdf.setFontSize(11);
    y = writeWrapped(pdf, `Question: ${snapshot.workspace.question}`, y);
    y += 8;
  }
  y = writeSection(pdf, "Hypotheses", (snapshot.hypotheses ?? []).map(hypothesisLine), y);
  y = writeSection(pdf, "Findings", (snapshot.findings ?? []).map(findingLine), y);

  for (const panel of panels) {
    pdf.addPage();
    let py = MARGIN;
    pdf.setFontSize(13);
    py = writeWrapped(pdf, `${panel.id}: ${panel.question}`, py);
    py += 6;
    const shot = await capture(panel);
    if (shot && shot.width > 0 && shot.height > 0) {
      const maxW = pageW - MARGIN * 2;
      const maxH = pdf.internal.pageSize.getHeight() - py - MARGIN;
      const scale = Math.min(maxW / shot.width, maxH / shot.height, 1);
      const w = shot.width * scale;
      const h = shot.height * scale;
      pdf.addImage(shot.dataUrl, "PNG", MARGIN, py, w, h);
    } else {
      pdf.setFontSize(10);
      pdf.text("(chart not available for capture)", MARGIN, py);
    }
  }
  return pdf;
}

export const pdfBlob = (pdf: jsPDF): Blob => pdf.output("blob");

/** A filesystem-safe name derived from the workspace title. */
export const defaultFileName = (title: string): string => {
  const safe = (title || "workspace").trim().replace(/[^a-z0-9_-]+/gi, "_").slice(0, 80);
  return `${safe || "workspace"}.pdf`;
};

/** Renders a panel already on screen (by its DOM id) to a PNG; the browser-only default capture. */
export const captureFromDom: PanelCapture = async (panel) => {
  const el = document.getElementById(`panel-${panel.id}`);
  if (!el) return null;
  const { toPng } = await import("html-to-image");
  const rect = el.getBoundingClientRect();
  // 1.5x keeps chart text legible without the file size blowing up on a workspace with many panels
  const dataUrl = await toPng(el, { pixelRatio: 1.5, backgroundColor: "#ffffff" });
  return { dataUrl, width: rect.width || el.scrollWidth || 800, height: rect.height || el.scrollHeight || 400 };
};

/** Builds the PDF and triggers a browser download of it. */
export async function downloadWorkspacePdf(snapshot: Snapshot, capture: PanelCapture = captureFromDom): Promise<void> {
  const pdf = await buildWorkspacePdf(snapshot, capture);
  const blob = pdfBlob(pdf);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = defaultFileName(snapshot.workspace.title);
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
