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

/** Capture `root` and download it as a one-page PDF. Throws if there's nothing to capture yet. */
export async function exportPanelPdf(panelId: string, root: HTMLElement): Promise<void> {
  const cap = capturePlot(root);
  if (!cap) throw new Error("nothing to export yet — the chart hasn't drawn");
  const blob = await canvasToPdfBlob(cap.canvas);
  triggerDownload(blob, `${exportBaseName(panelId)}.pdf`);
}

// --- minimal PDF writer -----------------------------------------------------------------------
//
// Why not a dependency (e.g. jsPDF): a jsPDF-class library pulls in a general-purpose PDF
// document model (text layout, fonts, vector drawing) to solve a problem that, here, is just
// "wrap one raster image in a one-page PDF so the user gets a real file". A canvas can export
// straight to baseline JPEG bytes (canvas.toBlob("image/jpeg")) and the PDF spec can embed a JPEG
// verbatim via the /DCTDecode filter — no re-encoding, no image codec of our own. That reduces
// the whole feature to ~40 lines of PDF object scaffolding around bytes the browser already
// produced, with no new dependency and no bundle-size cost. This is the standard trick small
// "image to PDF" tools use; it stops being the simplest option only if we later need multi-page
// output, text, or vector content, at which point reaching for a real PDF library would be
// justified.

const te = new TextEncoder();

function concatBytes(parts: (Uint8Array | string)[]): Uint8Array {
  const chunks = parts.map((p) => (typeof p === "string" ? te.encode(p) : p));
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}

/** Wrap a canvas's rendered pixels in a minimal single-page PDF (image fills the page, scaled to
 * a comfortable print size at ~144 "px per inch" so the PDF's physical page size is sane). */
export async function canvasToPdfBlob(canvas: HTMLCanvasElement, quality = 0.92): Promise<Blob> {
  const jpegBlob = await canvasToBlob(canvas, "image/jpeg", quality);
  const jpeg = new Uint8Array(await jpegBlob.arrayBuffer());
  return new Blob([buildPdfBytes(jpeg, canvas.width, canvas.height) as BlobPart], { type: "application/pdf" });
}

/** Pure byte-assembly for the single-page JPEG-in-PDF wrapper: no DOM/canvas involved, so this is
 * the part unit tests exercise directly (the canvas-dependent half is a thin, untestable-without-
 * a-browser shell around it). */
export function buildPdfBytes(jpeg: Uint8Array, width: number, height: number): Uint8Array {
  // PDF points are 1/72"; treat the image's device pixels as 144 dpi so pages print at a
  // reasonable physical size instead of one point per pixel (which would yield enormous pages).
  const PPI = 144;
  const pageW = (width / PPI) * 72;
  const pageH = (height / PPI) * 72;

  const content = `q ${pageW.toFixed(2)} 0 0 ${pageH.toFixed(2)} 0 0 cm /Im0 Do Q`;
  const contentBytes = te.encode(content);

  const objects: Uint8Array[] = [];
  objects.push(te.encode("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"));
  objects.push(te.encode("2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"));
  objects.push(
    te.encode(
      `3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageW.toFixed(2)} ${pageH.toFixed(2)}] ` +
        `/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>\nendobj\n`,
    ),
  );
  objects.push(
    concatBytes([
      `4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} ` +
        `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpeg.length} >>\nstream\n`,
      jpeg,
      "\nendstream\nendobj\n",
    ]),
  );
  objects.push(
    concatBytes([`5 0 obj\n<< /Length ${contentBytes.length} >>\nstream\n`, contentBytes, "\nendstream\nendobj\n"]),
  );

  const header = te.encode("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
  const parts: Uint8Array[] = [header];
  const offsets: number[] = [];
  let pos = header.length;
  for (const obj of objects) {
    offsets.push(pos);
    parts.push(obj);
    pos += obj.length;
  }
  const xrefStart = pos;
  let xref = `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const off of offsets) xref += `${String(off).padStart(10, "0")} 00000 n \n`;
  const trailer =
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefStart}\n%%EOF`;
  parts.push(te.encode(xref));
  parts.push(te.encode(trailer));

  return concatBytes(parts);
}
