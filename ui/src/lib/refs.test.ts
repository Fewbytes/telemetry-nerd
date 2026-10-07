import { describe, expect, it } from "vitest";
import type { Snapshot } from "./api";
import { refTargets, splitRefs, chatThread } from "./refs";

const snap = (over: Partial<Snapshot> = {}): Snapshot => ({
  panels: [
    { id: "p3", question: "Why is checkout slow?", closed: false },
    { id: "p10", question: "Old", closed: true },
  ],
  annotations: [
    { id: "a1", panel: "p3", label: "deploy", deleted: false },
    { id: "a2", panel: "p10", label: "orphan", deleted: false },
    { id: "a3", panel: "p3", label: "gone", deleted: true },
  ],
  hypotheses: [{ id: "h1", statement: "Saturation" }],
  findings: [{ id: "f2", claim: "p99 doubled" }],
  gaps: [{ id: "g1", missing_signal: "queue depth" }],
  threads: [
    { id: "t1", anchor: "p3", messages: [] },
    { id: "t2", anchor: null, messages: [] },
  ],
  last_seq: 0,
  ...over,
} as unknown as Snapshot);

describe("code node refs", () => {
  const targets = refTargets(snap({ code: [{ id: "c3", status: "failed", rerun_of: "c1" }] as unknown as Snapshot["code"] }));
  it("makes c-ids chips only for runs that exist", () => {
    expect(targets.get("c3")).toMatchObject({ kind: "code", domId: null, label: "code run: failed, re-run of c1" });
    const segs = splitRefs("see c3 and c9", targets);
    expect(segs.filter((x) => "ref" in x).map((x) => ("ref" in x ? x.ref : ""))).toEqual(["c3"]);
  });
});

describe("refTargets", () => {
  const t = refTargets(snap());
  it("indexes every live object by id with a DOM target", () => {
    expect(t.get("p3")).toMatchObject({ kind: "panel", domId: "panel-p3", closed: false });
    expect(t.get("f2")).toMatchObject({ kind: "finding", domId: "finding-f2", label: "p99 doubled" });
    expect(t.get("h1")).toMatchObject({ domId: "hypothesis-h1" });
    expect(t.get("g1")).toMatchObject({ domId: "gap-g1" });
    expect(t.get("t1")).toMatchObject({ domId: "thread-t1" });
  });
  it("marks closed panels", () => {
    expect(t.get("p10")).toMatchObject({ closed: true });
  });
  it("points annotations at their open panel, or the sidebar when the panel is closed", () => {
    expect(t.get("a1")?.domId).toBe("panel-p3");
    expect(t.get("a2")?.domId).toBe("annotation-a2");
  });
  it("omits deleted annotations", () => {
    expect(t.has("a3")).toBe(false);
  });
});

describe("hidden hypotheses (fygk)", () => {
  it("points a hidden, uncited hypothesis nowhere, like a closed panel", () => {
    const t = refTargets(snap({ hypotheses: [{ id: "h3", statement: "decoy", hidden: true }] } as never));
    expect(t.get("h3")).toMatchObject({ domId: null, closed: true });
  });
  it("still renders a hidden hypothesis a finding cites: hiding never breaks a citation", () => {
    const t = refTargets(
      snap({
        hypotheses: [{ id: "h3", statement: "decoy", hidden: true }],
        findings: [{ id: "f2", claim: "p99 doubled", hypotheses: [{ id: "h3", stance: "against" }] }],
      } as never),
    );
    expect(t.get("h3")).toMatchObject({ domId: "hypothesis-h3", closed: false });
  });
});

describe("splitRefs", () => {
  const t = refTargets(snap());
  const refs = (s: string) => splitRefs(s, t).flatMap((x) => ("ref" in x ? [x.ref] : []));
  it("finds known ids amid punctuation", () => {
    expect(refs("see p3, then (f2).")).toEqual(["p3", "f2"]);
  });
  it("leaves unknown ids as plain text", () => {
    expect(refs("p4 and f9")).toEqual([]);
  });
  it("matches whole ids only: p1 vs p10, ids inside words", () => {
    expect(refs("p1 p10")).toEqual(["p10"]);
    expect(refs("xp3 p3x p3_")).toEqual([]);
  });
  it("preserves text order and content", () => {
    const segs = splitRefs("a p3 b", t);
    expect(segs.map((s) => ("ref" in s ? s.ref : s.text)).join("|")).toBe("a |p3| b");
  });
  it("returns one text segment when nothing matches", () => {
    expect(splitRefs("nothing", t)).toEqual([{ text: "nothing" }]);
  });
});

describe("chatThread", () => {
  it("returns the first anchor-less thread", () => {
    expect(chatThread(snap().threads)?.id).toBe("t2");
  });
  it("returns null when none", () => {
    expect(chatThread([])).toBeNull();
  });
});
