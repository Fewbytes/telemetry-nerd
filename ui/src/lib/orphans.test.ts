import { describe, expect, it } from "vitest";
import type { Annotation, Panel } from "./api";
import { orphanedAnnotations } from "./orphans";

const ann = (id: string, panel: string | null, deleted = false): Annotation => ({
  id, kind: "note", panel, t_start_ms: null, t_end_ms: null, value: null, value_hi: null,
  label: id, links: [], author: "claude", created_at_ms: 0, deleted,
});
const panel = (id: string) => ({ id }) as Panel;

describe("orphanedAnnotations", () => {
  it("returns live annotations whose panel is not open", () => {
    expect(orphanedAnnotations([ann("a1", "p1")], [panel("p2")]).map((a) => a.id)).toEqual(["a1"]);
  });
  it("ignores workspace-wide, open-panel and deleted annotations", () => {
    const anns = [ann("a1", null), ann("a2", "p1"), ann("a3", "p9", true)];
    expect(orphanedAnnotations(anns, [panel("p1")])).toEqual([]);
  });
});
