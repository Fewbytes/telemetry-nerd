import type { CodeBrief, CodeNode, Panel } from "./api";

/** What a run is, in words AND a glyph: status is never carried by colour alone. */
export interface StatusView { glyph: string; text: string; tone: "ok" | "failed" | "running" }

const EXEC_TEXT: Record<string, string> = {
  error: "raised an exception",
  timeout: "timed out",
  crashed: "kernel crashed",
  interrupted: "interrupted (the daemon stopped mid-run)",
  not_run: "never reached the kernel",
  cancelled: "cancelled",
};

export function statusView(c: Pick<CodeBrief, "status" | "exec_status">): StatusView {
  if (c.status === "running") return { glyph: "…", text: "running", tone: "running" };
  if (c.status === "ok") return { glyph: "✓", text: "ok", tone: "ok" };
  const why = c.exec_status && c.exec_status !== "ok" ? EXEC_TEXT[c.exec_status] ?? c.exec_status : null;
  return { glyph: "✗", text: why ? `failed: ${why}` : "failed", tone: "failed" };
}

export function durationText(s: number | null | undefined): string {
  if (s == null) return "";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${Number(s.toPrecision(3))} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)} s`;
}

/** Newest runs first (ids are c1, c2, ...: numeric order, not string order). */
export const newestFirst = (runs: CodeBrief[]): CodeBrief[] =>
  [...runs].sort((a, b) => Number(b.id.slice(1)) - Number(a.id.slice(1)));

export interface BoundedText { text: string; hiddenLines: number }

/** The last `maxLines` lines (the end of a stream is what shows why it stopped) of at most `maxChars`. */
export function boundText(s: string, maxLines = 40, maxChars = 6000): BoundedText {
  let lines = s.replace(/\n$/, "").split("\n");
  let hidden = 0;
  if (lines.length > maxLines) { hidden = lines.length - maxLines; lines = lines.slice(-maxLines); }
  let text = lines.join("\n");
  if (text.length > maxChars) {
    const from = text.length - maxChars;
    // start on a line boundary: drop the partial first line unless the cut is already on one
    const start = text[from - 1] === "\n" ? from : text.indexOf("\n", from) + 1 || from;
    hidden += text.slice(0, start).split("\n").length - 1;
    text = text.slice(start);
  }
  return { text, hiddenLines: hidden };
}

/** An input dataset and the open panel that draws it, if any (for a link). */
export interface InputLink { dataset: string; panel: string | null }

export function inputLinks(c: Pick<CodeNode, "inputs">, panels: Pick<Panel, "id" | "dataset_ids" | "closed">[]): InputLink[] {
  return c.inputs.map((dataset) => ({
    dataset,
    panel: panels.find((p) => !p.closed && p.dataset_ids.includes(dataset))?.id ?? null,
  }));
}

export const outputLinks = (datasets: string[], panels: Pick<Panel, "id" | "dataset_ids" | "closed">[]): InputLink[] =>
  inputLinks({ inputs: datasets }, panels);

/** One line for the activity list: "c3 ✓ ok · 1.2 s · d1 → d4". */
export function runSummary(c: CodeBrief): string {
  const st = statusView(c);
  const parts = [`${st.glyph} ${st.text}`];
  const d = durationText(c.duration_s);
  if (d) parts.push(d);
  if (c.inputs.length || c.outputs.length) parts.push(`${c.inputs.join(", ") || "no inputs"} → ${c.outputs.join(", ") || "no outputs"}`);
  return parts.join(" · ");
}

// --- a tiny Python highlighter: enough to read code, not a parser --------------------------

export type TokKind = "kw" | "str" | "com" | "num" | "builtin" | "deco" | "plain";
export interface Token { text: string; kind: TokKind }

const KEYWORDS = new Set(
  ("False None True and as assert async await break class continue def del elif else except finally for from " +
    "global if import in is lambda nonlocal not or pass raise return try while with yield match case").split(" "),
);
const BUILTINS = new Set(
  ("abs all any bool dict enumerate float int len list map max min print range round set sorted str sum tuple zip " +
    "isinstance open type self cls").split(" "),
);

// order matters: comment, triple-quoted string, string (with prefix), decorator, number, word
const TOKEN = new RegExp(
  [
    String.raw`(?<com>#[^\n]*)`,
    String.raw`(?<tstr>[rRbBfFuU]{0,2}(?:"""[\s\S]*?(?:"""|(?![\s\S]))|'''[\s\S]*?(?:'''|(?![\s\S]))))`,
    String.raw`(?<str>[rRbBfFuU]{0,2}(?:"(?:\\.|[^"\\\n])*(?:"|$)|'(?:\\.|[^'\\\n])*(?:'|$)))`,
    String.raw`(?<deco>^[ \t]*@[\w.]+)`,
    String.raw`(?<num>\b(?:0[xX][\da-fA-F_]+|\d[\d_]*\.?[\d_]*(?:[eE][+-]?\d+)?j?)\b)`,
    String.raw`(?<word>[A-Za-z_]\w*)`,
  ].join("|"),
  "gm",
);

/** Split Python source into runs; concatenating every `text` gives the source back exactly. */
export function highlightPython(src: string): Token[] {
  const out: Token[] = [];
  const push = (text: string, kind: TokKind) => {
    if (!text) return;
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.text += text;
    else out.push({ text, kind });
  };
  let at = 0;
  for (const m of src.matchAll(TOKEN)) {
    push(src.slice(at, m.index), "plain");
    const g = m.groups!;
    const text = m[0];
    if (g.com !== undefined) push(text, "com");
    else if (g.tstr !== undefined || g.str !== undefined) push(text, "str");
    else if (g.deco !== undefined) push(text, "deco");
    else if (g.num !== undefined) push(text, "num");
    else push(text, KEYWORDS.has(text) ? "kw" : BUILTINS.has(text) ? "builtin" : "plain");
    at = m.index + text.length;
  }
  push(src.slice(at), "plain");
  return out;
}
