import type { Where } from "../lib/api";
import type { Note } from "../lib/panelNotes";

export function focusRects(where: Where | null | undefined, toX: (ms: number) => number, x0: number, x1: number): { x: number; w: number }[] {
  return (where?.spans ?? []).flatMap(([a, b]) => {
    const l = Math.max(x0, toX(a)), r = Math.min(x1, toX(b));
    return r > l ? [{ x: l, w: r - l }] : [];
  });
}

export function notesAt(notes: Note[], seriesId: string, ms: number): string[] {
  return notes
    .filter((n) => n.where?.spans?.some(([a, b]) => ms > a && ms <= b) && (!n.where.series || n.where.series.includes(seriesId)))
    .map((n) => n.key);
}
