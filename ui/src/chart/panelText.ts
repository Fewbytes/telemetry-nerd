// What a panel's own notes (bead 71qs) look like once scraped from the already-rendered DOM,
// for export: the export shouldn't silently drop the caveats/question/provenance that qualify
// the chart in-app (a chart without them is misleading once it leaves the app). Scraping the
// rendered text (rather than recomputing it from panelNotes.ts) keeps the export honest — it
// shows exactly what the viewer saw, not a second, possibly-diverging computation of it.

export interface PanelTextNote {
  kind: "caveat" | "info";
  text: string;
}

export interface PanelText {
  question: string;
  shown: string;
  where: string;
  query: string;
  notes: PanelTextNote[];
}

const text = (root: ParentNode, selector: string): string => root.querySelector(selector)?.textContent?.trim() ?? "";

/** Read the question, "what's shown"/provenance line, query expression and notes/caveats list
 * already rendered for a panel (see Panel.svelte: header .question, .shown .what/.where, .query
 * code, .notes .note). `root` is the panel's own `section.panel` element. */
export function extractPanelText(root: ParentNode): PanelText {
  const notes: PanelTextNote[] = Array.from(root.querySelectorAll(".notes .note")).map((li) => ({
    kind: li.classList.contains("caveat") ? "caveat" : "info",
    text: li.querySelector(".note-text")?.textContent?.trim() ?? li.textContent?.trim() ?? "",
  }));
  return {
    question: text(root, "header .question"),
    shown: text(root, ".shown .what"),
    where: text(root, ".shown .where"),
    query: text(root, ".query code"),
    notes,
  };
}
