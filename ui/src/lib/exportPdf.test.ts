import { describe, expect, it } from "vitest";
import { buildWorkspacePdf, defaultFileName, findingLine, hypothesisLine, pdfBlob, type CapturedPanel } from "./exportPdf";
import type { Finding, Hypothesis, Panel, Snapshot } from "./api";

const panel = (id: string, closed = false): Panel => ({
  id, question: `question for ${id}`, status: "open", spec: { layers: [], y: { range_mode: "data", unit: null, label: null } },
  dataset_ids: [], created_at_ms: 0, answered_by: null, closed,
});

const hypothesis = (id: string): Hypothesis => ({
  id, statement: `${id} statement`, status: "proposed", author: "claude",
  evidence_for: [], evidence_against: [], created_at_ms: 0, updated_at_ms: 0, hidden: false,
});

const finding = (id: string, verdict: Finding["verdict"] = null): Finding => ({
  id, claim: `${id} claim`, scope: { source: "s", selector: "{}", time_range: { start_ms: 0, end_ms: 1 }, step: "1m", aggregation: "avg", baseline_range: null },
  evidence: [], caveats: [], hypotheses: [], answers_panel: null, author: "claude", created_at_ms: 0,
  verdict, verdict_comment: null,
});

const snapshot = (overrides: Partial<Snapshot> = {}): Snapshot => ({
  panels: [], annotations: [], hypotheses: [], findings: [], gaps: [], threads: [], last_seq: 0,
  workspace: { id: "w1", title: "Demo workspace", question: "why is latency up?", archived: false, created_at_ms: 0 },
  ...overrides,
});

const noCapture = async (): Promise<CapturedPanel | null> => null;

describe("buildWorkspacePdf", () => {
  it("has one summary page plus one page per open panel", async () => {
    const s = snapshot({
      panels: [panel("p1"), panel("p2"), panel("p3", true)],
      hypotheses: [hypothesis("h1")],
      findings: [finding("f1", "accepted")],
    });
    const pdf = await buildWorkspacePdf(s, noCapture);
    // summary page + p1 + p2 (p3 is closed, excluded)
    expect(pdf.getNumberOfPages()).toBe(3);
  });

  it("produces a non-trivial PDF blob", async () => {
    const s = snapshot({ panels: [panel("p1")] });
    const pdf = await buildWorkspacePdf(s, noCapture);
    const blob = pdfBlob(pdf);
    expect(blob.size).toBeGreaterThan(200);
  });

  it("embeds a captured chart image when the capture succeeds", async () => {
    const s = snapshot({ panels: [panel("p1")] });
    let calls = 0;
    const capture = async (p: Panel): Promise<CapturedPanel | null> => {
      calls++;
      expect(p.id).toBe("p1");
      return { dataUrl: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", width: 400, height: 260 };
    };
    const pdf = await buildWorkspacePdf(s, capture);
    expect(calls).toBe(1);
    expect(pdf.getNumberOfPages()).toBe(2);
  });

  it("wraps a long hypotheses/findings list onto extra pages", async () => {
    const many = Array.from({ length: 80 }, (_, i) => hypothesis(`h${i}`));
    const s = snapshot({ hypotheses: many });
    const pdf = await buildWorkspacePdf(s, noCapture);
    expect(pdf.getNumberOfPages()).toBeGreaterThan(1);
  });
});

describe("line formatters", () => {
  it("formats a hypothesis line with id, status and statement", () => {
    expect(hypothesisLine(hypothesis("h7"))).toBe("h7 [proposed] h7 statement");
  });
  it("formats a finding line, using 'open' when there is no verdict yet", () => {
    expect(findingLine(finding("f3"))).toBe("f3 [open] f3 claim");
    expect(findingLine(finding("f4", "rejected"))).toBe("f4 [rejected] f4 claim");
  });
});

describe("defaultFileName", () => {
  it("sanitizes the workspace title into a safe filename", () => {
    expect(defaultFileName("My Workspace / Q3?")).toBe("My_Workspace_Q3_.pdf");
  });
  it("falls back to 'workspace' when the title is empty", () => {
    expect(defaultFileName("")).toBe("workspace.pdf");
  });
});
