import type { Annotation, Panel } from "./api";

/**
 * Live annotations anchored to a panel that is no longer open. Closing a panel keeps its
 * annotations in the workspace, but nothing else renders them (only open panels draw
 * annotations), so the sidebar lists them.
 */
export function orphanedAnnotations(annotations: Annotation[], panels: Panel[]): Annotation[] {
  const open = new Set(panels.map((p) => p.id));
  return annotations.filter((a) => !a.deleted && a.panel !== null && !open.has(a.panel));
}
