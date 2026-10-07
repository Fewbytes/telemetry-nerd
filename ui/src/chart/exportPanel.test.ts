import { describe, expect, it } from "vitest";
import { buildPanelPdf, exportBaseName, type CapturedImage } from "./exportPanel";
import type { PanelText } from "./panelText";

describe("exportBaseName", () => {
  it("prefixes the panel id for a predictable, shareable filename", () => {
    expect(exportBaseName("p3")).toBe("telemetry-nerd-p3");
  });
});

const image: CapturedImage = {
  dataUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  width: 400,
  height: 260,
};

const info = (overrides: Partial<PanelText> = {}): PanelText => ({
  question: "is p99 latency elevated?",
  shown: "Average per 1m bucket (line) with its min–max envelope (band).",
  where: "default · 10:00 – 11:00 · step 1m",
  query: "histogram_quantile(0.99, rate(latency_seconds_bucket[5m]))",
  notes: [],
  ...overrides,
});

describe("buildPanelPdf", () => {
  it("is a single-page PDF with a non-trivial size", () => {
    const pdf = buildPanelPdf("p3", image, info());
    expect(pdf.getNumberOfPages()).toBe(1);
    expect(pdf.output("blob").size).toBeGreaterThan(200);
  });

  it("includes the panel id, question, provenance line and query", () => {
    const pdf = buildPanelPdf("p3", image, info());
    const text = pdf.output("datauristring");
    const decoded = atob(text.slice(text.indexOf(",") + 1));
    for (const needle of ["p3", "is p99 latency elevated?", "default", "histogram_quantile"]) {
      expect(decoded).toContain(needle);
    }
  });

  it("lists every note/caveat, tagged by kind", () => {
    const pdf = buildPanelPdf(
      "p3",
      image,
      info({ notes: [{ kind: "caveat", text: "Some buckets have too few observations." }, { kind: "info", text: "The y-axis is scaled to the data." }] }),
    );
    const text = pdf.output("datauristring");
    const decoded = atob(text.slice(text.indexOf(",") + 1));
    expect(decoded).toContain("Notes & caveats");
    expect(decoded).toContain("[caveat] Some buckets have too few observations.");
    expect(decoded).toContain("[info] The y-axis is scaled to the data.");
  });

  it("says '(none)' when there are no notes, rather than omitting the section", () => {
    const pdf = buildPanelPdf("p3", image, info({ notes: [] }));
    const text = pdf.output("datauristring");
    const decoded = atob(text.slice(text.indexOf(",") + 1));
    expect(decoded).toContain("Notes & caveats");
    expect(decoded).toContain("none"); // "(none)"; PDF content streams escape literal parens
  });
});
