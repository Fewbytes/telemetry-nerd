import { describe, expect, it } from "vitest";
import { buildPdfBytes, exportBaseName } from "./exportPanel";

describe("exportBaseName", () => {
  it("prefixes the panel id for a predictable, shareable filename", () => {
    expect(exportBaseName("p3")).toBe("telemetry-nerd-p3");
  });
});

describe("buildPdfBytes", () => {
  const td = new TextDecoder("latin1"); // byte-for-byte, no UTF-8 reinterpretation
  // a 2x2 all-black JPEG (handcrafted-enough to be a plausible stand-in for canvas.toBlob output);
  // the PDF writer never looks inside it, just wraps it, so content doesn't matter here
  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xdb, 0, 0, 0, 0, 0xff, 0xd9]);

  it("produces a well-formed single-page PDF wrapping the JPEG via DCTDecode", () => {
    const bytes = buildPdfBytes(jpeg, 200, 100);
    const text = td.decode(bytes);

    expect(text.startsWith("%PDF-1.4")).toBe(true);
    expect(text).toContain("/Type /Catalog");
    expect(text).toContain("/Type /Page");
    expect(text).toContain("/Subtype /Image");
    expect(text).toContain("/Filter /DCTDecode");
    expect(text).toContain("/Width 200 /Height 100");
    expect(text.trimEnd().endsWith("%%EOF")).toBe(true);

    // the JPEG bytes are embedded verbatim (no re-encoding)
    const jpegText = td.decode(jpeg);
    expect(text).toContain(jpegText);

    // the page is sized from the image at 144 "px per inch": 200px -> 100pt, 100px -> 50pt
    expect(text).toContain("/MediaBox [0 0 100.00 50.00]");
  });

  it("points startxref at the actual byte offset of the xref table", () => {
    const bytes = buildPdfBytes(jpeg, 10, 10);
    const text = td.decode(bytes);
    const m = text.match(/startxref\n(\d+)\n/);
    expect(m).not.toBeNull();
    const offset = Number(m![1]);
    expect(text.slice(offset, offset + 4)).toBe("xref");
  });

  it("gives every xref entry an offset that lands exactly on its object's header", () => {
    const bytes = buildPdfBytes(jpeg, 10, 10);
    const text = td.decode(bytes);
    const xrefBlock = text.slice(text.indexOf("xref\n"), text.indexOf("trailer"));
    const entries = [...xrefBlock.matchAll(/(\d{10}) 00000 n \n/g)].map((m) => Number(m[1]));
    expect(entries).toHaveLength(5); // objects 1..5
    entries.forEach((off, i) => {
      expect(text.slice(off, off + 7)).toBe(`${i + 1} 0 obj`);
    });
  });
});
