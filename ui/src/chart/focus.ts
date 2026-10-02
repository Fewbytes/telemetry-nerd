import type { Where } from "../lib/api";
import type { Note } from "../lib/panelNotes";

export function focusRects(where: Where | null | undefined, toX: (ms: number) => number, x0: number, x1: number): { x: number; w: number }[] {
  return (where?.spans ?? []).flatMap(([a, b]) => {
    const l = Math.max(x0, toX(a)), r = Math.min(x1, toX(b));
    return r > l ? [{ x: l, w: r - l }] : [];
  });
}

/** A note can drive a focus band only when it says where: with no spans there is nothing to show. */
export const hasFocus = (n: Note): boolean => (n.where?.spans?.length ?? 0) > 0;

function notesCovering(notes: Note[], seriesId: string, ms: number): Note[] {
  return notes.filter((n) => n.where?.spans?.some(([a, b]) => ms > a && ms <= b) && (!n.where.series || n.where.series.includes(seriesId)));
}

export const notesAt = (notes: Note[], seriesId: string, ms: number): string[] => notesCovering(notes, seriesId, ms).map((n) => n.key);

/** Why a bucket could not be fetched or judged: the messages of the located untrusted_data caveats covering it. */
export const unknownReasons = (notes: Note[], seriesId: string, ms: number): string[] =>
  notesCovering(notes, seriesId, ms).filter((n) => n.key.startsWith("untrusted_data:")).map((n) => n.text);
